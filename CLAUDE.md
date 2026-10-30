# sociyx

Local-first, open-source (MIT) tool for bulk cleanup of the user's OWN social media history.
Python 3.11+, managed with `uv`. Site: https://sociyx.com/ · Repo: github.com/amanverasia/sociyx · PyPI: `sociyx` (alpha).

## Commands

```bash
uv sync --extra instagram          # install (instagrapi is an optional extra)
uv run pytest -q                   # full suite; live tests are skipped unless SOCIYX_LIVE_TESTS=1
uv run sociyx                      # guided TUI on a TTY; prints help otherwise
uv run sociyx tui --demo           # TUI on fake data in a temp folder
uv run sociyx ui --demo --no-open  # local web UI on fake data
```

## Layout

- `src/sociyx/` engine: `store.py` (SQLite), `scanning.py`, `filters.py`, `export.py`, `planning.py`, `executor.py`, `report.py`, `service.py` (UI-neutral logic shared by TUI and web), `cli.py`.
- `src/sociyx/adapters/` one package per platform (`bluesky`, `mastodon`, `instagram`) plus `fixture.py` (fake platform for tests/demo). Contract in `adapters/base.py`; guide in `docs/ADDING_A_PLATFORM.md`.
- `src/sociyx/tui/` Textual front end. `src/sociyx/web/` Starlette + Jinja2 + vendored htmx front end (no JS build).
- `website/` static marketing site (hosted on Cloudflare later). Excluded from the sdist. Not part of the Python package.
- `docs/` architecture, platform status, verification log, releasing. Keep `docs/VERIFICATION.md` and `PLATFORM_STATUS.md` honest.

## Rules that must not be broken

- Adapters do platform I/O only. Filtering, export, plans, retries, pacing, job state and reporting are shared code; don't reimplement them in an adapter or a front end.
- Deletion safety is shared by CLI, TUI and web: export first, typed `delete N` confirmation, confirmation token computed server-side, account recheck, fingerprint check against the previewed item, `IN_PROGRESS` committed before each request, pacing and daily caps, soft-block cool-downs. Never weaken or bypass these in a front end.
- Never claim a complete wipe. Report successful, failed, skipped and unverified deletions separately.
- Never bypass CAPTCHAs, MFA or other authentication controls. Login is manual in a visible browser.
- Treat browser profiles, session files and the data folder (`SOCIYX_HOME`, default `~/.local/share/sociyx`) as credentials. Keep them out of Git, exports, logs and screenshots. Pass anything logged through `sociyx.redact`. Never ask the user to paste passwords or tokens into chat.
- No telemetry, no hosted backend, no AI. Everything runs locally.
- Do not run live deletions against a real account unless the user explicitly authorizes that specific account and plan. Tests use the fixture adapter only.
- Don't copy code, branding or assets from Redact.dev (proprietary); sociyx is independent.

## Web UI security (localhost)

Binds 127.0.0.1; single-use launch secret to session cookie; Host check, Origin and CSRF check on POSTs; strict CSP; no CORS. `Referrer-Policy` must stay `same-origin`: `no-referrer` makes browsers send `Origin: null` on form POSTs and breaks every submit. Test client tests don't catch this, so check changes in a real browser (Playwright is a dependency).

## Conventions

- Match surrounding style; keep comments sparse and explain why, not what.
- Schema changes need a migration in `store.py` and a test in `tests/test_store_migration.py`.
- New platform behavior needs fixture or mock-server tests; mark anything hitting real endpoints `@pytest.mark.live` (read-only only).
- Legacy `UNPOST_HOME` and the old `unpost` data folder are still honored (see `paths.py`); nothing is moved automatically.

## Releasing

Trusted Publishing, no tokens: bump `version` in `pyproject.toml`, run `uv lock`, commit, push, then `gh release create vX.Y.Z --prerelease`. The `release.yml` workflow publishes to PyPI. Versions can never be reused. See `docs/RELEASING.md`.
