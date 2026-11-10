from types import SimpleNamespace
import json

import pytest
import requests

from brightspace_sync.external import ExternalDownloads, DocumentStream
from brightspace_sync.limits import DownloadBudget, DownloadStopped
from brightspace_sync.manifest import Manifest

BASE = 'https://school.brightspace.com'
PROVIDER = 'https://school.simplesyllabus.com'


def response(url, content=b'', content_type='text/html', status=200, **headers):
    result = requests.Response(); result.url = url; result.status_code = status
    result._content = content; result._content_consumed = True
    result.headers.update({'Content-Type': content_type, **headers})
    return result


def downloader(monkeypatch, tmp_path):
    monkeypatch.setattr('brightspace_sync.external.socket.getaddrinfo',
                        lambda *a, **k: [(None, None, None, None, ('8.8.8.8', 443))])
    bs = SimpleNamespace(base=BASE, session=requests.Session(), _throttle=lambda: None,
                         download_budget=DownloadBudget(tmp_path, 99999, 99999, 999999))
    return ExternalDownloads(bs, {'enabled': True, 'allowed_hosts': ['school.simplesyllabus.com']})


@pytest.mark.parametrize('url', ['http://school.simplesyllabus.com/file.pdf',
                                 'https://127.0.0.1/file.pdf', 'https://evil.example/file.pdf',
                                 'https://user:password@school.simplesyllabus.com/file.pdf',
                                 'file:///etc/passwd', 'https://school.simplesyllabus.com:444/file.pdf'])
def test_rejects_unapproved_destinations(monkeypatch, tmp_path, url):
    ext = downloader(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        ext._validate(url)


def test_blocks_dns_resolving_to_private_address(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr('brightspace_sync.external.socket.getaddrinfo',
                        lambda *a, **k: [(None, None, None, None, ('192.168.1.1', 443))])
    with pytest.raises(ValueError, match='private'):
        ext._validate(PROVIDER + '/file.pdf')


def test_redirect_is_validated_and_external_session_is_isolated(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path); seen = []
    monkeypatch.setattr(ext.bs.session, 'request', lambda *a, **k: pytest.fail('Brightspace session leaked'))
    def request(method, url, **kwargs):
        seen.append(url)
        return response(url, status=302, Location='https://evil.example/steal')
    monkeypatch.setattr(ext.session, 'request', request)
    with pytest.raises(ValueError):
        ext._request(PROVIDER + '/file.pdf')
    assert len(seen) == 1


def test_lti_handoff_fetches_pdf_without_saving_tokens(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path); calls = []
    source = BASE + '/quicklink?type=lti&ou=42'
    pages = [response(source, b'<iframe src="/component?launchLocation=%2Fd2l%2Fle%2Flti%2F42%2FtoolLaunch%2F1"></iframe>'),
             response(BASE + '/d2l/le/lti/42/toolLaunch/1',
                      f"<form method='post' action='{PROVIDER}/ui/lti-login'><input type='hidden' name='login_hint' value='secret'></form>".encode()),
             response(BASE + '/d2l/lti/authenticate',
                      f"<form method='post' action='{PROVIDER}/ui/lti-launch'><input type='hidden' name='id_token' value='another-secret'></form>".encode()),
             response(PROVIDER + '/en-US/sso-redirect?doc_url=%2Fdoc%2Fabc123'),
             response(PROVIDER + '/api2/doc-pdf/abc123/Syllabus.pdf', b'%PDF-1.7 sample', 'application/pdf')]
    def request(url, method='GET', data=None):
        calls.append((url, method, data)); return pages.pop(0)
    monkeypatch.setattr(ext, '_request', request)
    manifest = Manifest.load(tmp_path)
    path = ext.fetch(source, 'Syllabus', tmp_path, manifest)
    assert path and path.read_bytes().startswith(b'%PDF-')
    assert calls[1][0] == BASE + '/d2l/le/lti/42/toolLaunch/1'
    assert calls[2][1] == 'POST'
    manifest.save()
    assert 'another-secret' not in manifest.path.read_text()
    assert 'login_hint' not in manifest.path.read_text()
    assert ext.fetch(source, 'Syllabus duplicate', tmp_path / 'other', manifest) == path
    assert len(calls) == 5


def test_rejects_non_authentication_forms(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    markup = f"<form method='post' action='{PROVIDER}/delete'><input type='hidden' name='state' value='x'></form>".encode()
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(BASE + '/launch', markup))
    with pytest.raises(ValueError, match='form destination'):
        ext._syllabus(BASE + '/launch')


def test_limits_escape_online_fallback_handler(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    ext.bs.download_budget = DownloadBudget(tmp_path, 9999, 1, 9999)
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(PROVIDER + '/file.pdf', b'%PDF-sample',
                                                                'application/pdf', **{'Content-Length': '11'}))
    with pytest.raises(DownloadStopped):
        ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, Manifest.load(tmp_path))
    assert not list(tmp_path.rglob('*.part'))


def test_login_page_retains_previous_good_file(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(PROVIDER + '/file.pdf', b'%PDF-sample', 'application/pdf'))
    manifest = Manifest.load(tmp_path); path = ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, manifest)
    ext.results.clear()
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(PROVIDER + '/login', b'<form>Sign in</form>'))
    assert ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, manifest) == path
    assert path.read_bytes() == b'%PDF-sample'


def test_disguised_executable_is_not_saved(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(PROVIDER + '/file.pdf', b'MZmalware', 'application/pdf'))
    assert ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, Manifest.load(tmp_path)) is None
    assert not list(tmp_path.rglob('*.pdf')) and not list(tmp_path.rglob('*.part'))


def test_dry_run_performs_no_external_network(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, '_request', lambda *a, **k: pytest.fail('network during dry run'))
    assert ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, Manifest.load(tmp_path), dry_run=True) is None


def test_provider_authorizes_only_its_exact_signed_pdf_target(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    signed = 'https://s3.amazonaws.com/simple-systems.site.production/school/doc/syllabus/a/b/private.en-US.accessible.pdf?signature=secret'
    calls = []
    def request(url, method='GET', data=None):
        calls.append(url)
        if '/api2/' in url:
            return response(url, json.dumps({'url': signed}).encode(), 'application/json')
        ext._validate(url)
        return response(url, b'%PDF-sample', 'application/pdf')
    monkeypatch.setattr(ext, '_request', request)
    manifest = Manifest.load(tmp_path)
    path = ext.fetch(PROVIDER + '/doc/abc123', 'Syllabus', tmp_path, manifest)
    assert path and len(calls) == 2
    manifest.save()
    assert 'signature' not in manifest.path.read_text()
    with pytest.raises(ValueError):
        ext._validate(signed.replace('signature=secret', 'signature=different'))


@pytest.mark.parametrize('target', ['https://s3.amazonaws.com/unrelated/file.pdf',
                                   'https://s3.amazonaws.com/simple-systems.site.production/other/doc/syllabus/file.pdf',
                                   'https://evil.example/file.pdf'])
def test_provider_metadata_cannot_authorize_arbitrary_storage(monkeypatch, tmp_path, target):
    ext = downloader(monkeypatch, tmp_path); calls = []
    def request(url, *a):
        calls.append(url)
        return response(url, json.dumps({'url': target}).encode(), 'application/json')
    monkeypatch.setattr(ext, '_request', request)
    assert ext.fetch(PROVIDER + '/doc/abc123', 'Syllabus', tmp_path, Manifest.load(tmp_path)) is None
    assert len(calls) == 1


def test_download_url_without_extension_is_detected_by_content_type(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, '_request', lambda *a: response(PROVIDER + '/download?id=42', b'%PDF-sample', 'application/pdf'))
    path = ext.fetch(PROVIDER + '/download?id=42', 'Notes', tmp_path, Manifest.load(tmp_path))
    assert path and path.suffix == '.pdf'


def test_disabling_external_downloads_keeps_existing_offline_copy(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    monkeypatch.setattr(ext, '_request', lambda *a: response(PROVIDER + '/file.pdf', b'%PDF-sample', 'application/pdf'))
    manifest = Manifest.load(tmp_path)
    path = ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, manifest)
    ext.enabled = False
    monkeypatch.setattr(ext, '_request', lambda *a: pytest.fail('disabled network'))
    assert ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, manifest) == path


def test_pdf_signature_can_span_small_network_chunks():
    stream = SimpleNamespace(headers={}, status_code=200,
                             iter_content=lambda **kwargs: iter([b'%', b'PD', b'F-1.7', b' body']))
    assert b''.join(DocumentStream(stream, '.pdf').iter_content()) == b'%PDF-1.7 body'


def test_file_rejection_is_not_cached_as_success(monkeypatch, tmp_path):
    ext = downloader(monkeypatch, tmp_path)
    prompts = []
    ext.bs.download_budget = DownloadBudget(tmp_path, 1, 9999, 9999,
                                           lambda r: prompts.append(r.kind) or False)
    monkeypatch.setattr(ext, '_request', lambda *a, **k: response(PROVIDER + '/file.pdf', b'%PDF-sample',
                                                                'application/pdf', **{'Content-Length': '11'}))
    for _ in range(2):
        assert ext.fetch(PROVIDER + '/file.pdf', 'File', tmp_path, Manifest.load(tmp_path)) is None
    assert prompts == ['file', 'file']
    assert not ext.results
    assert not list(tmp_path.rglob('*.part'))
