import json
from datetime import datetime, timezone

from brightspace_sync import calendar_events
from brightspace_sync.client import Brightspace
from brightspace_sync.config import Config
from brightspace_sync.enrollments import Course
from brightspace_sync.manifest import Manifest
import requests


def test_calendar_uses_millisecond_dates_and_exports_all_pages(monkeypatch, tmp_path):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz):
            return cls(2026, 10, 20, 15, 34, 29, tzinfo=timezone.utc)

    monkeypatch.setattr(calendar_events, 'datetime', FixedTime)
    bs = Brightspace(requests.Session(), Config())
    monkeypatch.setattr(bs, 'version', lambda product: '1.99')
    calls = []
    first = '/d2l/api/le/1.99/1148605/calendar/events/myEvents/'
    next_url = bs.base + first + '?bookmark=next&startDateTime=2026-06-22T15%3A34%3A29.000Z'

    def get_json(path, *, params, default):
        calls.append((path, dict(params)))
        if path == first:
            return {'Objects': [{'CalendarEventId': 2, 'Title': 'Later',
                                 'StartDateTime': '2026-10-20T00:00:00.000Z'}], 'Next': next_url}
        assert path == next_url
        return {'Objects': [{'CalendarEventId': 1, 'Title': 'Earlier',
                             'StartDateTime': '2026-10-19T00:00:00.000Z'}], 'Next': None}

    monkeypatch.setattr(bs, 'get_json', get_json)
    course = Course(1148605, 'Advising', 'ADVISE', True, True)
    manifest = Manifest.load(tmp_path)
    assert calendar_events.sync_calendar(bs, course, tmp_path, manifest, dry_run=False) == 2
    assert calls[0][1] == {'startDateTime': '2026-06-22T15:34:29.000Z',
                           'endDateTime': '2027-06-17T15:34:29.000Z'}
    assert calls[1][1] == {}
    output = json.loads((tmp_path / 'calendar.json').read_text())
    assert [event['id'] for event in output['events']] == [1, 2]
    assert 'calendar_synced_at' in manifest.data


def test_calendar_dry_run_does_not_write(monkeypatch, tmp_path):
    bs = Brightspace(requests.Session(), Config())
    monkeypatch.setattr(bs, 'version', lambda product: '1.99')
    monkeypatch.setattr(bs, 'get_json', lambda *args, **kwargs: {'Objects': [], 'Next': None})
    course = Course(42, 'Example', 'EXAMPLE', True, True)
    assert calendar_events.sync_calendar(bs, course, tmp_path, Manifest.load(tmp_path), dry_run=True) == 0
    assert not (tmp_path / 'calendar.json').exists()
