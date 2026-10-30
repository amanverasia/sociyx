# Architecture

sociyx is a single Python package (`src/sociyx`) with a thin CLI, shared platform-neutral logic, and
per-platform adapters that do network I/O only. Three front ends sit over the same engine:

```
  CLI (cli.py)        TUI (tui/, Textual)        Web UI (web/, Starlette + htmx)
        \                    |                          /
         +-------- service.py (UI-neutral: BrowseModel, wording, import) --------+
                              |
   scanning.py  planning.py  executor.py  report.py  store.py  filters.py  redact.py
                              |
                     adapters/ (platform I/O only)
```

The engine (scanning, planning, executor, report) is never wrapped or re-implemented by a front end.

## Module map

| Module | Responsibility |
|---|---|
| `models.py` | Dataclasses and enums: `Account`, `ContentItem` (with `fingerprint()`), `DeleteStatus`, `VerifyStatus`, `Outcome`, the adapter error hierarchy (`AuthRequired`, `RateLimited`, `TransientError`, `PermanentError`), UTC helpers (`to_iso` is fixed-width so lexical order equals time order). |
| `store.py` | SQLite persistence (WAL, foreign keys on, schema version 2, `_migrate()` for v1 to v2). `Plan.confirmation_token`. |
| `filters.py` | `Filter` (types, since/until, keywords, exclude keywords), `parse_bound`, `select()` (one account, oldest first). |
| `export.py` | json/jsonl/csv rendering, `write_private_file` (mode 0600, refuses to write inside `secrets/`), `sha256_file`. |
| `scanning.py` | `scan_account`: resumable per-content-type paging with dedup, retries and missing-marking. Optional `should_stop` is polled before each page and during waits; stopping leaves the scan `running` (resumable), marks nothing missing. |
| `planning.py` | `create_plan`, `create_plan_from_items` (freeze an exact hand-picked list; `create_plan` is a filter in front of it), `plan_digest`, `check_plan_ready`, `preview`, `describe_pacing`. |
| `executor.py` | `Executor.run`, `pause_job`, `cancel_job`, `job_is_running`: the safety-critical deletion loop. Optional `on_wait(seconds, reason)` and `on_state(text)` callbacks exist for UIs; they never influence behaviour. |
| `report.py` | `build_report`, text/json/csv rendering. |
| `cli.py` | argparse commands; exit codes; wires everything together. Bare `sociyx` on a TTY (or `sociyx tui [--demo]`) lazily imports and launches the TUI; without a TTY it prints help. `sociyx ui` lazily imports and serves the web UI. |
| `service.py` | UI-neutral logic shared by the TUI and the web UI: `BrowseModel` (filtering, conversation grouping, selection set), the per-type dashboard rows, session verification, scan/review/results wording (`TYPE_WARNINGS`, `status_message`, `confirmation_phrase`), `unfinished_jobs`, `selection_digest`, and `import_export` (also used by `sociyx import`). No Textual or Starlette imports. |
| `tui/` | Guided Textual UI (see "Guided TUI" below). Presentation only: all logic that is not UI lives in the modules above. `tui/model.py` is a re-export shim of `service.py`. |
| `web/` | Local web UI (see "Local web UI" below). Presentation and access control only; `sociyx ui [--demo] [--port N] [--no-open]` lazily imports it. |
| `redact.py` | `scrub()` and a logging filter that removes credentials from logs and errors. |
| `paths.py` | Data directory layout, `private_dir` (0700), `secrets_dir(platform)`, `write_private` (0600, atomic). |
| `loopback.py` | Shared login helpers: single-use `CallbackServer` on 127.0.0.1 (`/callback`, optional preferred port, then ephemeral) and `open_visible_browser` (visible Playwright Chromium, optional persistent profile; falls back to the system browser). Used by both OAuth adapters. |
| `adapters/base.py` | The `Adapter` ABC and `ScanPage`. |
| `adapters/__init__.py` | Registry `ADAPTERS` (lazy import by `"module:Class"`), `get_adapter`, `adapter_class`. |
| `adapters/fixture.py` | Synthetic adapter with injectable faults (`SOCIYX_FIXTURE_FAULTS`) for demos and tests. |
| `adapters/bluesky/` | `adapter.py` (scan/delete/verify), `client.py` (XRPC, OAuth DPoP and password auth, error mapping), `oauth.py` (PAR + PKCE + DPoP, token exchange/refresh), `dpop.py`, `identity.py` (handle/DID/PDS resolution), `session.py`, `_http.py`. |
| `adapters/mastodon/` | `adapter.py`, `client.py` (REST, Link pagination, rate-limit mapping, HTML to text), `oauth.py` (app registration, PKCE S256, loopback and OOB), `session.py`, `mapping.py` (status JSON to `ContentItem`). |
| `adapters/instagram/` | `adapter.py` (scan/delete/verify, all 8 content types), `errors.py` (instagrapi → sociyx mapping), `mapping.py` (objects/raw dicts → `ContentItem`), `session.py` (0600 settings), `browser.py` (visible login, cookie polling), `export_import.py` (offline Download-your-information parsing). |

## Store schema

Tables (see `store.SCHEMA`):

| Table | Purpose | Notable columns |
|---|---|---|
| `meta` | key/value (`schema_version`) | |
| `accounts` | known accounts, PK `(platform, account_id)` | `handle`, `last_verified_at` |
| `items` | everything discovered, PK `(platform, account_id, item_id)` | `content_type`, `created_at` (UTC), `text`, `url`, `version`, `raw_json`, `fingerprint`, `state` (`present`/`missing`/`deleted`), `last_scan_id` |
| `scans` | one row per scan of a content type | `status` (`running`/`complete`/`failed`/`abandoned`), `cursor`, `pages`, `items_seen`, `new_items` |
| `plans` | frozen selections, unique `(plan_id, platform, account_id)` | `filters_json`, `item_count`, `digest`, `export_path`, `export_sha256` |
| `plan_items` | items in a plan and their outcome | `position`, `fingerprint` (what was previewed), `outcome`, `attempts`, `request_may_have_landed`, `detail` |
| `jobs` | one job per plan (`plan_id` unique) | `status` (`running`/`paused`/`stopped_auth`/`cancelled`/`completed`), `control` (`''`/`pause`/`cancel`, set by another process), `rate_limited_until`, `error` |
| `events` | job event log (scrubbed) | `level`, `message` |

`request_may_have_landed` records that a delete request whose outcome is unknown was sent for the
item (transient error, interrupted attempt, lost response). It persists across runs and changes how an
`ALREADY_DELETED` answer is classified (see Crash recovery). Databases created before schema v2 are
migrated on open; rows that already had attempts are flagged.

## Data flow

```
 login            scan                     select/filter           plan                      execute                         report
 -----            ----                     -------------           ----                      -------                         ------
 adapter.login -> per content type:        Filter(types, since,    items = select(...) and   token == plan token?            build_report(plan):
 verified         scan_page(cursor)        until, keywords,        only deletable types      check_plan_ready:               outcomes by section,
 Account          loop; each page is       exclude) over local     digest = sha256 of        - export exists, sha256 same    latest scans per type,
 stored           one transaction:         DB, one account only    sorted (item_id,          - plan_items match digest       discovery notes,
 (secrets_dir     upsert (dedup),                                  fingerprint)              get_or_create_job               disclaimer
  0600)           save cursor                                      export JSON written       flock job lock (non-blocking)
                  complete + non-empty                             0600 + sha256 stored      adapter.current_account == plan account?
                  => unseen items of                               token =                   for each pending/in_progress item:
                  that type: 'missing'                             plan_id:account_id:         fingerprint still == preview? else SKIPPED
                  foreign item => abort                            count:digest[:8]            (if in_progress: verify first)
                                                                                                pace (interruptible wait)
                                                                                                commit IN_PROGRESS (+attempt)
                                                                                                adapter.delete(item)
                                                                                                  RateLimited -> persist wait, wait
                                                                                                  Transient -> backoff, bounded retry
                                                                                                  AuthRequired -> stop job
                                                                                                map status; verify_deleted (retry PRESENT)
```

### Scanning

- One scan unit per content type. The cursor is stored after each page in the same transaction as the
  items, so a killed scan resumes from the last committed page (`start_or_resume_scan`; `--fresh`
  abandons the old scan).
- Dedup is by primary key; overlapping pages do not double-count. A repeated or unchanged cursor marks the scan
  failed ("pagination did not advance") instead of looping forever.
- Items not seen by a **complete** scan are marked `missing`, but only if that scan returned at least
  one item. A complete scan with zero items prints a warning and marks nothing, because an empty
  result more often means breakage (wrong account, expired session) than a deleted history.
- `--max-pages` leaves the scan `running` so the next scan resumes; the CLI reports it as PARTIAL.
- Transient errors retry with backoff (up to 3); rate limits wait up to 5 times; then the scan fails.
- Any returned item whose platform, account or content type differs from the scan's aborts the scan
  and stores nothing from that page.

### Planning

A plan is a frozen list of `(item_id, fingerprint)` with a digest, plus a JSON export of exactly those
items. The fingerprint is the SHA-256 of platform, account, item id, content type, version marker and
text. The export's SHA-256 is stored; execution refuses if the file is missing or modified. The
confirmation token ties the plan id, account, count and digest prefix together so a token for one plan
cannot authorize another.

### Execution

Pre-flight (any failure raises `PlanRefused`, nothing deleted): token matches; export intact and
digest matches the stored plan items; job not already `completed`/`cancelled`; non-blocking `flock`
on `locks/job-<id>.lock` obtained (a second process is refused; a `running` status with a free lock
means the previous process died and is recovered), then `locks/account-<platform>-<account>.lock`
(refused if another plan for the same account is running); `adapter.current_account(plan.account_id)` equals
the plan's platform and account (`AuthRequired` here stops the job as `stopped_auth`).

Per item:

1. Compare the stored item fingerprint to the plan's. Different: `skipped` ("changed since plan was
   created"), no request.
2. If the item is `in_progress` from an earlier run, call `verify_deleted`. `GONE` becomes
   `deleted_verified`; otherwise it is retried and treated as possibly-sent.
3. Wait for pacing (`min_interval`, from `adapter.min_delete_interval` or `--delay`), in chunks of at
   most 1 s so Ctrl+C, `job pause` and `job cancel` take effect during waits.
4. Commit `IN_PROGRESS` and bump `attempts` **before** the request is sent.
5. `adapter.delete(item)`. The adapter also compares the platform's current version to
   `item.version` and returns `CHANGED` if it differs (Bluesky `swapRecord`; Mastodon `edited_at`).
6. Errors:
   - `RateLimited`: the wait (default 60 s if unknown, capped at 900 s) is stored in
     `jobs.rate_limited_until` so a restart honours it; does not consume an attempt; after 5 waits
     the job pauses.
   - `TransientError`: exponential backoff 2 s, 4 s, ... capped at 60 s, up to 3 attempts, then
     `failed`. Sets `request_may_have_landed`.
   - `AuthRequired`: the item returns to `pending`, the job stops as `stopped_auth`.
   - `PermanentError`/other: `failed`.
7. Outcome mapping: `ALREADY_DELETED` becomes `already_deleted`, or `deleted_verified` if
   `request_may_have_landed` (the earlier attempt probably removed it); `INACCESSIBLE` stays;
   `CHANGED` becomes `skipped`; `DELETED` is followed by `verify_deleted` (retrying while `PRESENT`
   up to 2 more times, 3 s apart): `GONE` is `deleted_verified`, `PRESENT` is `failed`, `UNKNOWN` or a
   failed check is `deleted_unverified`.

Ctrl+C: the first sets a flag (finish current item, then pause); the second raises
`KeyboardInterrupt` immediately: the job is stored as `paused` and the in-flight item stays
`in_progress`.

### Account isolation

- Composite foreign keys: `plan_items (plan_id, platform, account_id)` references `plans`, and
  `(platform, account_id, item_id)` references `items`; a foreign account's item cannot be attached to
  a plan (test: `test_cross_account_plan_item_rejected_by_foreign_keys`).
- `select()` filters by account in SQL and again in Python.
- The scanner rejects foreign items; adapters double-check (Bluesky checks the DID of every record URI;
  Mastodon checks the status's account id).
- The executor re-asks the platform who the session belongs to before the first delete, and the CLI
  does the same before scan/whoami.

### Crash recovery

| Crash point | State left | On resume |
|---|---|---|
| Before `IN_PROGRESS` commit | `pending` | Normal attempt. |
| After commit, before/while request | `in_progress`, job `running` (or `paused`) | Lock free so run recovers; item is verified first. `GONE` is `deleted_verified`, otherwise delete is retried with `request_may_have_landed` set. |
| After request, before outcome saved | same | Same; a follow-up "already gone" is reported as verified-deleted, not "already deleted". |
| During waits | `pending` (requeued) | Normal attempt. |
| `job cancel` while an attempt's result is unknown | `in_progress` with note | Not retried (job is cancelled); the report lists it under "Interrupted / unknown" so you can check the platform. |

## Guided TUI

`src/sociyx/tui/` (Textual, imported lazily by `cli.py`):

| Path | Responsibility |
|---|---|
| `__init__.py` | `run_tui(demo)`: silences stderr logging (it would corrupt the screen) and runs the app. |
| `app.py` | `SociyxApp`: the UI-thread `Store`, adapter factory, `run_interactive`/`login_flow` (login with the real terminal), quit interception, test seams (`adapter_factory`, `executor_options`, `scan_options`, `progress_hook`). |
| `model.py` | Compatibility shim re-exporting `BrowseModel` and friends, which now live in `sociyx/service.py` (pure filtering, conversation grouping keyed on `raw` `thread_id`/`thread_title`, the selection set). |
| `dialogs.py` | Confirm, info, date-range and platform-picker modals. |
| `styles.py` | Shared Textual CSS. |
| `demo.py` | `prepare_demo_home`: scratch `SOCIYX_HOME` seeded with `DemoFixtureAdapter` accounts (fixture plus fake DMs). |
| `screens/help.py` | Key-help modal (`?`). |
| `screens/home.py`, `resume.py` | Accounts; unfinished jobs and the resume confirmation. |
| `screens/account.py`, `scan.py` | Per-type dashboard with background session re-verification; scan options, live progress, Stop. |
| `screens/browse.py`, `review.py` | Select items; review, typed `delete N` confirmation, then `create_plan_from_items` and hand-off to Run. |
| `screens/run.py`, `results.py` | Executor worker with live counts, countdown, pause/cancel; report sections and next steps. |

### Threading model

The Textual event loop (UI thread) owns `app.store`. Slow work runs in `run_worker(thread=True)` workers
that each open **their own** `Store` and their own adapter instance, because SQLite connections and adapter
clients must not cross threads. Workers talk to the UI only through `app.call_from_thread`. Controls go the
other way through the database: Pause/Cancel call `pause_job`/`cancel_job` on a separate short-lived `Store`,
which sets `jobs.control` (the executor polls it every second, including during waits); the `flock` job lock
tells `pause_job` that a worker in this same process is running. `Executor` installs its SIGINT handler only
on the main thread, so in a worker it is simply skipped; the TUI never relies on signals. Quitting during a
scan or run is intercepted (`request_quit`): the scan is told to stop after its page, a run is paused first.

Login is different: adapters prompt with `getpass` and drive a visible Playwright browser, so the UI is
suspended (`App.suspend`) and `adapter.login` runs in a plain thread without an asyncio loop (Playwright's sync
API refuses to run inside a thread that has one).

The confirmation token stays mandatory in `Executor.run`; the TUI passes `plan.confirmation_token` itself only
after the typed phrase (new run) or the resume dialog.

## Local web UI

`src/sociyx/web/` (Starlette, uvicorn, Jinja2; htmx vendored under `static/`, no build step, no CDN). Started by
`sociyx ui`, which binds a socket on `127.0.0.1` (random free port by default) and hands it to uvicorn.

| Path | Responsibility |
|---|---|
| `__init__.py` | `run_web(demo, port, open_browser)`: bind, build the app, print/open the one-time launch URL, run uvicorn with access logs off and no `Server` header. |
| `security.py` | `SecurityMiddleware`, a pure ASGI middleware in front of every route (the access-control layer, see below). |
| `state.py` | `WebState`: launch secret (single use), sessions (id, CSRF token, per-account `BrowseModel`s), job registries, adapter/store factories (mirrors `SociyxApp`, same test seams). |
| `jobs.py` | `ScanJob`, `RunJob`, `LoginJob`: background threads. |
| `app.py` | Routes and views (sync functions, run in Starlette's thread pool), `shutdown_jobs`. |
| `templates/`, `static/` | Jinja2 templates (autoescaped, no inline script or style) and `style.css`, `app.js`, `htmx.min.js` (+ its 0BSD licence note). |

Pages: Home, Add account (login), Account dashboard (session check, per-type counts, scan with Stop and a page
limit), Browse (type tabs, search, date range, conversation grouping, pagination, selection that survives
filter changes), Review, Run, Results, Resume, Import, Help.

### Threading model

Every request is handled by a sync function in the thread pool and opens its own short-lived `Store`
(SQLite connections must not cross threads). Scans, deletion runs and logins are daemon threads that each open
their own `Store` and their own adapter instance, exactly like the TUI workers, and publish progress through
attributes guarded by a lock; pages poll small HTML fragments with htmx (`hx-trigger="every 1s"`; a finished
job answers with an `HX-Redirect` or a fragment without a trigger, so polling stops). Pause and cancel go through
`pause_job`/`cancel_job` on a request-local `Store` (the same `jobs.control` mechanism as the TUI). The
`Executor`, its file locks, pacing, export-before-delete, account re-check and fingerprint checks are used
unchanged. Selection state is server-side, keyed by session id, under a per-session lock. The ASGI lifespan
shutdown (Ctrl+C) pauses every running job after its current item and stops scans between pages before the
process exits; closing a browser tab does not stop a job.

Logins run `adapter.login` in a plain thread on the server machine (visible browser or OAuth loopback). For the
Instagram password method the adapter is built with `prompt_secret`/`prompt_text` callables that answer from the
form (password first, then the optional 2FA code) and never read a terminal; the values live in the job's private
dict only for the duration of the call and are cleared afterwards.

### Security model

The threat is any web page open in the user's browser and any other local user, not a remote attacker.
`SecurityMiddleware` enforces, in this order, before routing:

1. `Host` must be exactly `127.0.0.1:PORT` or `localhost:PORT` (DNS rebinding) else 400.
2. Only GET, HEAD, POST exist (no OPTIONS, so no preflight and no CORS; `Access-Control-*` headers are never sent).
3. `GET /?launch=SECRET` is the only unauthenticated request. The secret (`secrets.token_urlsafe(32)`) is compared in
   constant time, is single use (invalidated at the first correct exchange), and mints a session id
   (`token_urlsafe(32)`) in a cookie `HttpOnly; SameSite=Strict; Path=/`, then redirects to `/` so the secret does
   not stay in the address bar.
4. Everything else needs a valid session cookie, else 401 (static files included). Sessions expire after 12 hours.
5. POST additionally needs `Origin` (or `Referer`) equal to `http://<Host>`, a urlencoded body, and the session's CSRF
   token in `X-CSRF-Token` (added by `app.js` to every htmx request) or a `csrf_token` form field. State-changing
   routes exist only as POST.

Every response, refusals included, carries `Content-Security-Policy` (`default-src 'self'`, `script-src 'self'`,
`style-src 'self'`, `frame-ancestors 'none'`, `form-action 'self'`, `base-uri 'none'`, no inline script or style),
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` and
`Cache-Control: no-store` (`no-cache` for `/static/`). htmx is configured with `allowEval: false` and without
its inline indicator styles. Static files are served from a whitelist of file names read at startup; there is no
path parameter anywhere. Export and report downloads resolve a plan id in the store (the export path must also be
inside the exports folder). Uvicorn access logging is off, nothing logs the secret, cookie, CSRF token, password or
confirmation token, and every user-visible error text goes through `redact.scrub`.

Deletion safety is the TUI's, enforced on the server: a plan is created only when the review form is submitted, from
the server-side selection (never from ids sent by the browser); the typed phrase `delete N` is compared with N
recomputed from that selection; a digest of the selection in the form detects a selection that changed in another
tab; `plan.confirmation_token` is read from the database inside the run thread and handed to `Executor.run`, and is
never put in a template, response or cookie. Starting the run clears the selection, so a double submit cannot create
a second plan.

## Design decisions and trade-offs

- **Plans are immutable, deletion is opt-in per plan.** Costs an extra step; prevents "delete what
  currently matches" surprises.
- **IN_PROGRESS before the request.** A crash yields "unknown" rather than a false "pending", at the
  price of verifying on resume.
- **Stop, never bypass.** Auth failures and challenges stop the job. A flaky login costs the user a
  manual step.
- **Honest verification.** Adapters return `UNKNOWN` when they cannot check; the report distinguishes
  verified from unverified.
- **Conservative pacing.** Defaults sit below documented limits (Bluesky 1/s, Mastodon 1/60 s). Slow but
  safe; `--delay` overrides.
- **SQLite with composite FKs** instead of query discipline alone for isolation.
- **Adapters do I/O only.** Filtering, retries, pacing and outcomes are shared, so a new platform
  cannot weaken them (see [ADDING_A_PLATFORM.md](ADDING_A_PLATFORM.md)).
- **Single-writer assumption per job and per account.** `flock` protects against two executors on the same job
  (`locks/job-<id>.lock`) and against two different plans running for the same account
  (`locks/account-<platform>-<account>.lock`), so per-account rate limits are never shared by two runs.
- **Fixture adapter in the registry.** It makes the full workflow demonstrable without real accounts
  but shows a banner on every command so fake data is never mistaken for real.
- **No telemetry, no hosted components.** Nothing to trust except the platforms you use and this code.
