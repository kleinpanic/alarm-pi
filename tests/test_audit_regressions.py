"""Regression tests for the security/correctness audit fixes."""

import io
import json
import urllib.error

import pytest

from alarm.daemon_core import AlarmDaemon
from alarm.web.server import create_app

REMOTE = {"REMOTE_ADDR": "10.0.0.42"}


@pytest.fixture
def client():
    daemon = AlarmDaemon()
    app = create_app(daemon)
    app.config.update(TESTING=True)
    c = app.test_client()
    c.daemon = daemon
    return c


def _create(client):
    r = client.post("/api/alarms", json={"label": "Wake", "time": "07:00", "days": [0]})
    assert r.status_code == 201
    return r.get_json()["id"]


# ---- web: login redirect, CSRF, PIN session, brute force ----

@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_login_next_cannot_redirect_off_site(client, target):
    client.daemon.settings.web_pin = "4242"
    r = client.post("/login", query_string={"next": target}, data={"pin": "4242"},
                    environ_overrides=REMOTE)
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_login_next_keeps_local_path(client):
    client.daemon.settings.web_pin = "4242"
    r = client.post("/login?next=/alarms", data={"pin": "4242"}, environ_overrides=REMOTE)
    assert r.headers["Location"] == "/alarms"


def test_cross_origin_writes_are_refused(client):
    aid = _create(client)
    evil = {"Origin": "https://evil.example"}
    r = client.post(f"/api/alarms/{aid}/skip", data='{"date": "2030-01-01"}',
                    content_type="text/plain", headers=evil)
    assert r.status_code == 403
    assert client.post("/api/ringing/dismiss", headers=evil).status_code == 403
    r = client.post("/settings", data={"snooze": "5", "port": "8765", "pin": "1"},
                    headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403
    assert client.daemon.repository.snapshot().alarms[0].skip_dates == []


def test_same_origin_and_headerless_writes_still_work(client):
    aid = _create(client)
    r = client.post(f"/api/alarms/{aid}/skip", json={"date": "2030-01-01"},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 200
    # CLI / popup clients send no Origin or Referer.
    assert client.post("/api/ringing/dismiss").status_code == 200


def test_changing_pin_revokes_existing_sessions(client):
    client.daemon.settings.web_pin = "4242"
    client.post("/login", data={"pin": "4242"}, environ_overrides=REMOTE)
    assert client.get("/api/status", environ_overrides=REMOTE).status_code == 200
    client.daemon.settings.web_pin = "9999"
    assert client.get("/api/status", environ_overrides=REMOTE).status_code == 401


def test_login_locks_out_after_repeated_failures(client):
    client.daemon.settings.web_pin = "4242"
    for _ in range(5):
        r = client.post("/login", data={"pin": "0000"}, environ_overrides=REMOTE)
        assert r.status_code == 401
    r = client.post("/login", data={"pin": "4242"}, environ_overrides=REMOTE)
    assert r.status_code == 429
    assert client.get("/api/status", environ_overrides=REMOTE).status_code == 401


def test_skip_rejects_non_object_body_instead_of_crashing(client):
    aid = _create(client)
    r = client.post(f"/api/alarms/{aid}/skip", json=[1])
    assert r.status_code == 400
    assert r.get_json()["issues"][0]["field"] == "$"


# ---- validation: relative sound paths ----

def test_relative_sound_path_resolves_against_project_root(tmp_path, monkeypatch):
    from alarm import config
    from alarm.validation import validate_settings_payload

    monkeypatch.chdir(tmp_path)  # anywhere but the project root
    normalized = validate_settings_payload(
        {"default_sound": "sounds/new-alarm-tone.mp3"}, partial=True
    )
    assert normalized["default_sound"] == str(
        (config.PROJECT_ROOT / "sounds" / "new-alarm-tone.mp3").resolve()
    )


def test_example_settings_migrate_from_any_working_directory(tmp_path, monkeypatch):
    from pathlib import Path
    from alarm import config

    example = Path(__file__).resolve().parent.parent / "config" / "settings.example.json"
    config.SETTINGS_FILE.write_text(example.read_text())
    monkeypatch.chdir(tmp_path)
    settings = config.get_repository().load().settings
    assert settings.default_sound.endswith("sounds/new-alarm-tone.mp3")


# ---- CLI: HTTP error statuses are results, not "daemon down" ----

def test_cli_api_returns_error_body_for_http_error(monkeypatch, capsys):
    from alarm import cli

    def fake_urlopen(req, timeout):
        body = io.BytesIO(json.dumps({"error": "not found or already ringing"}).encode())
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, body)

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)
    assert cli.main(["test", "99"]) == 0
    out = capsys.readouterr().out
    assert "already ringing" in out
    assert "Cannot reach daemon" not in out


# ---- popup: labels beginning with "-" ----

def test_popup_argv_survives_dash_prefixed_label(monkeypatch):
    from alarm import daemon_core, popup
    from alarm.models import Alarm

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 0

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(daemon_core.subprocess, "Popen", FakePopen)
    daemon = AlarmDaemon(audio=object())
    alarm = Alarm(id="1", label="--irritable", time="07:00", days_of_week=[0])
    daemon._spawn_popup(alarm)
    daemon._popup_proc = None

    argv = captured["cmd"][3:]  # drop python -m alarm.popup
    seen = {}
    monkeypatch.setattr(popup, "run_popup", lambda *a: seen.setdefault("args", a))
    popup.main(argv)
    port, label, time_str, snoozable, minutes, irritable = seen["args"]
    assert label == "--irritable"
    assert irritable is False


# ---- install: unit is rewritten for this checkout ----

def test_install_points_unit_at_this_project(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from alarm import cli

    repo_unit = Path(__file__).resolve().parent.parent / "systemd" / cli.SERVICE
    (cli.PROJECT_ROOT / "systemd").mkdir(exist_ok=True)
    shutil.copy(repo_unit, cli.PROJECT_ROOT / "systemd" / cli.SERVICE)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_systemctl", lambda *a: 0)

    assert cli.main(["install"]) == 0
    unit = (tmp_path / ".config" / "systemd" / "user" / cli.SERVICE).read_text()
    assert "/home/klein" not in unit
    assert f"WorkingDirectory={cli.PROJECT_ROOT}" in unit
    assert f"ExecStart={cli.sys.executable} -m alarm serve" in unit
