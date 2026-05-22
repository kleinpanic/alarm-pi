# Alarm System Upgrade — CLI + LAN Web UI

**Date:** 2026-05-22
**Branch:** `upgrade/cli-and-webui`
**Target host:** `eulerpi4` (Raspberry Pi 4, Debian 13 arm64), runtime path `/home/klein/codeWS/Python/Alarm`

## Goal

Upgrade the existing Python alarm system into (1) a proper packaged CLI and
(2) a Tailwind-styled, LAN-accessible web UI that can manage alarms **and**
control a currently-ringing alarm (dismiss / snooze) remotely.

The school alarm schedule has been stopped and the systemd service disabled;
the upgrade must not re-introduce those alarms.

## Current state (reviewed)

- `models.py` — `Alarm`, `Settings` dataclasses (clean).
- `config.py` — JSON load/save; non-atomic, no locking.
- `scheduler.py` — `AlarmScheduler`: time/day/skip matching, in-memory snooze (lost on restart).
- `audio.py` — `AudioPlayer`: mpv subprocess + amixer volume escalation ("irritable mode"). Already non-blocking.
- `popup.py` — Tkinter popup; `mainloop()` **blocks** the daemon loop.
- `daemon_core.py` — `AlarmDaemon`: 5s poll loop; `_fire_alarm` blocks on the popup (one alarm at a time, loop stalls while ringing).
- `alarm_cli.py` — argparse CLI (list/add/edit/enable/disable/delete/test/next/skip).
- No git, no tests, no package metadata. `validate_time` has a dead duplicate branch.

## Decisions (approved)

1. **Full control incl. live ringing** — daemon hosts the web server in-process; web can dismiss/snooze the active alarm.
2. **Keep popup when a display exists**, web is primary control.
3. **Optional PIN, default off** — open on trusted LAN; PIN gates control actions when set.

## Architecture

Single daemon process, three cooperating parts sharing one thread-safe
`RuntimeState`:

- **Scheduler loop thread** — polls alarms; on fire, sets `ringing` state and
  starts audio (non-blocking). Never blocks on UI.
- **Flask web server thread** — bound `0.0.0.0:<web_port>`; serves HTML pages
  (Jinja2 + Tailwind) and a JSON API. Reads state, invokes control actions.
- **Popup (optional, separate process)** — launched by the daemon when
  `DISPLAY` is set; its STOP/SNOOZE buttons call the **local HTTP API**
  (`127.0.0.1:<web_port>`). Daemon kills it on remote dismiss.

**Unified control:** dismiss / snooze / test all funnel through the daemon's
control methods, exposed over HTTP. Web, popup, and CLI are all clients of the
same API — one code path, no divergent logic.

`alarms.json` remains the source of truth for config; the daemon hot-reloads on
mtime change (so CLI edits apply live). `config/state.json` persists snooze schedule
+ ringing status (restart-safe; the web reads it for live status).

### Runtime environment (Debian 13 / PEP 668)

Debian Trixie's system Python is externally-managed (PEP 668), so Flask cannot
be `pip install`ed globally. The project ships a venv at `.venv/` created with
`python3 -m venv`; Flask installs there. The systemd unit's `ExecStart` points
at the venv interpreter: `/home/klein/codeWS/Python/Alarm/.venv/bin/python -m
alarm serve`. The CLI entrypoint is also the venv `alarm` script.

### PIN and local clients

When a PIN is set it gates the web UI from other LAN hosts. Requests from
`127.0.0.1`/`::1` **bypass** the PIN — so the daemon-spawned popup and `alarm`
CLI runtime commands (which hit the local API) never need it. Remote browsers
authenticate once via a login page; a signed session cookie carries it.

### Port

Default web port **8765** (avoids the crowded 5000). Configurable via
`settings.web_port`. Templates, tests, systemd unit, and CLI all read this default.

### Module layout

```
alarm/
  models.py        Alarm, Settings (+ web_* settings fields)
  config.py        atomic writes + file lock
  state.py         NEW thread-safe RuntimeState, state.json persistence
  scheduler.py     snooze sourced from RuntimeState
  audio.py         (unchanged)
  popup.py         thin client: buttons POST to local API; daemon-managed lifecycle
  daemon_core.py   scheduler loop + web thread + non-blocking fire + control methods
  web/
    server.py      Flask app factory; pages + JSON API; optional PIN
    templates/     base, dashboard, alarms, form, settings (Tailwind via Play CDN)
    static/
  cli.py           NEW packaged entrypoint
pyproject.toml     console_scripts: alarm = alarm.cli:main
tests/             pytest: models, scheduler, config, web routes
```

### CLI (`alarm …`)

- Config (edit JSON directly, daemon hot-reloads): `list`, `add`, `edit`,
  `enable`, `disable`, `delete`, `skip`, `next`.
- Runtime (HTTP to running daemon): `status`, `dismiss`, `snooze`, `test`.
- Service/process: `serve` (run daemon in foreground), `start`/`stop`/`restart`
  (wrap `systemctl --user`), `install` (deploy unit).
- Nicer output (aligned tables, color when tty). Keep stdlib argparse — no new
  runtime dep beyond Flask.

### Web API (JSON)

`GET /api/status`, `GET /api/alarms`, `POST /api/alarms`, `PUT /api/alarms/<id>`,
`DELETE /api/alarms/<id>`, `POST /api/alarms/<id>/enable|disable|skip|test`,
`POST /api/ringing/dismiss`, `POST /api/ringing/snooze`, `GET/PUT /api/settings`.
Pages: `/` dashboard, `/alarms`, `/alarms/new`, `/alarms/<id>/edit`, `/settings`.

### Web UI

- **Dashboard:** current time, next alarm, alarm count; big red **RINGING**
  banner with Dismiss + Snooze buttons (auto-poll `/api/status` every ~2s).
- **Alarms:** cards — label, time, days, badges (enabled/snooze/irritable);
  inline enable toggle, edit, delete, skip-today.
- **Add/Edit form:** label, time, day pickers, sound, volume, irritable knobs.
- **Settings:** audio player, default sound/snooze, web port, PIN toggle.
- Mobile-first (phone on LAN). Tailwind Play CDN — no Node build on the Pi.

### Error handling

- Atomic config writes (`tmp` + `os.replace`) + `flock` to avoid CLI/web races.
- Web actions validate input; return JSON `{error}` + 4xx on bad data.
- Missing sound / no amixer / no display degrade gracefully (already partly handled).
- PIN (when set) required on all mutating API routes + page access via session cookie.

### Testing & validation

- `pytest` for scheduler (fire/skip/snooze/next), models round-trip, config
  atomic IO, web routes (Flask test client).
- Logic + web routes testable on macOS; audio/popup are Pi-only (integration).
- **Visual validation:** deploy to Pi, run daemon, SSH-tunnel the web port to
  the Mac, drive with Playwright MCP + screenshots (dashboard, alarms, form,
  ringing banner).

### Deploy

Develop in local clone → `pytest` → rsync to Pi → create `.venv` + install
Flask on the Pi → update systemd unit
(`ExecStart=/home/klein/codeWS/Python/Alarm/.venv/bin/python -m alarm serve`) →
`systemctl --user enable --now` → tunnel + screenshot validation. No school
alarms re-added.

## Out of scope (YAGNI)

Accounts/multi-user, HTTPS/TLS, push notifications, calendar sync, audio
streaming to the browser, packaging to PyPI.
