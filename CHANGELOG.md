# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed
- Web server startup hardened: runs in a failure-isolated daemon thread (a web
  crash can no longer affect the alarm scheduler), `_start_web` is now
  idempotent, and the `web_enabled` config gate (default `true`) is honored at
  runtime — toggling it on via config hot-reload starts the server live.

## [1.0.0] — 2026-05-22

Initial public release.

### Added
- Packaged `alarm` CLI: config (`list/add/edit/enable/disable/delete/skip/next`),
  runtime (`status/dismiss/snooze/test`), and service (`serve/start/stop/restart/install`).
- Tailwind LAN web UI: dashboard with live RINGING banner, alarms list, add/edit
  form, and settings — including remote dismiss/snooze of a ringing alarm.
- JSON HTTP API backing the web UI and CLI runtime commands.
- Single-process daemon hosting the scheduler loop and web server over a shared,
  thread-safe `RuntimeState`; non-blocking alarm firing.
- Persistent snooze and ringing state (`config/state.json`) that survives restarts.
- On-device Tkinter popup as a thin client of the local API (auto-detected via `DISPLAY`).
- Optional LAN PIN with automatic localhost bypass.
- Atomic, file-locked config writes with live hot-reload.
- pytest suite, GitHub Actions CI (Python 3.10–3.12), gitleaks secret scanning,
  and pre-commit hooks mirroring CI.

### Notes
- Carries forward the original alarm engine unchanged: mpv playback, amixer
  volume escalation ("irritable mode"), snooze, skip-dates, and per-alarm sounds.
