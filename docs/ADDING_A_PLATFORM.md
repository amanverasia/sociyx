# Adding a platform

An adapter does **platform I/O only**. Do not implement filtering, export, planning, retries, pacing,
job state or reporting in an adapter; the shared code provides them and enforces the safety model.

Before writing code, check the platform's terms and API. Prefer official APIs. Never implement CAPTCHA
solving, MFA bypass, or credential scraping. Browser automation is acceptable only in a visible
browser, with the user logging in themselves, and with the ToS risk documented in `discovery_notes` and
`docs/PLATFORM_STATUS.md`.

## Steps

1. Create `src/sociyx/adapters/<name>/` (or a single module) with a class subclassing
   `sociyx.adapters.base.Adapter`.
2. Register it in `src/sociyx/adapters/__init__.py`:
   ```python
   ADAPTERS = {..., "myplatform": "sociyx.adapters.myplatform:MyPlatformAdapter"}
   ```
   Imports are lazy, so heavy dependencies only load when used.
3. Write mock-transport tests (and optional read-only live tests), then docs: `docs/platforms/<name>.md`
   and a row in the README table.

## Skeleton

```python
from __future__ import annotations
from typing import ClassVar
import httpx

from sociyx.adapters.base import Adapter, ScanPage
from sociyx.models import (Account, AuthRequired, ContentItem, DeleteResult, DeleteStatus,
                           PermanentError, RateLimited, TransientError, VerifyStatus)


class MyPlatformAdapter(Adapter):
    platform: ClassVar[str] = "myplatform"
    label: ClassVar[str] = "My Platform"
    content_types: ClassVar[dict[str, str]] = {"post": "your posts", "like": "your likes"}  # one scan unit each
    deletable_types: ClassVar[frozenset[str]] = frozenset({"post", "like"})
    discovery_notes: ClassVar[str] = "What history is listed, what is NOT covered, what is unverified."
    min_delete_interval: ClassVar[float] = 2.0   # seconds; stay below documented limits

    def __init__(self, http: httpx.Client | None = None, **injectables):
        self._http = http or httpx.Client(timeout=30)   # injectable for tests

    def login(self, account_hint=None, method=None) -> Account:
        # Interactive. Persist the session under paths.secrets_dir("myplatform") using
        # paths.write_private (0600). Return the account AS REPORTED BY THE PLATFORM after login.
        ...

    def current_account(self, account_id: str) -> Account:
        # Ask the platform who the stored session belongs to. Raise AuthRequired if none/expired,
        # or if it belongs to a different account.
        ...

    def scan_page(self, account: Account, content_type: str, cursor: str | None) -> ScanPage:
        # One page of THIS account's content of ONE type. next_cursor=None when exhausted.
        # Every item: platform == self.platform, account_id == account.account_id.
        ...

    def delete(self, item: ContentItem) -> DeleteResult:
        ...

    def verify_deleted(self, item: ContentItem) -> VerifyStatus:
        return VerifyStatus.UNKNOWN   # until you have a reliable check

    def logout(self, account_id: str) -> None: ...   # revoke best effort, delete local session
    def close(self) -> None: ...                      # close clients/browsers
```

## Error contract

Raise these from `models.py`; the shared code reacts uniformly.

| Raise | When | Effect |
|---|---|---|
| `AuthRequired` | No/expired/invalid session; login challenge, CAPTCHA, MFA prompt; account mismatch | Scan aborts or the job stops (`stopped_auth`). Never try to get around it. |
| `RateLimited(retry_after=seconds)` | HTTP 429 or equivalent; pass the platform's hint if any (`Retry-After`, reset headers) | Persisted wait, bounded repeats, then pause. |
| `TransientError` | Network error, timeout, 5xx | Bounded retry with backoff. |
| `PermanentError` (or other `AdapterError`) | Anything retrying will not fix; unexpected response shapes | Scan fails / item `failed`. |

Never put tokens, codes or passwords in exception messages (the executor scrubs, but do not rely on it).

## Content types are scan units

Each key in `content_types` is scanned independently with its own cursor and its own
"complete scan" status. Choose types so that each is one listing endpoint or file. `deletable_types`
must be a subset; types you can list but not delete (e.g. history you can only read) belong in
`content_types` only, and plans will exclude them and say so.

## `item_id`, `version`, `fingerprint`

- `item_id` must be unique within `(platform, account_id)` and stable across scans (a URI, a numeric
  id). If two content types can share a platform id (a post and a bookmark of the same status),
  namespace them (Mastodon uses `fav:<id>`, `bm:<id>`).
- `account_id` must be a stable identifier (a DID, `host:numeric-id`), never a display name or handle.
- `created_at` must be timezone-aware UTC or `None`. Do not invent dates. For things like favourites,
  say in `discovery_notes` which date you report.
- `version` is an opaque marker that **changes when the content changes**: a record CID, an
  `edited_at` timestamp, an ETag. It lets `delete()` detect that the item is not what the user
  previewed. If the platform has no such marker, leave it empty and state that edits cannot be
  detected. `fingerprint()` hashes id, type, version and text, so the executor also skips items whose
  locally stored fingerprint changed after planning.
- In `delete()`, re-read the current version from the platform if you can, and compare with
  `item.version` before deleting; use atomic compare-and-delete (`swapRecord`-style) where offered.
  Return `CHANGED` on mismatch.

## `delete()` classification rules

| Return | Use when |
|---|---|
| `DELETED` | The platform accepted the delete request. |
| `ALREADY_DELETED` | The platform says the item does not exist (404/RecordNotFound) and you are sure it is not a permissions problem. |
| `INACCESSIBLE` | 403/forbidden, item belongs to another account/repo, or the platform says it cannot be deleted by this account. |
| `CHANGED` | The platform's current version differs from `item.version`. |

If a 404 is ambiguous (gone vs not yours), look again (as the Mastodon adapter does) before choosing.
Raise `AuthRequired`, `RateLimited`, `TransientError` or `PermanentError` for everything else. Never
delete anything other than `item`; never loop over listings inside `delete()`.

## `verify_deleted()` semantics

Return `GONE` only when you positively observed absence from the authoritative source (e.g. 404 from
the origin server). Return `PRESENT` if you observed it. Return `UNKNOWN` if the check cannot be made
or is unreliable (cached/CDN reads, eventually consistent search, no endpoint). **Never guess `GONE`.**
`UNKNOWN` leads to `deleted_unverified` in the report, which is the honest result.

## `discovery_notes`

This text is printed by `sociyx platforms`, `scan`, `status` and every report. It must state
plainly: what history is listed and any known or unverified caps, what is NOT covered (DMs, media,
copies elsewhere), date-semantics caveats, pacing consequences, and anything unverified. Do not
overstate. The report disclaimer says a report is not evidence nothing else remains.

## Authentication and session storage

- Store session material only under `paths.secrets_dir("<platform>")`, written with
  `paths.write_private(path, data)` (0600, atomic). Browser profiles go in subdirectories of the same
  place (they are credentials).
- Make `repr()` of session classes hide secrets. Corrupt session files should read as "no session",
  never echoing their contents.
- For OAuth with a loopback redirect, use `sociyx.loopback`:
  - `CallbackServer(preferred_port=None)`: `start()`, use `redirect_uri` (`http://127.0.0.1:<port>/callback`),
    `wait(timeout)` returns the query params or `None`, then `stop()`. It is single-use and logs
    nothing.
  - `open_visible_browser(url, profile_dir=None)` opens a visible Playwright Chromium (persistent
    profile if given), falling back to the system browser; returns a cleanup callable. The URL must
    not contain secrets.
  - Always use PKCE and a random `state`, verify `state` (and issuer where applicable) on the
    callback, and verify the resulting account identity matches what the user asked for.
- Prompt for secrets with `getpass` (inject the prompt function for tests). The user types passwords
  only into the platform's own page or the local hidden prompt.
- `logout()`: revoke remotely best effort (do not fail if offline), then delete local files.

## Tests to write

Use `httpx.MockTransport` (inject `http=`), and tmp `SOCIYX_HOME` (see `tests/conftest.py`; it is
autouse). Cover at least:

- registry entry and class metadata (`content_types`, `deletable_types` subset, `discovery_notes`)
- multi-page scan, cursor termination, mapping of each content type, foreign-account rejection
- `delete()`: success, already deleted, changed version, forbidden/inaccessible, 429 with and without
  a hint, 5xx and transport errors as `TransientError`, 401 as `AuthRequired`
- `verify_deleted()`: gone, present, errors give `UNKNOWN`
- login (including bad state/callback, account mismatch), session file mode 0600, no secrets in error
  messages, logout survives network failure
- Optional live read-only tests in `tests/test_<name>_live.py`, marked with `pytest.mark.live` and
  skipped unless `SOCIYX_LIVE_TESTS=1` (see `tests/test_bluesky_live.py`). They must perform no login
  and no writes.

## Checklist

- [ ] Subclasses `Adapter`; all class attributes set; `deletable_types` is a subset of `content_types`
- [ ] Registered in `adapters/__init__.py`
- [ ] Every returned item has the right `platform`/`account_id`/`content_type`
- [ ] `created_at` UTC-aware or `None`; `item_id` stable and unique
- [ ] `version` detects edits (or the gap is documented)
- [ ] `delete()` classifies per the table; checks version; no collateral deletes
- [ ] `verify_deleted()` returns `UNKNOWN` when it cannot be sure
- [ ] Errors mapped to the four exception types; none contain secrets
- [ ] Sessions under `secrets_dir`, `write_private`, hidden reprs
- [ ] Default `min_delete_interval` below documented limits
- [ ] Honest `discovery_notes`; `docs/platforms/<name>.md` has a "Verified vs unverified" table
- [ ] Mock tests pass; live tests (if any) are read-only and marked `live`
- [ ] ToS and automation risk documented in `docs/PLATFORM_STATUS.md` when applicable
