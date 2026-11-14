# Contributing

Thanks for your interest in AlarmPi.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
pre-commit install
```

## Before you push

- `ruff check .` — lint must be clean.
- `python -m pytest` — all tests must pass.
- `pre-commit run --all-files` — runs gitleaks + formatting/hygiene hooks.
- Never commit real secrets or your personal `config/*.json` (these are gitignored).

CI runs ruff, the same test suite (Python 3.10–3.12), a web-asset build and a gitleaks scan; PRs must be green.

## Conventions

- No new runtime dependencies beyond Flask without discussion.
- Keep modules focused; add tests for new behavior.
