import queue

from brightspace_sync.config import Config
from brightspace_sync.gui import tasks


def test_sync_worker_is_alive_after_finish(monkeypatch):
    """`_stop` used to shadow threading.Thread._stop(), so is_alive() blew up
    with 'Event object is not callable' once the worker had finished - which is
    exactly the state the window is in when the user quits."""
    outcome = object()
    monkeypatch.setattr(tasks, "run_sync", lambda *a, **k: outcome)

    q: "queue.Queue[tuple]" = queue.Queue()
    w = tasks.SyncWorker(Config(), q)
    w.start()
    w.join(timeout=5)

    assert w.is_alive() is False          # must not raise
    assert q.get_nowait() == ("sync_done", outcome)


def test_sync_worker_stop_sets_cancel(monkeypatch):
    seen = {}
    monkeypatch.setattr(tasks, "run_sync",
                        lambda *a, **k: seen.update(should_stop=k["should_stop"]))
    w = tasks.SyncWorker(Config(), queue.Queue())
    w.stop()
    w.run()
    assert seen["should_stop"]() is True   # cancel event is set
