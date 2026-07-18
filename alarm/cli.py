#!/usr/bin/env python3
"""
Alarm CLI.

Three groups of commands:
  config   — edit alarms.json directly (the running daemon hot-reloads):
             list, add, edit, enable, disable, delete, skip, next
  runtime  — talk to the running daemon over its local HTTP API:
             status, dismiss, snooze, test
  service  — manage the daemon process:
             serve (foreground), start, stop, restart, install
"""

import sys
import json
import argparse
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from . import config
from .config import (
    load_alarms, load_settings, get_alarm_by_id, add_alarm,
    ensure_config_dir, PROJECT_ROOT,
)
from .models import Alarm
from .scheduler import AlarmScheduler
from .dayutil import parse_days, format_days, validate_time
from .validation import ValidationError, validate_alarm_payload

SERVICE = "alarm-daemon.service"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


# ---------------- runtime HTTP helpers ----------------

def _api(method: str, path: str, body: dict | None = None):
    port = load_settings().web_port
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or "null")
    except urllib.error.URLError as e:
        print(_c("31", f"Cannot reach daemon on port {port} ({e}). Is it running? `alarm start`"))
        sys.exit(2)


# ---------------- config commands ----------------

def _print_validation(error: ValidationError) -> int:
    for issue in error.issues:
        print(_c("31", f"{issue.field}: {issue.message}"))
    return 1


def _alarm_input(args, *, partial=False) -> dict:
    payload = {}
    aliases = {
        "days": "days_of_week", "sound": "sound_path", "volume": "base_volume",
        "irritable_duration": "irritable_duration_minutes",
        "irritable_step": "irritable_volume_step",
    }
    for source, target in aliases.items():
        if hasattr(args, source) and getattr(args, source) is not None:
            value = getattr(args, source)
            if source == "days":
                try:
                    value = parse_days(value)
                except ValueError:
                    value = []
            if source == "sound":
                value = None if value in (None, "default") else value
            payload[target] = value
    if hasattr(args, "label") and args.label is not None:
        payload["label"] = args.label
    if hasattr(args, "time") and args.time is not None:
        try:
            payload["time"] = validate_time(args.time)
        except ValueError:
            payload["time"] = args.time
    if not partial:
        payload.update({
            "id": "pending", "enabled": not args.disabled,
            "snoozable": not args.no_snooze, "irritable": args.irritable,
            "sound_path": args.sound or None, "skip_dates": [],
        })
    return payload

def cmd_list(args) -> int:
    alarms = load_alarms()
    if not alarms:
        print("No alarms configured. Add one: alarm add --label 'Wake' --time 07:00 --days weekdays")
        return 0
    print(_c("1", f"{'ID':<4} {'Label':<22} {'Time':<6} {'Days':<10} {'On':<4} {'Snz':<4} {'Irr':<4}"))
    print("-" * 58)
    for a in alarms:
        on = (_c("32", "on ") if a.enabled else _c("90", "off")).ljust(3)
        label = (a.label[:21] + "…") if len(a.label) > 22 else a.label
        print(f"{a.id:<4} {label:<22} {a.time:<6} {format_days(a.days_of_week):<10} "
              f"{on}  {'y' if a.snoozable else '-':<4} {'y' if a.irritable else '-':<4}")
    return 0


def cmd_add(args) -> int:
    try:
        alarm = Alarm.from_payload(validate_alarm_payload(_alarm_input(args)))
    except ValidationError as e:
        return _print_validation(e)
    alarm = add_alarm(alarm)
    print(_c("32", f"Added #{alarm.id}: '{alarm.label}' at {alarm.time} ({format_days(alarm.days_of_week)})"))
    return 0


def cmd_edit(args) -> int:
    if not get_alarm_by_id(args.id):
        print(_c("31", f"Error: alarm not found: {args.id}"))
        return 1
    try:
        changes = validate_alarm_payload(_alarm_input(args, partial=True), partial=True)
    except ValidationError as e:
        return _print_validation(e)
    updated = None
    def mutate(document):
        nonlocal updated
        for alarm in document.alarms:
            if alarm.id == args.id:
                for field, value in changes.items():
                    setattr(alarm, field, value)
                updated = alarm
                return
    config.get_repository().transaction(mutate)
    print(_c("32", f"Updated #{updated.id}: '{updated.label}'"))
    return 0


def _toggle(args, value: bool) -> int:
    alarm = get_alarm_by_id(args.id)
    if not alarm:
        print(_c("31", f"Error: alarm not found: {args.id}"))
        return 1
    if not value:
        config.get_repository().disable_alarm(args.id)
    else:
        def enable(document):
            for current in document.alarms:
                if current.id == args.id:
                    current.enabled = True
        config.get_repository().transaction(enable)
    print(f"{'Enabled' if value else 'Disabled'} #{alarm.id}: '{alarm.label}'")
    return 0


def cmd_enable(args):
    return _toggle(args, True)


def cmd_disable(args):
    return _toggle(args, False)


def cmd_delete(args) -> int:
    alarm = get_alarm_by_id(args.id)
    if not alarm:
        print(_c("31", f"Error: alarm not found: {args.id}"))
        return 1
    if not args.yes:
        if input(f"Delete #{alarm.id} '{alarm.label}'? [y/N] ").lower() not in ("y", "yes"):
            print("Cancelled")
            return 0
    config.get_repository().delete_alarm(args.id)
    print(f"Deleted #{args.id}")
    return 0


def cmd_skip(args) -> int:
    alarm = get_alarm_by_id(args.id)
    if not alarm:
        print(_c("31", f"Error: alarm not found: {args.id}"))
        return 1
    raw = args.date.lower()
    if raw == "today":
        date = datetime.now().strftime("%Y-%m-%d")
    elif raw == "tomorrow":
        date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        try:
            date = datetime.strptime(args.date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print(_c("31", "Error: use YYYY-MM-DD, 'today', or 'tomorrow'"))
            return 1
    def skip(document):
        for current in document.alarms:
            if current.id == args.id and date not in current.skip_dates:
                current.skip_dates.append(date)
    config.get_repository().transaction(skip)
    print(f"Skipping #{alarm.id} on {date}")
    return 0


def cmd_next(args) -> int:
    upcoming = AlarmScheduler().get_next_alarms(load_alarms(), count=args.count)
    if not upcoming:
        print("No upcoming alarms.")
        return 0
    print(_c("1", f"Next {len(upcoming)} alarm(s):"))
    for alarm, dt in upcoming:
        print(f"  {dt.strftime('%a %Y-%m-%d %H:%M')}  #{alarm.id} '{alarm.label}'")
    return 0


# ---------------- runtime commands ----------------

def cmd_status(args) -> int:
    s = _api("GET", "/api/status")
    print(f"Time:    {s['now']}")
    print(f"Alarms:  {s['enabled_count']}/{s['alarm_count']} enabled")
    if s.get("next"):
        n = s["next"]
        print(f"Next:    {n['time']} '{n['label']}' ({n['when_human']})")
    else:
        print("Next:    none")
    if s.get("ringing"):
        print(_c("31", f"RINGING: '{s['ringing']['label']}' (since {s['ringing']['started_at']})"))
    return 0


def cmd_dismiss(args) -> int:
    print("Dismissed." if _api("POST", "/api/ringing/dismiss").get("dismissed") else "Nothing ringing.")
    return 0


def cmd_snooze(args) -> int:
    body = {"minutes": args.minutes} if args.minutes else {}
    print("Snoozed." if _api("POST", "/api/ringing/snooze", body).get("snoozed") else "Nothing ringing.")
    return 0


def cmd_test(args) -> int:
    r = _api("POST", f"/api/alarms/{args.id}/test")
    print("Firing test." if r.get("ok") else _c("31", "Not found or an alarm is already ringing."))
    return 0


# ---------------- service commands ----------------

def cmd_serve(args) -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .daemon_core import run_daemon
    run_daemon()
    return 0


def _systemctl(*sub) -> int:
    try:
        return subprocess.call(["systemctl", "--user", *sub])
    except FileNotFoundError:
        print(_c("31", "systemctl not available"))
        return 1


def cmd_start(args):
    return _systemctl("start", SERVICE)


def cmd_stop(args):
    return _systemctl("stop", SERVICE)


def cmd_restart(args):
    return _systemctl("restart", SERVICE)


def cmd_install(args) -> int:
    import shutil
    from pathlib import Path
    src = PROJECT_ROOT / "systemd" / SERVICE
    if not src.exists():
        print(_c("31", f"Unit file missing: {src}"))
        return 1
    dst_dir = Path.home() / ".config" / "systemd" / "user"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / SERVICE)
    _systemctl("daemon-reload")
    print(f"Installed {SERVICE}. Enable with: systemctl --user enable --now {SERVICE}")
    return 0


# ---------------- parser ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alarm", description="Alarm clock CLI")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="List alarms").set_defaults(func=cmd_list)

    a = sub.add_parser("add", help="Add an alarm")
    a.add_argument("--label", "-l", required=True)
    a.add_argument("--time", "-t", required=True, help="HH:MM (24h)")
    a.add_argument("--days", "-d", required=True, help="0-6 / names / weekdays|weekends|daily")
    a.add_argument("--disabled", action="store_true", help="create disabled")
    a.add_argument("--no-snooze", action="store_true")
    a.add_argument("--irritable", action="store_true")
    a.add_argument("--sound")
    a.add_argument("--volume", "-v", type=int, default=70)
    a.add_argument("--irritable-duration", type=int, default=5)
    a.add_argument("--irritable-step", type=int, default=10)
    a.set_defaults(func=cmd_add)

    e = sub.add_parser("edit", help="Edit an alarm")
    e.add_argument("id")
    e.add_argument("--label", "-l")
    e.add_argument("--time", "-t")
    e.add_argument("--days", "-d")
    e.add_argument("--volume", "-v", type=int)
    e.add_argument("--sound", help="path, or 'default' to reset")
    e.set_defaults(func=cmd_edit)

    for name, fn, h in (("enable", cmd_enable, "Enable an alarm"),
                        ("disable", cmd_disable, "Disable an alarm")):
        sp = sub.add_parser(name, help=h)
        sp.add_argument("id")
        sp.set_defaults(func=fn)

    d = sub.add_parser("delete", help="Delete an alarm")
    d.add_argument("id")
    d.add_argument("-y", "--yes", action="store_true")
    d.set_defaults(func=cmd_delete)

    sk = sub.add_parser("skip", help="Skip an alarm on a date")
    sk.add_argument("id")
    sk.add_argument("date", help="YYYY-MM-DD | today | tomorrow")
    sk.set_defaults(func=cmd_skip)

    nx = sub.add_parser("next", help="Show upcoming alarms")
    nx.add_argument("-n", "--count", type=int, default=5)
    nx.set_defaults(func=cmd_next)

    sub.add_parser("status", help="Daemon/runtime status").set_defaults(func=cmd_status)
    sub.add_parser("dismiss", help="Dismiss the ringing alarm").set_defaults(func=cmd_dismiss)
    sn = sub.add_parser("snooze", help="Snooze the ringing alarm")
    sn.add_argument("-m", "--minutes", type=int)
    sn.set_defaults(func=cmd_snooze)
    ts = sub.add_parser("test", help="Fire an alarm now (via daemon)")
    ts.add_argument("id")
    ts.set_defaults(func=cmd_test)

    sub.add_parser("serve", help="Run the daemon in the foreground").set_defaults(func=cmd_serve)
    sub.add_parser("start", help="Start the systemd service").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="Stop the systemd service").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="Restart the systemd service").set_defaults(func=cmd_restart)
    sub.add_parser("install", help="Install the systemd user unit").set_defaults(func=cmd_install)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        build_parser().print_help()
        return 1
    ensure_config_dir()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
