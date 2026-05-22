# Contributing

Thanks for your interest in AlarmPi.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest pre-commit
pre-commit install
```

## Before you push

- `python -m pytest` — all tests must pass.
- `pre-commit run --all-files` — runs gitleaks + formatting/hygiene hooks.
- Never commit real secrets or your personal `config/*.json` (these are gitignored).

CI runs the same test suite (Python 3.10–3.12) and a gitleaks scan; PRs must be green.

## Conventions

- No new runtime dependencies beyond Flask without discussion.
- Keep modules focused; add tests for new behavior.
