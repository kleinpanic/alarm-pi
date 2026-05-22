import threading

import alarm.web.server as srv
from alarm.daemon_core import AlarmDaemon


def test_web_disabled_starts_no_thread():
    d = AlarmDaemon()
    d.settings.web_enabled = False
    d._start_web()
    assert d._web_thread is None


def test_web_enabled_starts_isolated_thread_and_is_idempotent(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def fake_run(daemon):
        started.set()
        release.wait(2)

    monkeypatch.setattr(srv, "run_server", fake_run)
    d = AlarmDaemon()
    d.settings.web_enabled = True

    d._start_web()
    assert started.wait(2)
    assert d._web_thread.is_alive()
    assert d._web_thread.daemon  # isolated background thread

    first = d._web_thread
    d._start_web()  # idempotent: no second thread
    assert d._web_thread is first

    release.set()


def test_web_failure_is_contained(monkeypatch):
    blew_up = threading.Event()

    def boom(daemon):
        blew_up.set()
        raise RuntimeError("port already in use")

    monkeypatch.setattr(srv, "run_server", boom)
    d = AlarmDaemon()
    d.settings.web_enabled = True

    d._start_web()
    assert blew_up.wait(2)
    d._web_thread.join(2)
    assert not d._web_thread.is_alive()  # exception stayed in the thread
