from brightspace_sync.sync import SyncOutcome, _report


def test_report_clamps_and_forwards():
    seen = []
    cb = lambda f, m: seen.append((f, m))
    _report(cb, 1.5, "over")
    _report(cb, -0.2, "under")
    _report(cb, 0.5, "mid")
    assert seen == [(1.0, "over"), (0.0, "under"), (0.5, "mid")]


def test_report_tolerates_no_callback():
    _report(None, 0.5, "nobody listening")   # must not raise


def test_outcome_defaults():
    o = SyncOutcome()
    assert o.ok and not o.stopped and not o.auth_failed
    o.stopped = True
    assert not o.ok
