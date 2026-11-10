"""Bounded, allowlisted document downloads; never execute provider page scripts."""

import hashlib
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit, unquote

import requests

from .download import download_to, _name_from_disposition
from .util import log, slugify

_DOCUMENTS = {'.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/plain',
              '.csv': 'text/csv', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
              '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'}
_LTI_FIELDS = {'iss', 'login_hint', 'target_link_uri', 'lti_message_hint', 'client_id',
               'lti_deployment_id', 'id_token', 'state', 'deployment_id'}


class DocumentStream:
    """Reject a login/error page or executable disguised as a document."""
    def __init__(self, response, extension):
        self.response, self.extension = response, extension
        self.headers, self.status_code = response.headers, response.status_code

    def raise_for_status(self):
        self.response.raise_for_status()

    def close(self):
        self.response.close()

    def iter_content(self, **kwargs):
        prefix = bytearray()
        checked = False
        def validate(chunk):
            expected = b'%PDF-' if self.extension == '.pdf' else b'PK' if self.extension in ('.docx', '.xlsx', '.pptx') else None
            if expected and not chunk.startswith(expected):
                raise ValueError('document signature does not match its type')
            if not expected and (chunk.startswith((b'MZ', b'\x7fELF')) or b'\0' in chunk):
                raise ValueError('binary content is not a text document')
        for chunk in self.response.iter_content(**kwargs):
            if not checked:
                prefix.extend(chunk)
                if len(prefix) < 8:
                    continue
                validate(prefix)
                chunk, checked = bytes(prefix), True
            yield chunk
        if not checked:
            if not prefix:
                raise ValueError('empty document')
            validate(prefix)
            yield bytes(prefix)


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.frames, self.forms, self.links = [], [], []
        self.form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'iframe' and attrs.get('src'):
            self.frames.append(attrs['src'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'form':
            self.form = {'action': attrs.get('action', ''), 'method': attrs.get('method', 'get').lower(), 'fields': {}}
            self.forms.append(self.form)
        if tag == 'input' and self.form is not None and attrs.get('name'):
            if attrs.get('type', '').lower() != 'hidden':
                self.form['unsafe'] = True
            self.form['fields'][attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.form = None


class ExternalDownloads:
    def __init__(self, bs, options):
        self.bs = bs
        self.enabled = options.get('enabled', True)
        self.hosts = {h.lower() for h in options.get('allowed_hosts', [])}
        self.brightspace = urlsplit(bs.base).hostname
        self.lti_hosts = {host for host in self.hosts if host.endswith('.simplesyllabus.com')}
        self.session = requests.Session()
        self.session.trust_env = False
        self.results = {}
        self.download_targets = set()

    def _validate(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or (parsed.hostname not in self.hosts | {self.brightspace} and url not in self.download_targets)):
            raise ValueError('destination is not an allowed HTTPS host')
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('local/private network destinations are blocked')

    def _request(self, url, method='GET', data=None):
        for _ in range(8):
            self._validate(url)
            self.bs._throttle()
            session = self.bs.session if urlsplit(url).hostname == self.brightspace else self.session
            response = session.request(method, url, data=data, stream=True,
                                       allow_redirects=False, timeout=(10, 30))
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get('Location', ''))
                response.close()
                # A POST containing LTI tokens is never replayed to another host.
                if method == 'POST' and response.status_code in (307, 308) and urlsplit(url).hostname != urlsplit(target).hostname:
                    raise ValueError('cross-host POST replay blocked')
                if response.status_code in (301, 302, 303):
                    method, data = 'GET', None
                url = target
                continue
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise ValueError(f'provider returned HTTP {status}')
            return response
        raise ValueError('too many redirects')

    def _text(self, response):
        try:
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 ** 2:
                    raise ValueError('provider metadata exceeds the 2 MiB parsing limit')
            return body.decode('utf-8', errors='replace')
        finally:
            response.close()

    def _syllabus(self, url):
        method, data = 'GET', None
        for _ in range(10):
            response = self._request(url, method, data)
            url = response.url
            parsed = urlsplit(url)
            if parsed.hostname in self.lti_hosts and parsed.path.endswith('/sso-redirect'):
                destination = parse_qs(parsed.query).get('doc_url', [None])[0]
                if destination:
                    target = urljoin(url, destination)
                    self._validate(target)
                    if urlsplit(target).hostname == parsed.hostname and re.search(r'/doc/[a-zA-Z0-9]+', urlsplit(target).path):
                        response.close()
                        return target
            if urlsplit(url).hostname in self.hosts and re.search(r'/doc/[a-zA-Z0-9]+', urlsplit(url).path):
                response.close()
                return url
            markup = self._text(response)
            page = Page()
            page.feed(markup)
            if page.frames:
                frame = urljoin(url, page.frames[0])
                location = parse_qs(urlsplit(frame).query).get('launchLocation', [None])[0]
                # Only follow the course-tool launch supplied by Brightspace.
                if location and urlsplit(url).hostname == self.brightspace:
                    frame = urljoin(self.bs.base, location)
                    if urlsplit(frame).hostname != self.brightspace or '/toolLaunch/' not in urlsplit(frame).path:
                        raise ValueError('unexpected tool launch location')
                url, method, data = frame, 'GET', None
                continue
            forms = [f for f in page.forms if not f.get('unsafe') and f['method'] == 'post'
                     and f['fields'] and set(f['fields']) <= _LTI_FIELDS]
            if len(forms) != 1:
                raise ValueError('syllabus launch requires unsupported browser interaction')
            form = forms[0]
            url, method, data = urljoin(url, form['action']), 'POST', form['fields']
            self._validate(url)
            if urlsplit(url).hostname not in self.lti_hosts or urlsplit(url).path not in ('/ui/lti-login', '/ui/lti-launch'):
                raise ValueError('unsupported authentication form destination')
        raise ValueError('syllabus launch exceeded ten steps')

    def fetch(self, source, title, directory, manifest, *, force=False, dry_run=False):
        if dry_run:
            return None
        source = urljoin(self.bs.base + '/', source)
        digest = hashlib.sha256(source.encode()).hexdigest()[:16]
        key = 'external:' + digest
        manifest.touch(key)
        if not self.enabled:
            previous = manifest.get(key)
            return manifest.abs_path(previous) if previous and manifest.abs_path(previous).is_file() else None
        cache_key = (str(manifest.root.resolve()), key)
        if cache_key in self.results:
            return self.results[cache_key]
        try:
            parsed = urlsplit(source)
            query = parse_qs(parsed.query)
            if parsed.hostname == self.brightspace:
                if query.get('type') != ['lti'] or 'syllab' not in title.lower():
                    return None
                document = self._syllabus(source)
                match = re.search(r'/doc/([a-zA-Z0-9]+)', urlsplit(document).path)
                # Simple Syllabus accepts a display filename after the document ID.
                url = urlunsplit(('https', urlsplit(document).netloc,
                                  '/api2/doc-pdf/' + match[1] + '/Syllabus.pdf', '', ''))
                extension = '.pdf'
            elif parsed.hostname in self.lti_hosts and re.search(r'/doc/([a-zA-Z0-9]+)', parsed.path):
                document_id = re.search(r'/doc/([a-zA-Z0-9]+)', parsed.path)[1]
                url = urlunsplit(('https', parsed.netloc, '/api2/doc-pdf/' + document_id + '/Syllabus.pdf', '', ''))
                extension = '.pdf'
            else:
                self._validate(source)
                url = source
                extension = next((e for e in _DOCUMENTS if parsed.path.lower().endswith(e)), None)
            response = self._request(url)
            content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            if extension is None:
                extension = next((e for e, mime in _DOCUMENTS.items() if mime == content_type), None)
                if extension is None and content_type == 'application/octet-stream':
                    filename = _name_from_disposition(response.headers.get('Content-Disposition')) or ''
                    extension = Path(filename).suffix.lower()
                if extension not in _DOCUMENTS:
                    response.close()
                    raise ValueError('this source does not provide a supported document download')
            if extension == '.pdf' and content_type == 'application/json' and urlsplit(url).hostname in self.lti_hosts:
                metadata = json.loads(self._text(response))
                target = metadata.get('url') if isinstance(metadata, dict) else None
                if not isinstance(target, str):
                    raise ValueError('PDF generation did not return a download URL')
                parsed_target = urlsplit(target)
                tenant = urlsplit(url).hostname.removesuffix('.simplesyllabus.com')
                expected = '/simple-systems.site.production/' + tenant + '/doc/syllabus/'
                path = unquote(parsed_target.path)
                if (parsed_target.hostname != 's3.amazonaws.com' or not path.startswith(expected)
                        or not path.endswith('.pdf') or '..' in path.split('/')):
                    raise ValueError('unsupported PDF storage destination')
                # Only this exact, short-lived target is authorized by the provider.
                # It is never written into an index, log, shortcut or manifest.
                self.download_targets.add(target)
                response = self._request(target)
                content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            if content_type != _DOCUMENTS[extension] and content_type != 'application/octet-stream':
                response.close()
                raise ValueError('response is not the expected document type (possibly a login page)')
            wrapper = SimpleNamespace(download_budget=getattr(self.bs, 'download_budget', None),
                                      get_stream=lambda _: DocumentStream(response, extension))
            safe_url = urlunsplit((*urlsplit(url)[:3], '', ''))
            destination = directory / 'Online downloads'
            if not destination.resolve().is_relative_to(manifest.root.resolve()):
                response.close()
                raise ValueError('download destination escapes the course folder')
            filename = slugify(title) + ' - ' + digest + extension
            result = download_to(wrapper, safe_url, dest_dir=destination,
                                 fallback_name=filename, prefer_name=filename,
                                 manifest=manifest, key=key, last_modified=None,
                                 force=True, dry_run=False)
            if result.action == 'declined':
                previous = manifest.get(key)
                return manifest.abs_path(previous) if previous and manifest.abs_path(previous).is_file() else None
            if result.action == 'error':
                raise ValueError('document transfer failed')
            manifest.entries[key]['kind'] = 'external'
            self.results[cache_key] = result.path
            return result.path
        except Exception as exc:
            # Never log launch token values, request URLs or provider response bodies.
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            log.warning('  online only: %s (%s)', title, reason)
            previous = manifest.get(key)
            return manifest.abs_path(previous) if previous and manifest.abs_path(previous).is_file() else None
