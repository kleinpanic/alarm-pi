# AlarmPi — Raspberry Pi alarm clock

[![CI](https://github.com/kleinpanic/alarm-pi/actions/workflows/ci.yml/badge.svg)](https://github.com/kleinpanic/alarm-pi/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

Headless alarm daemon for a Raspberry Pi with:

- A packaged **`alarm` CLI** (config + runtime control + service management)
- A **LAN web UI** (Tailwind) to manage alarms and dismiss/snooze a ringing
  alarm from your phone
- Optional on-device **Tkinter popup** (when a display is attached)
- Irritable mode (escalating volume), snooze, skip-dates, per-alarm sounds
- JSON config with atomic writes; runtime state persisted across restarts

## Requirements

- Python 3.10+ (Debian's system Python is fine; a venv is used for Flask)
- `mpv` (audio playback) and `amixer` (volume): `sudo apt install mpv alsa-utils`
- An X display only if you want the on-device popup (`DISPLAY=:0`)

## Install

```bash
git clone https://github.com/kleinpanic/alarm-pi.git
cd alarm-pi
python3 -m venv .venv
.venv/bin/pip install -e .          # installs Flask + the `alarm` command

# seed local config from the examples (these files are gitignored)
cp config/settings.example.json config/settings.json
cp config/alarms.example.json   config/alarms.json

.venv/bin/alarm install             # install the systemd user unit
systemctl --user enable --now alarm-daemon.service
```

The daemon serves the web UI on `http://<pi-lan-ip>:8765`.

If you skip the `cp` step the daemon creates default config on first run.

## Configuration

Runtime config lives in `config/` and is **gitignored** (it holds your
personal schedule). Versioned templates are provided:

- `config/alarms.example.json` — alarm definitions
- `config/settings.example.json` — audio/web/snooze settings

`alarms.json` and `settings.json` are hot-reloaded by the running daemon, so
CLI/web edits apply live. Paths in `default_sound` may be absolute or relative
to the project root.

## CLI

```bash
# config (the running daemon hot-reloads on change)
alarm list
alarm add --label "Wake up" --time 07:00 --days weekdays
alarm add --label "Gym" --time 06:30 --days mon,wed,fri --irritable
alarm edit 1 --time 07:30
alarm enable 1 | alarm disable 1 | alarm delete 1
alarm skip 1 tomorrow
alarm next

# runtime (talks to the running daemon)
alarm status
alarm dismiss
alarm snooze --minutes 10
alarm test 1

# service
alarm serve            # run in foreground
alarm start | stop | restart
```

`--days` accepts `0-6` (0=Mon), day names (`mon,wed`), or `weekdays` /
`weekends` / `daily`.

## Web UI

`http://<pi>:8765` — dashboard (clock, next alarm, live RINGING banner with
Dismiss/Snooze), alarms list (toggle/edit/test/skip/delete), add/edit form,
and settings (snooze, port, optional PIN, default sound).

**PIN:** blank by default (open on a trusted LAN). Set a PIN in Settings to
gate other LAN hosts; `127.0.0.1` always bypasses it, so the popup and CLI
never need it.

## Project layout

```
alarm/
  cli.py          packaged `alarm` entrypoint
  daemon_core.py  scheduler loop + web thread + non-blocking firing + control
  scheduler.py    time/day/skip matching, snooze
  state.py        thread-safe runtime state (ringing + snoozes), persisted
  audio.py        mpv playback + amixer volume escalation
  popup.py        standalone Tk popup (buttons POST to local API)
  config.py       atomic, locked JSON I/O
  models.py       Alarm, Settings
  web/            Flask app + Jinja2/Tailwind templates
config/           alarms.json, settings.json, state.json
systemd/          alarm-daemon.service (user unit)
tests/            pytest
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest pre-commit
.venv/bin/python -m pytest          # run the test suite
pre-commit install                  # enable local gitleaks + pytest hooks
```

CI (GitHub Actions) runs the same `pytest` suite across Python 3.10–3.12 plus a
[gitleaks](https://github.com/gitleaks/gitleaks) secret scan over full history.
The pre-commit hooks mirror CI so local commits and CI catch the same issues.

## License

[MIT](LICENSE) © 2026 Klein Panic
