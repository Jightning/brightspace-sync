import requests
import pytest

from brightspace_sync.client import Brightspace, AuthExpired, BrightspaceError
from brightspace_sync.config import Config


def response(status=200, body=b'{}'):
    result = requests.Response()
    result.status_code = status
    result._content = body
    return result


def test_version_discovery_uses_product_route_strings_and_numeric_sort(monkeypatch):
    bs = Brightspace(requests.Session(), Config())
    calls = []
    def get(path):
        calls.append(path)
        return response(body=b'{"SupportedVersions":["1.9","1.99","2.0","unstable"]}')
    monkeypatch.setattr(bs, "_raw_get", get)
    assert bs.le('/42/overview') == '/d2l/api/le/1.99/42/overview'
    assert bs.version('le') == '1.99'
    assert calls == ['/d2l/api/le/versions/']


def test_unavailable_version_discovery_falls_back(monkeypatch):
    bs = Brightspace(requests.Session(), Config())
    monkeypatch.setattr(bs, "_raw_get", lambda p: response(404))
    assert bs.version('le') == '1.0'


@pytest.mark.parametrize('status,optional,raises', [(403, True, False), (403, False, True),
                                                   (401, True, True)])
def test_optional_permission_does_not_hide_authentication_failure(monkeypatch, status, optional, raises):
    session = requests.Session()
    monkeypatch.setattr(session, 'get', lambda *a, **k: response(status))
    bs = Brightspace(session, Config(request_delay_seconds=0))
    if raises:
        with pytest.raises(AuthExpired):
            bs._raw_get('/overview', allow_forbidden=optional)
    else:
        assert bs._raw_get('/overview', allow_forbidden=optional).status_code == 403


def test_paged_still_follows_enrollment_bookmarks(monkeypatch):
    bs = Brightspace(requests.Session(), Config())
    calls = []
    def get_json(path, *, params, default):
        calls.append(dict(params))
        return ({'Items': [{'id': 1}], 'PagingInfo': {'HasMoreItems': True, 'Bookmark': 'second'}}
                if len(calls) == 1 else
                {'Items': [{'id': 2}], 'PagingInfo': {'HasMoreItems': False}})
    monkeypatch.setattr(bs, 'get_json', get_json)
    assert list(bs.paged('/enrollments', params={'type': '3'})) == [{'id': 1}, {'id': 2}]
    assert calls == [{'type': '3'}, {'type': '3', 'bookmark': 'second'}]


def test_paged_accepts_legacy_array(monkeypatch):
    bs = Brightspace(requests.Session(), Config())
    monkeypatch.setattr(bs, 'get_json', lambda *a, **k: [{'id': 1}])
    assert list(bs.paged('/events')) == [{'id': 1}]


def test_paged_rejects_repeating_next_url(monkeypatch):
    bs = Brightspace(requests.Session(), Config())
    monkeypatch.setattr(bs, 'get_json', lambda *a, **k: {'Objects': [], 'Next': '/repeat'})
    with pytest.raises(BrightspaceError, match='repeated Next URL'):
        list(bs.paged('/events'))
