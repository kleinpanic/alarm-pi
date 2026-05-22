import pytest

from alarm.daemon_core import AlarmDaemon
from alarm.web.server import create_app


@pytest.fixture
def client():
    daemon = AlarmDaemon()  # sandbox config; no loop/web/audio started
    app = create_app(daemon)
    app.config.update(TESTING=True)
    c = app.test_client()
    c.daemon = daemon
    return c


def test_pages_render(client):
    for path in ("/", "/alarms", "/settings", "/alarms/new"):
        assert client.get(path).status_code == 200


def test_api_crud_cycle(client):
    r = client.post("/api/alarms", json={"label": "Wake", "time": "07:00", "days": [0, 1, 2]})
    assert r.status_code == 201
    aid = r.get_json()["id"]

    assert any(a["id"] == aid for a in client.get("/api/alarms").get_json())

    r = client.put(f"/api/alarms/{aid}", json={"time": "08:15"})
    assert r.status_code == 200 and r.get_json()["time"] == "08:15"

    assert client.post(f"/api/alarms/{aid}/disable").get_json()["enabled"] is False
    assert client.post(f"/api/alarms/{aid}/enable").get_json()["enabled"] is True

    r = client.post(f"/api/alarms/{aid}/skip", json={"date": "2026-12-25"})
    assert "2026-12-25" in r.get_json()["skip_dates"]

    assert client.delete(f"/api/alarms/{aid}").status_code == 200
    assert client.delete(f"/api/alarms/{aid}").status_code == 404


def test_api_create_validation(client):
    assert client.post("/api/alarms", json={"label": "x", "time": "99:99", "days": [0]}).status_code == 400
    assert client.post("/api/alarms", json={"label": "x", "time": "07:00", "days": []}).status_code == 400


def test_ringing_lifecycle(client):
    r = client.post("/api/alarms", json={"label": "Test", "time": "07:00", "days": [0]})
    aid = r.get_json()["id"]
    # nothing ringing yet
    assert client.get("/api/status").get_json()["ringing"] is None
    assert client.post("/api/ringing/dismiss").get_json()["dismissed"] is False
    # fire it
    assert client.post(f"/api/alarms/{aid}/test").status_code == 200
    ring = client.get("/api/status").get_json()["ringing"]
    assert ring and ring["alarm_id"] == aid
    # dismiss
    assert client.post("/api/ringing/dismiss").get_json()["dismissed"] is True
    assert client.get("/api/status").get_json()["ringing"] is None


def test_pin_gates_remote_but_not_local(client):
    client.daemon.settings.web_pin = "4242"
    remote = {"REMOTE_ADDR": "10.0.0.42"}
    assert client.get("/api/status", environ_overrides=remote).status_code == 401
    # localhost (test client default 127.0.0.1) bypasses
    assert client.get("/api/status").status_code == 200
