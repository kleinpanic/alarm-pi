"""
Flask app: LAN web UI + JSON API for the alarm daemon.

All control actions (dismiss/snooze/test) delegate to the daemon. Config CRUD
goes straight to the shared JSON via the config module (the daemon hot-reloads).
A PIN, when set, gates non-local browsers; 127.0.0.1 always bypasses it so the
daemon-spawned popup and the local CLI never need it.
"""

import logging
import shutil
import secrets
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, session, abort,
)

from .. import config
from ..models import Alarm, Settings
from ..scheduler import AlarmScheduler
from ..dayutil import format_days
from ..validation import FieldIssue, ValidationError, validate_alarm_payload, validate_settings_payload

logger = logging.getLogger(__name__)
LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}


ALARM_ALIASES = {
    "days": "days_of_week", "sound": "sound_path", "volume": "base_volume",
    "irritable_duration": "irritable_duration_minutes",
    "irritable_step": "irritable_volume_step",
}


def _issue_data(error: ValidationError) -> list[dict[str, str]]:
    return [
        {"field": issue.field, "code": issue.code, "message": issue.message}
        for issue in error.issues
    ]


def _form_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _alarm_defaults() -> dict[str, Any]:
    return Alarm(id="pending", label="", time="07:00", days_of_week=[]).to_dict()


def _canonical_alarm(payload: Any, *, base: Optional[Alarm] = None, form: bool = False) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        validate_alarm_payload(payload, partial=True)
    raw = dict(payload)
    canonical: dict[str, Any] = {}
    for key, value in raw.items():
        canonical[ALARM_ALIASES.get(key, key)] = value
    if form:
        canonical["days_of_week"] = [_form_int(item) for item in raw.get("days", [])]
        for name in ("enabled", "snoozable", "irritable"):
            canonical[name] = bool(raw.get(name))
        for name in ("volume", "irritable_duration", "irritable_step"):
            canonical[ALARM_ALIASES[name]] = _form_int(raw.get(name))
        canonical["sound_path"] = raw.get("sound") or None
    merged = (base.to_dict() if base is not None else _alarm_defaults())
    merged.update(canonical)
    if base is None:
        merged["id"] = "pending"
    return merged


def _canonical_alarm_patch(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        validate_alarm_payload(payload, partial=True)
    return {ALARM_ALIASES.get(key, key): value for key, value in dict(payload).items()}


def _settings_form_payload(form, current: Settings) -> dict[str, Any]:
    payload = current.to_dict()
    payload.update({
        "default_snooze_minutes": _form_int(form.get("snooze")),
        "web_port": _form_int(form.get("port")),
        "web_pin": form.get("pin") or None,
        "default_sound": form.get("sound"),
    })
    return payload


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
        document = daemon.repository.snapshot()
        alarms = document.alarms
        snap = daemon.state.snapshot()
        upcoming = []
        for alarm, due_at in AlarmScheduler().get_next_alarms(alarms, count=5):
            upcoming.append({
                "id": alarm.id,
                "label": alarm.label,
                "time": alarm.time,
                "when": due_at.isoformat(),
                "when_human": due_at.strftime("%a %H:%M"),
                "days": format_days(alarm.days_of_week),
            })
        runtime = document.runtime.to_dict()
        audio_available = shutil.which(document.settings.audio_player) is not None
        readiness = {
            "scheduler": {
                "ok": runtime["scheduler_checkpoint"] is not None,
                "label": "Scheduling active" if runtime["scheduler_checkpoint"] else "Awaiting first scheduler check",
            },
            "web": {"ok": True, "label": "Web control available"},
            "config": {"ok": True, "label": "Configuration loaded"},
            "audio": {
                "ok": audio_available,
                "label": f"{document.settings.audio_player} available" if audio_available else f"{document.settings.audio_player} not found",
            },
        }
        return {
            "now": datetime.now().strftime("%H:%M:%S"),
            "ringing": snap["ringing"],
            "next": upcoming[0] if upcoming else None,
            "upcoming": upcoming,
            "alarm_count": len(alarms),
            "enabled_count": sum(1 for a in alarms if a.enabled),
            "queue_count": len(runtime["queue"]),
            "snooze_count": len(runtime["snoozes"]),
            "diagnostic_count": len(runtime["diagnostics"]),
            "scheduler_checkpoint": runtime["scheduler_checkpoint"],
            "readiness": readiness,
        }

    def _runtime_payload() -> dict[str, Any]:
        document = daemon.repository.snapshot()
        labels = {alarm.id: alarm.label for alarm in document.alarms}
        runtime = document.runtime.to_dict()
        for collection in (runtime["queue"], runtime["snoozes"]):
            for item in collection:
                item.setdefault("label", labels.get(item.get("alarm_id"), "Unknown alarm"))
        return runtime

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

    def _render_alarm_form(title, alarm, submitted, error):
        issues = error.issues if error else ()
        field_issues = {}
        aliases = {value: key for key, value in ALARM_ALIASES.items()}
        for issue in issues:
            field = issue.field.split("[", 1)[0]
            field_issues.setdefault(aliases.get(field, field), []).append(issue)
        values = _form_payload(submitted) if submitted is not None else None
        selected = [_form_int(item) for item in submitted.getlist("days")] if submitted is not None else (alarm.days_of_week if alarm else [0, 1, 2, 3, 4])
        return render_template("form.html", title=title, alarm=alarm, values=values,
                               selected_days=selected, issues=issues, field_issues=field_issues)

    def _update_alarm_command(aid: str, changes: dict[str, Any]) -> Optional[Alarm]:
        updated = None
        def mutate(document):
            nonlocal updated
            for index, current in enumerate(document.alarms):
                if current.id == aid:
                    payload = current.to_dict()
                    payload.update(changes)
                    updated = Alarm.from_payload(payload)
                    document.alarms[index] = updated
                    return
        daemon.repository.transaction(mutate)
        return updated

    def _update_settings_command(changes: dict[str, Any]) -> Settings:
        updated = None
        def mutate(document):
            nonlocal updated
            payload = document.settings.to_dict()
            payload.update(changes)
            updated = Settings.from_payload(payload)
            document.settings = updated
        daemon.repository.transaction(mutate)
        return updated

    def _add_skip_command(aid: str, date: str) -> Optional[Alarm]:
        updated = None
        def mutate(document):
            nonlocal updated
            for index, current in enumerate(document.alarms):
                if current.id == aid:
                    dates = sorted(set(current.skip_dates + [date]))
                    changes = validate_alarm_payload({"skip_dates": dates}, partial=True)
                    payload = current.to_dict()
                    payload.update(changes)
                    updated = Alarm.from_payload(payload)
                    document.alarms[index] = updated
                    return
        daemon.repository.transaction(mutate)
        return updated

    # ---- pages ----

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", status=_status_payload())

    @app.route("/alarms")
    def alarms_page():
        return render_template("alarms.html", alarms=config.load_alarms())

    @app.route("/runtime")
    def runtime_page():
        return render_template("runtime.html", runtime=_runtime_payload())

    @app.route("/system")
    def system_page():
        status = _status_payload()
        return render_template(
            "system.html",
            status=status,
            settings=daemon.repository.snapshot().settings,
            process_running=bool(getattr(daemon, "_running", False)),
        )

    @app.route("/diagnostics")
    def diagnostics_page():
        runtime = _runtime_payload()
        return render_template(
            "diagnostics.html",
            diagnostics=list(reversed(runtime["diagnostics"][-50:])),
            status=_status_payload(),
        )

    @app.route("/alarms/new", methods=["GET", "POST"])
    def alarm_new():
        if request.method == "POST":
            try:
                normalized = validate_alarm_payload(_canonical_alarm(_form_payload(request.form), form=True))
                created = Alarm.from_payload(normalized)
                created.id = "pending"
                daemon.repository.create_alarm(created)
                return redirect(url_for("alarms_page"))
            except ValidationError as e:
                return _render_alarm_form("New alarm", None, request.form, e), 400
        return _render_alarm_form("New alarm", None, None, None)

    @app.route("/alarms/<aid>/edit", methods=["GET", "POST"])
    def alarm_edit(aid):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            abort(404)
        if request.method == "POST":
            try:
                submitted = _form_payload(request.form)
                complete = _canonical_alarm(submitted, base=alarm, form=True)
                validate_alarm_payload(complete)
                form_values = _canonical_alarm(submitted, form=True)
                editable = (
                    "label", "time", "days_of_week", "enabled", "snoozable",
                    "irritable", "sound_path", "base_volume",
                    "irritable_duration_minutes", "irritable_volume_step",
                )
                normalized = validate_alarm_payload(
                    {key: form_values[key] for key in editable}, partial=True
                )
                _update_alarm_command(aid, normalized)
                return redirect(url_for("alarms_page"))
            except ValidationError as e:
                return _render_alarm_form("Edit alarm", alarm, request.form, e), 400
        return _render_alarm_form("Edit alarm", alarm, None, None)

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        s = config.load_settings()
        if request.method == "POST":
            try:
                complete = _settings_form_payload(request.form, s)
                validate_settings_payload(complete)
                changes = {
                    "default_snooze_minutes": complete["default_snooze_minutes"],
                    "web_port": complete["web_port"],
                    "web_pin": complete["web_pin"],
                    "default_sound": complete["default_sound"],
                }
                normalized = validate_settings_payload(changes, partial=True)
                updated = _update_settings_command(normalized)
                daemon.settings = updated
                return redirect(url_for("settings_page"))
            except ValidationError as e:
                by_field = {}
                for issue in e.issues:
                    by_field.setdefault(issue.field, []).append(issue)
                return render_template("settings.html", s=s, values=request.form,
                                       issues=e.issues, field_issues=by_field), 400
        return render_template("settings.html", s=s, values=None, issues=(), field_issues={})

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
            body = request.get_json(silent=True)
            if body is None:
                body = None
            normalized = validate_alarm_payload(_canonical_alarm(body))
            created = daemon.repository.create_alarm(Alarm.from_payload(normalized))
        except ValidationError as e:
            return jsonify(error="validation_failed", issues=_issue_data(e)), 400
        return jsonify(created.to_dict()), 201

    @app.route("/api/alarms/<aid>", methods=["PUT", "PATCH"])
    def api_update(aid):
        alarm = config.get_alarm_by_id(aid)
        if not alarm:
            return jsonify(error="not found"), 404
        try:
            body = request.get_json(silent=True)
            changes = validate_alarm_payload(_canonical_alarm_patch(body), partial=True)
            updated = _update_alarm_command(aid, changes)
        except ValidationError as e:
            return jsonify(error="validation_failed", issues=_issue_data(e)), 400
        return jsonify(updated.to_dict())

    @app.delete("/api/alarms/<aid>")
    def api_delete(aid):
        if daemon.delete_alarm(aid):
            return jsonify(ok=True)
        return jsonify(error="not found"), 404

    def _set_enabled(aid, value):
        alarm = next((item for item in daemon.repository.snapshot().alarms if item.id == aid), None)
        if not alarm:
            return jsonify(error="not found"), 404
        if value:
            _update_alarm_command(aid, {"enabled": True})
        else:
            daemon.disable_alarm(aid)
        current = next(item for item in daemon.repository.snapshot().alarms if item.id == aid)
        return jsonify(current.to_dict())

    @app.post("/api/alarms/<aid>/enable")
    def api_enable(aid):
        return _set_enabled(aid, True)

    @app.post("/api/alarms/<aid>/disable")
    def api_disable(aid):
        return _set_enabled(aid, False)

    @app.post("/api/alarms/<aid>/skip")
    def api_skip(aid):
        alarm = next((item for item in daemon.repository.snapshot().alarms if item.id == aid), None)
        if not alarm:
            return jsonify(error="not found"), 404
        body = request.get_json(force=True, silent=True) or {}
        date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
        if date == "tomorrow":
            date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            updated = _add_skip_command(aid, date)
        except ValidationError as e:
            return jsonify(error="validation_failed", issues=_issue_data(e)), 400
        return jsonify(updated.to_dict())

    @app.post("/api/alarms/<aid>/test")
    def api_test(aid):
        return (jsonify(ok=True) if daemon.test(aid) else (jsonify(error="not found or already ringing"), 409))

    @app.post("/api/alarms/<aid>/duplicate")
    def api_duplicate(aid):
        alarm = next((item for item in daemon.repository.snapshot().alarms if item.id == aid), None)
        if not alarm:
            return jsonify(error="not found"), 404
        duplicate = alarm.to_dict()
        duplicate.update(id="pending", label=f"{alarm.label} copy", enabled=False, skip_dates=[])
        created = daemon.repository.create_alarm(Alarm.from_payload(duplicate))
        return jsonify(created.to_dict()), 201

    @app.delete("/api/alarms/<aid>/skip/<date>")
    def api_remove_skip(aid, date):
        alarm = next((item for item in daemon.repository.snapshot().alarms if item.id == aid), None)
        if not alarm:
            return jsonify(error="not found"), 404
        try:
            changes = validate_alarm_payload(
                {"skip_dates": [item for item in alarm.skip_dates if item != date]}, partial=True
            )
            updated = _update_alarm_command(aid, changes)
        except ValidationError as e:
            return jsonify(error="validation_failed", issues=_issue_data(e)), 400
        return jsonify(updated.to_dict())

    @app.post("/api/ringing/dismiss")
    def api_dismiss():
        return jsonify(dismissed=daemon.dismiss())

    @app.post("/api/ringing/snooze")
    def api_snooze():
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, Mapping):
            error = ValidationError((FieldIssue("$", "type", "must be an object"),))
            return jsonify(error="validation_failed", issues=_issue_data(error)), 400
        unknown = sorted(set(body) - {"minutes"})
        issues = [FieldIssue(key, "unknown", "is not a recognized field") for key in unknown]
        minutes = body.get("minutes")
        if minutes is not None and type(minutes) is not int:
            issues.insert(0, FieldIssue("minutes", "type", "must be an integer"))
        if issues:
            return jsonify(error="validation_failed", issues=_issue_data(ValidationError(issues))), 400
        return jsonify(snoozed=daemon.snooze(minutes))

    @app.get("/api/settings")
    def api_settings():
        return jsonify(config.load_settings().to_dict())

    @app.get("/api/runtime")
    def api_runtime():
        return jsonify(_runtime_payload())

    @app.get("/api/export")
    def api_export():
        response = jsonify(daemon.repository.snapshot().to_dict())
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        response.headers["Content-Disposition"] = f'attachment; filename="alarmpi-backup-{stamp}.json"'
        return response

    return app


def run_server(daemon) -> None:
    """Run the Flask server (blocking; called in the daemon's web thread)."""
    app = create_app(daemon)
    app.run(host=daemon.settings.web_host, port=daemon.settings.web_port,
            threaded=True, use_reloader=False, debug=False)
