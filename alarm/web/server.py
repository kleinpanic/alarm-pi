"""
Flask app: LAN web UI + JSON API for the alarm daemon.

All control actions (dismiss/snooze/test) delegate to the daemon. Config CRUD
goes straight to the shared JSON via the config module (the daemon hot-reloads).
A PIN, when set, gates non-local browsers; 127.0.0.1 always bypasses it so the
daemon-spawned popup and the local CLI never need it.
"""

import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, session, abort,
)

from .. import config
from ..models import Alarm
from ..scheduler import AlarmScheduler
from ..dayutil import parse_days, format_days, validate_time

logger = logging.getLogger(__name__)
LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def create_app(daemon) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.jinja_env.filters["fmtdays"] = format_days

    # ---- PIN gate ----

    def _pin_required() -> bool:
        pin = daemon.settings.web_pin
        if not pin:
            return False
        if request.remote_addr in LOCAL_ADDRS:
            return False
        return not session.get("authed")

    @app.before_request
    def _gate():
        if request.endpoint in ("login", "static"):
            return None
        if _pin_required():
            if request.path.startswith("/api/"):
                return jsonify(error="PIN required"), 401
            return redirect(url_for("login", next=request.path))
        return None

    # ---- shared helpers ----

    def _status_payload() -> dict:
        alarms = config.load_alarms()
        snap = daemon.state.snapshot()
        nxt = AlarmScheduler().get_next_alarms(alarms, count=1)
        next_alarm = None
        if nxt:
            a, dt = nxt[0]
            next_alarm = {"id": a.id, "label": a.label, "time": a.time,
                          "when": dt.isoformat(), "when_human": dt.strftime("%a %H:%M")}
        return {
            "now": datetime.now().strftime("%H:%M:%S"),
            "ringing": snap["ringing"],
            "next": next_alarm,
            "alarm_count": len(alarms),
            "enabled_count": sum(1 for a in alarms if a.enabled),
        }

    def _apply(payload: dict, base: Optional[Alarm]) -> Alarm:
        """Build or update an Alarm from a normalized payload. Raises ValueError."""
        a = base or Alarm(id=config.generate_alarm_id(), label="", time="07:00", days_of_week=[])
        if "label" in payload:
            a.label = (payload["label"] or "").strip() or "Alarm"
        if payload.get("time") is not None:
            a.time = validate_time(payload["time"])
        if payload.get("days") is not None:
            days = payload["days"]
            a.days_of_week = parse_days(days) if isinstance(days, str) else sorted({int(d) for d in days})
            if not a.days_of_week:
                raise ValueError("Select at least one day")
        for key, attr in (("enabled", "enabled"), ("snoozable", "snoozable"), ("irritable", "irritable")):
            if key in payload and payload[key] is not None:
                setattr(a, attr, _truthy(payload[key]))
        if payload.get("sound") is not None:
            a.sound_path = payload["sound"] or None
        if payload.get("volume") is not None:
            a.base_volume = int(payload["volume"])
        if payload.get("irritable_duration") is not None:
            a.irritable_duration_minutes = int(payload["irritable_duration"])
        if payload.get("irritable_step") is not None:
            a.irritable_volume_step = int(payload["irritable_step"])
        return a

    def _form_payload(form) -> dict:
        # Unchecked checkboxes are absent from the POST body → treated as false.
        return {
            "label": form.get("label"),
            "time": form.get("time"),
            "days": form.getlist("days") or [],
            "enabled": "on" if form.get("enabled") else "",
            "snoozable": "on" if form.get("snoozable") else "",
            "irritable": "on" if form.get("irritable") else "",
            "sound": form.get("sound", ""),
            "volume": form.get("volume") or 70,
            "irritable_duration": form.get("irritable_duration") or 5,
            "irritable_step": form.get("irritable_step") or 10,
        }

    # ---- pages ----

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", status=_status_payload())

    @app.route("/alarms")
    def alarms_page():
        return render_template("alarms.html", alarms=config.load_alarms())

    @app.route("/alarms/new", methods=["GET", "POST"])
    def alarm_new():
        if request.method == "POST":
            try:
                config.add_alarm(_apply(_form_payload(request.form), None))
                return redirect(url_for("alarms_page"))
            except ValueError as e:
                return render_template("form.html", title="New alarm", alarm=None,
                                       selected_days=[int(d) for d in request.form.getlist("days")],
                                       error=str(e)), 400
        return render_template("form.html", title="New alarm", alarm=None,
                               selected_days=[0, 1, 2, 3, 4], error=None)

    @app.route("/alarms/<aid>/edit", methods=["GET", "POST"])
    def alarm_edit(aid):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            abort(404)
        if request.method == "POST":
            try:
                config.update_alarm(_apply(_form_payload(request.form), alarm))
                return redirect(url_for("alarms_page"))
            except ValueError as e:
                return render_template("form.html", title="Edit alarm", alarm=alarm,
                                       selected_days=alarm.days_of_week, error=str(e)), 400
        return render_template("form.html", title="Edit alarm", alarm=alarm,
                               selected_days=alarm.days_of_week, error=None)

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        s = config.load_settings()
        if request.method == "POST":
            s.default_snooze_minutes = int(request.form.get("snooze") or s.default_snooze_minutes)
            s.web_port = int(request.form.get("port") or s.web_port)
            s.web_pin = request.form.get("pin") or None
            s.default_sound = request.form.get("sound") or s.default_sound
            config.save_settings(s)
            daemon.settings = s
            return redirect(url_for("settings_page"))
        return render_template("settings.html", s=s)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if request.form.get("pin") == daemon.settings.web_pin:
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("dashboard"))
            return render_template("login.html", error="Wrong PIN"), 401
        return render_template("login.html", error=None)

    # ---- JSON API ----

    @app.get("/api/status")
    def api_status():
        return jsonify(_status_payload())

    @app.get("/api/alarms")
    def api_list():
        return jsonify([a.to_dict() for a in config.load_alarms()])

    @app.post("/api/alarms")
    def api_create():
        try:
            a = _apply(request.get_json(force=True, silent=True) or {}, None)
        except (ValueError, TypeError) as e:
            return jsonify(error=str(e)), 400
        config.add_alarm(a)
        return jsonify(a.to_dict()), 201

    @app.route("/api/alarms/<aid>", methods=["PUT", "PATCH"])
    def api_update(aid):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            return jsonify(error="not found"), 404
        try:
            updated = _apply(request.get_json(force=True, silent=True) or {}, alarm)
        except (ValueError, TypeError) as e:
            return jsonify(error=str(e)), 400
        config.update_alarm(updated)
        return jsonify(updated.to_dict())

    @app.delete("/api/alarms/<aid>")
    def api_delete(aid):
        if config.delete_alarm(aid):
            return jsonify(ok=True)
        return jsonify(error="not found"), 404

    def _set_enabled(aid, value):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            return jsonify(error="not found"), 404
        alarm.enabled = value
        config.update_alarm(alarm)
        return jsonify(alarm.to_dict())

    @app.post("/api/alarms/<aid>/enable")
    def api_enable(aid):
        return _set_enabled(aid, True)

    @app.post("/api/alarms/<aid>/disable")
    def api_disable(aid):
        return _set_enabled(aid, False)

    @app.post("/api/alarms/<aid>/skip")
    def api_skip(aid):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            return jsonify(error="not found"), 404
        body = request.get_json(force=True, silent=True) or {}
        date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        if date == "tomorrow":
            date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        if date not in alarm.skip_dates:
            alarm.skip_dates.append(date)
            config.update_alarm(alarm)
        return jsonify(alarm.to_dict())

    @app.post("/api/alarms/<aid>/test")
    def api_test(aid):
        return (jsonify(ok=True) if daemon.test(aid) else (jsonify(error="not found or already ringing"), 409))

    @app.post("/api/ringing/dismiss")
    def api_dismiss():
        return jsonify(dismissed=daemon.dismiss())

    @app.post("/api/ringing/snooze")
    def api_snooze():
        body = request.get_json(force=True, silent=True) or {}
        minutes = body.get("minutes")
        return jsonify(snoozed=daemon.snooze(int(minutes) if minutes else None))

    @app.get("/api/settings")
    def api_settings():
        return jsonify(config.load_settings().to_dict())

    return app


def run_server(daemon) -> None:
    """Run the Flask server (blocking; called in the daemon's web thread)."""
    app = create_app(daemon)
    app.run(host=daemon.settings.web_host, port=daemon.settings.web_port,
            threaded=True, use_reloader=False, debug=False)
