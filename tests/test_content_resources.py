import json
from pathlib import Path

import requests

from brightspace_sync.content import sync_content, _wrap_html, _render_html, _write_index
from brightspace_sync.enrollments import Course
from brightspace_sync.manifest import Manifest
from brightspace_sync.navigation import navigation_links


BASE = 'https://school.brightspace.com'
COURSE = Course(42, 'Example course', 'EXAMPLE', True, True)
NAV = '''<a class="d2l-navigation-s-link" href="/launch?ou=42&amp;type=lti">Syllabus</a>
<d2l-menu-item-link text="Syllabus" href="/launch?ou=42&amp;type=lti"></d2l-menu-item-link>
<d2l-menu-item-link text="Other tool" href="https://tool.example/"></d2l-menu-item-link>'''


def response(status=200, body=b'', headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class BrightspaceStub:
    base = BASE

    def __init__(self, overview=None, nav=NAV):
        self.overview = overview or {'Description': {'Html': '<p>Welcome</p>'}, 'HasAttachment': True}
        self.overview_status = 200
        self.nav = nav
        self.streams = 0
        self.fail_attachment = False

    def le(self, path):
        return '/api' + path

    le_unstable = le

    def get_json(self, path, **kwargs):
        return {'Modules': []}

    def _raw_get(self, path, **kwargs):
        if path.endswith('/overview'):
            return response(self.overview_status, json.dumps(self.overview).encode())
        assert path.endswith('/Home')
        return response(body=self.nav.encode())

    def get_stream(self, path):
        assert path.endswith('/overview/attachment')
        self.streams += 1
        return response(404 if self.fail_attachment else 200, b'%PDF-1.4 sample',
                        {'Content-Disposition': 'attachment; filename="Syllabus.pdf"'})


def sync(bs, root, manifest=None, dry_run=False):
    manifest = manifest or Manifest.load(root)
    stats = sync_content(bs, COURSE, root, manifest, force=False, dry_run=dry_run)
    return manifest, stats


def test_navigation_deduplicates_mobile_links_and_rejects_non_web_urls():
    links = navigation_links(NAV + '''<d2l-menu-item-link text="Bad" href="javascript:alert(1)">
        </d2l-menu-item-link><a href="/unrelated">Ignore</a>''', BASE + '/course')
    assert links == [('Syllabus', BASE + '/launch?ou=42&type=lti'),
                     ('Other tool', 'https://tool.example/')]


def test_sync_saves_overview_attachment_and_navigation_in_portable_index(tmp_path):
    manifest, stats = sync(BrightspaceStub(), tmp_path)
    content = tmp_path / 'Content'
    assert (content / 'Course overview/Syllabus.pdf').read_bytes().startswith(b'%PDF')
    assert 'Welcome' in (content / 'Course overview/Overview.html').read_text()
    assert 'Welcome' in (content / 'Course overview/Overview.md').read_text()
    shortcuts = list((content / 'Course links').glob('Syllabus*.url'))
    assert len(shortcuts) == 1
    assert 'URL=' + BASE + '/launch?ou=42&type=lti' in shortcuts[0].read_text()
    index = json.loads((content / 'content_index.json').read_text())
    assert index['modules'][0]['children'][0]['local_path'] == 'Content/Course overview/Syllabus.pdf'
    page = (content / 'index.html').read_text()
    assert '../Content/Course%20overview/Syllabus.pdf' in page
    assert 'type=lti' in page and '(online)' in page and 'Read saved text' in page
    assert str(tmp_path) not in page
    assert stats.downloaded == 1 and stats.links == 2
    assert manifest.prune({'content'}, dry_run=False) == []


def test_overview_rechecked_without_timestamp_and_removed_files_pruned(tmp_path):
    bs = BrightspaceStub()
    manifest, _ = sync(bs, tmp_path)
    manifest.touched.clear()
    bs.overview['Description']['Html'] = '<p>Changed</p>'
    sync(bs, tmp_path, manifest)
    assert bs.streams == 2
    assert 'Changed' in (tmp_path / 'Content/Course overview/Overview.html').read_text()
    manifest.touched.clear()
    bs.overview_status = 404
    sync(bs, tmp_path, manifest)
    removed = manifest.prune({'content'}, dry_run=False)
    assert {p.name for p in removed} == {'Overview.html', 'Overview.md', 'Syllabus.pdf'}


def test_permission_denied_preserves_previous_overview(tmp_path):
    bs = BrightspaceStub()
    manifest, _ = sync(bs, tmp_path)
    manifest.touched.clear()
    bs.overview_status = 403
    bs.nav = ''
    sync(bs, tmp_path, manifest)
    assert manifest.prune({'content'}, dry_run=False) == []


def test_missing_attachment_keeps_online_fallback_and_reports_error(tmp_path):
    bs = BrightspaceStub()
    bs.fail_attachment = True
    _, stats = sync(bs, tmp_path)
    assert stats.errors == 1 and stats.error_detail
    page = (tmp_path / 'Content/index.html').read_text()
    assert BASE + '/api/42/overview/attachment' in page
    assert 'online fallback' in (tmp_path / 'Content/content_index.md').read_text()


def test_dry_run_writes_nothing_and_does_not_download_attachment(tmp_path):
    root = tmp_path / 'missing'
    bs = BrightspaceStub()
    sync(bs, root, dry_run=True)
    assert not root.exists()
    assert bs.streams == 0


def test_saved_description_keeps_local_images_and_absolute_online_links():
    page = _wrap_html('Overview', '<img src="_assets/map.png"><a href="/help">Help</a>', BASE)
    assert '<base ' not in page
    assert 'src="_assets/map.png"' in page
    assert f'href="{BASE}/help"' in page


def test_html_index_resolves_existing_brightspace_links_and_escapes_labels():
    page = _render_html(COURSE, [
        {'type': 'topic', 'title': '<Quiz>', 'topic_type': 'link',
         'source_url': '/d2l/quiz?ou=42&topic=1', 'local_path': 'Content/Quiz.url'},
        {'type': 'topic', 'title': 'Local PDF', 'local_path': 'Content/Notes #1.pdf'},
    ], BASE)
    assert f'href="{BASE}/d2l/quiz?ou=42&amp;topic=1"' in page
    assert '&lt;Quiz&gt;' in page
    assert '../Content/Notes%20%231.pdf' in page


def test_existing_failed_download_gets_online_fallback_instead_of_broken_file_link(tmp_path):
    nodes = [{'type': 'topic', 'title': 'Missing PDF', 'local_path': 'Content/missing.pdf',
              'source_url': '/content/enforced/42/missing.pdf'}]
    _write_index(COURSE, tmp_path / 'Content', nodes, dry_run=False, base_url=BASE)
    page = (tmp_path / 'Content/index.html').read_text()
    assert f'href="{BASE}/content/enforced/42/missing.pdf"' in page
    assert '(online)' in page
    assert '../Content/missing.pdf' not in page
    saved = json.loads((tmp_path / 'Content/content_index.json').read_text())
    assert saved['modules'][0]['local_path'] is None


def test_external_syllabus_is_saved_in_its_course_folder_and_index_opens_offline(monkeypatch, tmp_path):
    bs = BrightspaceStub()
    class External:
        enabled = True
        def fetch(self, source, title, directory, manifest, **kwargs):
            if title != 'Syllabus':
                return None
            path = directory / 'Online downloads/Syllabus.pdf'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'%PDF-test')
            manifest.record('external:syllabus', local=path, last_modified=None)
            return path
    bs.external_downloads = External()
    manifest, stats = sync(bs, tmp_path)
    page = (tmp_path / 'Content/index.html').read_text()
    assert 'Course%20links/Online%20downloads/Syllabus.pdf' in page
    assert '(offline)' in page and 'Online source</a>' in page
    assert stats.external_files == 1
    assert manifest.prune({'content'}, dry_run=False) == []
    saved = json.loads((tmp_path / 'Content/content_index.json').read_text())
    syllabus = saved['modules'][-1]['children'][0]
    assert syllabus['offline_path'] == 'Content/Course links/Online downloads/Syllabus.pdf'


def test_declined_description_image_is_retried_when_description_is_unchanged(tmp_path):
    from brightspace_sync.limits import DownloadBudget
    bs = BrightspaceStub(overview={'Description': {'Html': '<img src="/image.png">'}})
    prompts = []
    bs.download_budget = DownloadBudget(tmp_path, 1, 9999, 9999,
                                       lambda r: prompts.append(r.kind) or False)
    bs.get_stream = lambda path: response(body=b'image', headers={'Content-Length': '5'})
    for _ in range(2):
        sync(bs, tmp_path, Manifest.load(tmp_path))
    assert prompts == ['file', 'file']
    assert not list(tmp_path.rglob('*.part'))
