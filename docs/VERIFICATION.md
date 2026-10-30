# Verification: what was tested and how

The automated test suite performs no real login and no real deletion. The only real-account
results are the Instagram ones below, which the account owner reported (not reproducible by the
suite). The first real deletion on any platform should be a tiny plan (1-3 items) that you have
reviewed and authorize yourself.

## Verified by the user on a real account

| Platform | Result | Source |
|---|---|---|
| Instagram | scan works | user report |
| Instagram | list works | user report |
| Instagram | DM unsend works | user report |

Nothing else was reported, so nothing else is claimed.

## Automated tests (mock transport and fixture)

`uv run --extra instagram pytest` runs 201 tests offline and skips 7 live tests (as of this writing).

| File | Covers |
|---|---|
| `tests/test_filters.py` | `since`/`until` inclusivity, bare-date end-of-day semantics and the day after, exact instants, `since > until` error, unknown timestamps, offset input converted to UTC, case-insensitive any-match keywords, exclude wins over include, type filter |
| `tests/test_isolation.py` | `select` never returns another account's items; composite foreign keys reject a cross-account plan item; scanner rejects a foreign item and stores nothing; executor refuses when the session is a different account |
| `tests/test_scanning.py` | overlapping pages deduplicated; interrupted scan resumes from saved cursor; stuck cursor detected; missing-marking only after a complete scan; partial scan stays resumable; zero-item complete scan warns and marks nothing; transient retry then fail; unknown content type |
| `tests/test_executor.py` | wrong token refused; only plan items deleted; edits after planning skipped (platform side and local DB); tampered/missing export refused; distinct outcomes; retry limits (exactly `max_attempts` calls); rate-limit waits persisted and chunked, pause/cancel during waits; auth expiry and challenge stop, resume after login; crash after request then resume never double-deletes; stale `running` recovery; pause/cancel/resume; completed job cannot rerun; job lock held elsewhere; pacing; lost response then gone is verified; scrubbed error details |
| `tests/test_cli.py` | `platforms` and fixture banner, list/export, friendly bad-filter error, plan dry-run/live/report, auth stop exit code and resume |
| `tests/test_redact.py` | bearer/DPoP tokens, JWTs, app passwords, JSON password/refresh token removed; normal text untouched |
| `tests/test_store_migration.py` | v1 databases migrate to v2 with data preserved and attempted rows flagged `request_may_have_landed` |
| `tests/test_bluesky.py` | identity resolution (PLC, well-known fallback, mismatch, garbage), DPoP proofs, PKCE vector, client id, PAR nonce retry, full OAuth login, bad state/sub/timeouts, 0600 session files, refresh on 401, app-password login with 2FA and no leakage, scan pagination and type mapping, foreign DID rejected, delete via `swapRecord` and its edge cases (already gone, changed, `InvalidSwap`, forbidden, foreign repo), rate limits (`Retry-After`, `ratelimit-reset`), 5xx/transport as transient, 401 as auth, verify, logout |
| `tests/test_mastodon.py` | registry/contract, HTML to text, loopback login and app cache, state mismatch, OOB fallback and explicit OOB, username mismatch revokes token, scan pagination (Link, empty page, no-Link fallback), post/boost split, favourites/bookmarks ids, foreign status rejected, `edited_at` version, public scan without session, 429 handling, 5xx, 401, delete/undo classification, verify, logout |
| `tests/test_instagram.py` | registry/metadata, media/like/saved mapping, post scan filters (clips/foreign), unknown type, `comment_export` empty scan, media delete (success/changed/inaccessible), gone→already-deleted, like verify, error mapping (gone/blocked/auth/rate-limit/transient), session 0600, no secrets in errors, export parsing, DM own-only filter, CLI `import` offline and directory validation, DM sender verification before unsend, CLI session-file forwarding |

These tests use `httpx.MockTransport` and canned responses written from documentation and live
observations. They prove the code behaves as designed against those responses, not that real servers
behave the same way.

## Live read-only tests

Run with `SOCIYX_LIVE_TESTS=1 uv run pytest -m live`. They make unauthenticated GET requests (and one
PAR request, see below). No login, no tokens, no writes.

| Platform | Test | Result (2026-10-30) |
|---|---|---|
| Bluesky | `test_resolve_bsky_app` resolves a public handle to DID and PDS | passed |
| Bluesky | `test_scan_public_account_two_pages` pages `listRecords` twice with cursor | passed |
| Bluesky | `test_verify_nonexistent_record_is_gone` | passed (400 `RecordNotFound` gives `GONE`) |
| Bluesky | `test_oauth_discovery` PDS to authorization-server metadata | passed |
| Bluesky | `test_par_request_returns_request_uri` PAR with DPoP, nonce challenge then success | passed |
| Mastodon | `test_public_statuses_two_pages` mastodon.social, Link pagination | passed |
| Mastodon | `test_nonexistent_status_is_gone` | passed (404) |

Totals: Bluesky 5 passed, Mastodon 2 passed. The PAR request creates a pending authorization request
on bsky.social that nobody completes; it grants nothing.

## Not yet live-verified

- Instagram: deleting posts, reels, stories; unliking; unsaving; deleting comments (own posts and
  `comment_export`); importing a real data export; logging in through the TUI (suspend plus
  Playwright)
- Bluesky and Mastodon: any real login (OAuth browser flow, app-password flow, emailed code,
  Mastodon loopback/OOB) and any delete, unreblog, unfavourite, unbookmark
- Token exchange, DPoP-bound resource requests, token refresh, token revocation
- Rate-limit behaviour under real load; the actual limits applied to your account
- The Playwright window on Linux Mint (Chromium launch, `install-deps`)
- Propagation to AppViews, relays, other Mastodon servers
- Favourites/bookmarks pagination on a live server
- Any history depth cap on listing endpoints

See the "Verified vs unverified" tables in [platforms/bluesky.md](platforms/bluesky.md),
[platforms/mastodon.md](platforms/mastodon.md) and [platforms/instagram.md](platforms/instagram.md).
