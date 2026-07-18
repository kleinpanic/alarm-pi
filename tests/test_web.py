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
    for path in ("/", "/alarms", "/runtime", "/system", "/settings", "/diagnostics", "/alarms/new"):
        assert client.get(path).status_code == 200


def test_pages_use_packaged_assets_without_runtime_cdn(client):
    for path in ("/", "/login"):
        html = client.get(path).get_data(as_text=True)
        assert 'href="/static/app.css"' in html
        assert "cdn.tailwindcss.com" not in html
        assert "http://" not in html and "https://" not in html
    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert len(css.data) > 10_000


def test_operational_views_expose_state_without_service_stop(client):
    status = client.get("/api/status").get_json()
    assert set(status["readiness"]) == {"scheduler", "web", "config", "audio"}
    assert isinstance(status["upcoming"], list)
    assert client.get("/api/runtime").get_json()["queue"] == []
    system_html = client.get("/system").get_data(as_text=True)
    assert 'data-post="/api/service/stop"' not in system_html.lower()
    assert "alarm service restart" in system_html


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

    r = client.delete(f"/api/alarms/{aid}/skip/2026-12-25")
    assert "2026-12-25" not in r.get_json()["skip_dates"]

    r = client.post(f"/api/alarms/{aid}/duplicate")
    assert r.status_code == 201
    assert r.get_json()["label"] == "Wake copy"
    assert r.get_json()["enabled"] is False

    backup = client.get("/api/export")
    assert backup.status_code == 200
    assert backup.headers["Content-Disposition"].startswith("attachment;")
    assert backup.get_json()["schema_version"] == 1

    assert client.delete(f"/api/alarms/{aid}").status_code == 200
    assert client.delete(f"/api/alarms/{aid}").status_code == 404


def test_api_create_validation(client):
    assert client.post("/api/alarms", json={"label": "x", "time": "99:99", "days": [0]}).status_code == 400
    assert client.post("/api/alarms", json={"label": "x", "time": "07:00", "days": []}).status_code == 400


def test_json_validation_is_strict_complete_and_ordered(client):
    response = client.post(
        "/api/alarms",
        json={
            "label": " ",
            "time": "99:99",
            "days": [True],
            "enabled": 1,
            "volume": "70",
            "surprise": True,
        },
    )
    assert response.status_code == 400
    assert response.get_json() == {
        "error": "validation_failed",
        "issues": [
            {"field": "label", "code": "empty", "message": "must not be empty"},
            {"field": "time", "code": "format", "message": "must use HH:MM in 24-hour time"},
            {"field": "days_of_week[0]", "code": "type", "message": "must be an integer"},
            {"field": "enabled", "code": "type", "message": "must be a boolean"},
            {"field": "base_volume", "code": "type", "message": "must be an integer"},
            {"field": "surprise", "code": "unknown", "message": "is not a recognized field"},
        ],
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data": "", "content_type": "application/json"},
        {"data": "[]", "content_type": "application/json"},
        {"data": "null", "content_type": "application/json"},
    ],
)
def test_json_rejects_empty_or_non_object_bodies(client, kwargs):
    response = client.post("/api/alarms", **kwargs)
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "validation_failed"
    assert body["issues"][0]["field"] == "$"


def test_alarm_form_shows_all_errors_and_retains_every_submitted_value(client):
    response = client.post(
        "/alarms/new",
        data={
            "label": " ", "time": "25:99", "days": ["1", "6"],
            "enabled": "on", "irritable": "on", "volume": "101",
            "irritable_duration": "0", "irritable_step": "0",
            "sound": "/missing/alarm.wav",
        },
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'id="validation-summary"' in html
    for field_id in ("alarm-label", "alarm-time", "alarm-volume", "alarm-duration", "alarm-step", "alarm-sound"):
        assert f'id="{field_id}"' in html
        assert f'aria-describedby="{field_id}-error"' in html
        assert f'id="{field_id}-error"' in html
    assert 'value="25:99"' in html
    assert 'value="101"' in html
    assert 'value="/missing/alarm.wav"' in html
    assert 'name="days" value="1" class="peer sr-only" checked' in html
    assert 'name="days" value="6" class="peer sr-only" checked' in html
    assert 'name="enabled"' in html and 'name="irritable"' in html


def test_alarm_form_unchecked_boxes_store_false(client):
    r = client.post("/alarms/new", data={
        "label": "Boxes", "time": "07:00", "days": ["0"], "volume": "70",
        "irritable_duration": "5", "irritable_step": "10", "sound": "",
    })
    assert r.status_code == 302
    created = next(a for a in client.get("/api/alarms").get_json() if a["label"] == "Boxes")
    assert created["enabled"] is False
    assert created["snoozable"] is False
    assert created["irritable"] is False

    r = client.post(f"/alarms/{created['id']}/edit", data={
        "label": "Boxes", "time": "07:00", "days": ["0"], "volume": "70",
        "irritable_duration": "5", "irritable_step": "10", "sound": "",
        "enabled": "on",
    })
    assert r.status_code == 302
    edited = next(a for a in client.get("/api/alarms").get_json() if a["id"] == created["id"])
    assert edited["enabled"] is True
    assert edited["snoozable"] is False


def test_settings_form_shows_all_errors_and_retains_submitted_values(client):
    response = client.post(
        "/settings",
        data={"snooze": "0", "port": "70000", "pin": " 4242 ", "sound": "/missing/default.wav"},
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'id="validation-summary"' in html
    for field_id in ("settings-snooze", "settings-port", "settings-sound"):
        assert f'id="{field_id}"' in html
        assert f'aria-describedby="{field_id}-error"' in html
        assert f'id="{field_id}-error"' in html
    assert 'value="0"' in html
    assert 'value="70000"' in html
    assert 'value=" 4242 "' in html
    assert 'value="/missing/default.wav"' in html


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


def test_snooze_json_rejects_coercion_and_unknown_fields(client):
    for body, fields in (({"minutes": "5"}, ["minutes"]), ({"minutes": True, "extra": 1}, ["minutes", "extra"])):
        response = client.post("/api/ringing/snooze", json=body)
        assert response.status_code == 400
        assert [issue["field"] for issue in response.get_json()["issues"]] == fields


def test_pin_gates_remote_but_not_local(client):
    client.daemon.settings.web_pin = "4242"
    remote = {"REMOTE_ADDR": "10.0.0.42"}
    assert client.get("/api/status", environ_overrides=remote).status_code == 401
    # localhost (test client default 127.0.0.1) bypasses
    assert client.get("/api/status").status_code == 200
