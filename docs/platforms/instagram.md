# Instagram (private mobile API via instagrapi; unofficial)

Adapter: `src/sociyx/adapters/instagram/`. Uses Instagram's private mobile API through the
community `instagrapi` library (`uv sync --extra instagram`, verified against 3.2.2).
This is **unofficial**: it conflicts with Instagram's Terms of Use and can trigger
challenges or action blocks. You accepted that risk for your own data; sociyx mitigates it
with very slow pacing, stops on any challenge/login prompt, and never tries to bypass one.

## Risk note

Automating Instagram through the private API can get an account restricted, temporarily
blocked or challenged, and in the worst case disabled. The account owner accepted that
risk for their own account. sociyx does not claim that Instagram's Terms of Use do not
apply to you, and the project does not endorse that argument: acting on your own data
does not by itself make automation permitted. Slow pacing lowers the chance of a block
but does not remove it. Never share passwords, session files or tokens in chats or issues;
sociyx only asks for them at local hidden prompts.

## Install

```bash
uv sync --extra instagram
uv run playwright install chromium   # only needed for the browser login
```

## Authentication

Three methods (`sociyx login instagram [--method browser|password|sessionfile]`):

1. **Browser (default).** A visible Chromium window opens on Instagram's login page where
   **you** log in (2FA/challenges included). sociyx never types into the page and never
   touches CAPTCHAs; it only polls the browser cookie jar until a `sessionid` appears,
   then exchanges it via `login_by_sessionid`. If the mobile API refuses the browser's
   `sessionid` you get guidance to use `--method password`.
2. **Password.** You type your username at a visible prompt (or pass `--account`) and your
   password and optional 2FA code at hidden prompts. `with_challenge_flow=False` is set on
   every client so the library never auto-resolves challenges or CAPTCHAs; a challenge
   surfaces as `AuthRequired` and stops the job. Complete it in the official app or
   website, then log in again. Never type passwords or codes into a chat.
3. **Session file.** An existing instagrapi settings JSON (`--method sessionfile`,
   path via `--account PATH` or `--session-file PATH`). Useful when you already have a working session.

Session settings (tokens, cookies, device ids) are stored in
`secrets/instagram/<account_id>.json` (0600). Browser profiles live under
`secrets/instagram/browser-profile`. `logout` deletes the session file and profile
(best effort, never fails offline).

## Content types

| Type | Meaning | Delete action | Verify |
|---|---|---|---|
| `post` | your feed posts (carousels included; reels excluded) | `media_delete` (+ `media_info_v1` pre-check, caption-change detection) | `media_info_v1` 404 → gone |
| `reel` | your reels | `media_delete` | same as post |
| `story` | your live + archived stories | `story_delete` | live check + `media_info_v1`; archived-but-resolvable → `UNKNOWN` |
| `like` | posts you liked (via `collection_medias_v1_chunk("liked")`) | `media_unlike` | `has_liked` flag (`UNKNOWN` when absent) |
| `saved` | posts you saved (all collections) | `media_unsave` (removes from **all** collections) | `UNKNOWN` (viewer-saved flag not exposed) |
| `dm` | direct messages **you** sent (unsent for everyone) | `direct_message_delete` after locating the message in-thread and verifying its sender | re-reads the thread (`found`→present, `absent`→gone) |
| `comment` | comments you wrote on **your own** posts (top-level) | `comment_bulk_delete` | re-reads comment pages |
| `comment_export` | comments you wrote on **other people's** posts, from a data export | fuzzy resolve (owner's recent posts, ±120 s, exact text) then `comment_bulk_delete` | only when resolved in-run, else `UNKNOWN` |

Item ids: media `"<pk>_<owner>"`, likes/saves `"like:<pk>"` / `"saved:<pk>"` (so API scans
and export imports collapse), comments `"comment:<media_id>:<pk>"`, export comments
`"cexp:<sha>"`, DMs `"dm:<thread>:<message>"`. `version` is `taken_at + caption hash` for
media (caption edits detected); other types carry no version and rely on the local
fingerprint check.

DMs: only inbox threads reachable via `direct_threads_chunk` (message requests / pending
not covered); only your sent messages are listed and unsent. Unsending removes them for
the other party, but they may already have seen them, got notifications, or exported them.
Instagram's unsend can report success without effect (see instagrapi issue #890), so
sociyx never trusts the unsend response. Before unsending it locates the message in the
thread (up to `dm_lookup_pages=3` pages, newest first) and checks the sender is you; after
unsending, verification re-reads the thread: message still there is `present`, message
gone is `gone`, message not located is `UNKNOWN`. A message not found in the lookup window
is not unsent.

Comments on other people's posts: **no API lists them**. Request your export (Settings →
Accounts Center → Your information and permissions → Download your information, JSON) and
run `sociyx import instagram <export_dir> [--account <id>]` (`--account` takes the numeric
id shown by `sociyx accounts`). Import only writes records to the local database. It reads
`liked_posts.json`, `saved_posts.json`, `post_comments*.json` and `reels_comments*.json`
anywhere under the folder (likes and saved become `like` / `saved` items that merge with
API scans; comments become `comment_export`). Unrecognized layouts are logged and skipped. Resolving each export comment
is fuzzy; when not found it reports `inaccessible` with manual steps
(Settings → Your activity → Comments).

NOT covered: likes on comments, story replies, tags, replies to comments,
follows/followers. Instagram may retain backups or copies after deletion; deletion is not
instantly visible everywhere.

## Discovery limits

The private API does not give a complete inventory, so a scan is never proof that
everything is gone. Do not treat a finished cleanup as a complete wipe.

- Posts, reels, stories (live and archive): listed through the profile and archive
  endpoints. How far back they go is **unverified**.
- Likes and saved: listed via the `liked` and `saved` collection feeds. The depth cap on
  likes is **unverified**; older likes may need the data export.
- DMs: only inbox threads reachable through `direct_threads_chunk`. Message requests and
  pending threads are not covered. Only messages you sent are listed.
- Comments: only top-level comments on your own posts are listed. Comments on other
  people's posts cannot be listed at all; they come only from the data export, and
  resolving them is fuzzy.
- Not covered: likes on comments, story replies, tags, replies to comments,
  follows/followers, and server-side backups.

## Endpoints / library calls used

All through `instagrapi.Client` (private `direct_v2/`, `media/`, `collections/`,
`archive/` endpoints under the hood):

| Purpose | Call |
|---|---|
| Scan posts | `user_medias_paginated` (filters `product_type == "clips"` out) |
| Scan reels | `user_clips_paginated_v1` |
| Scan stories | `user_stories` (live) + `archive_stories_v1(0)` (archive) |
| Scan likes / saved | `collection_medias_v1_chunk("liked" / "saved")` |
| Scan DMs | `direct_threads_chunk` + `private_request("direct_v2/threads/{id}/")` |
| Scan own comments | `user_medias_paginated` + `media_comments_chunk` per post |
| Pre-delete / verify media | `media_info_v1` |
| Delete / unlike / unsave | `media_delete`, `story_delete`, `media_unlike`, `media_unsave` |
| Delete comment | `comment_bulk_delete` |
| Unsend DM | `direct_message_delete` |
| Resolve export comment | `user_id_from_username` + `user_medias_paginated` + `media_comments_chunk` |

Delete flow: re-read the target first (`Gone` → `already_deleted`); media checks owner
and caption version (`changed` / `inaccessible`); DM checks the sender is you;
comment_export resolves fuzzily or returns `inaccessible` with manual guidance.

## Rate limits and pacing

Instagram publishes no numbers for these endpoints. The adapter assumes the worst. Class
variables on `InstagramAdapter`:

| Variable | Value | Effect |
|---|---|---|
| `min_delete_interval` | 30.0 | base seconds between delete requests |
| `pacing_jitter` | 0.5 | up to +50% added, so 30–45 s |
| `think_pause_every` | 25 | long pause after every 25 requests |
| `think_pause_seconds` | (60.0, 180.0) | the pause is 1–3 min |
| `daily_write_cap` | 200 | delete requests per rolling 24 h per account; `--daily-cap` can only lower it (use e.g. `--daily-cap 100` for DM jobs) |
| `scan_delay` | (8.0, 20.0) | jittered seconds between scan pages |

The client is also created with `delay_range=[1, 3]`.

A 1,000-item cleanup takes ~10+ hours plus pauses.

## Error mapping

`adapters/instagram/errors.py` translates instagrapi exceptions:

| instagrapi exception | sociyx error | Effect |
|---|---|---|
| `FeedbackRequired`, `SentryBlock`, `ProxyAddressIsBlocked` | `ActionBlocked` (cool-down 86,400 s) | job stops with a 24 h cool-down; `--ignore-cooldown` only if you know the block was cleared |
| `LoginRequired`, `ClientLoginRequired`, `ChallengeError`, `AccountSuspended`, `CaptchaChallengeRequired`, `ReloginAttemptExceeded`, `TwoFactorRequired`, `BadPassword`, `BadCredentials`, `RecaptchaChallengeForm` | `AuthRequired` | job stops; resolve it in the official app or website, then log in again; nothing is retried or bypassed |
| `PleaseWaitFewMinutes`, `RateLimitError`, `ClientThrottledError` | `RateLimited` (retry after 900 s, escalating, persisted across restarts) | waits, then retries |
| connection, timeout, incomplete-read, JSON-decode errors | `TransientError` | retried up to the limit |
| `MediaNotFound`, `StoryNotFound`, `InvalidMediaId`, `MediaUnavailable`, `CommentNotFound`, `DirectThreadNotFound`, `DirectMessageNotFound`, `CollectionNotFound` | `Gone` | treated as already deleted |
| any other `ClientError`, `ValidationError` | `PermanentError` | item fails |

## Verification method

Post/reel: `media_info_v1` 404 → `gone`. Story: live-list + `media_info_v1`
(archived-but-resolvable → `UNKNOWN`). Like: `has_liked` flag. Saved: always `UNKNOWN`.
DM: re-read the thread. Comment: re-read comment pages (3 pages max, else `UNKNOWN`).
Network failures during checks give `UNKNOWN` (`deleted_unverified`).

## Coverage and gaps

See table above. Message requests, story replies, comment likes, tags, follows, and any
server-side backups are not covered. Reports state they cover only the planned items.

## Verified vs unverified

| Item | Status | Evidence |
|---|---|---|
| Scan and list on a real account | Verified by the user | user report |
| DM unsend, with re-read verification | Verified by the user | user report |
| `instagrapi==3.2.2` exposes every method/exception the adapter calls, with compatible signatures | Verified (local, 2026-10-30) | signature check against installed package |
| Media mapping, ownership filtering, delete classification, DM sender checks, error mapping, session 0600, export parsing | Verified (mock) | `tests/test_instagram.py` |
| `import instagram` ingests the export files above | Verified (mock) | CLI import test |
| Real delete of posts, reels, stories | Not yet live-verified | |
| Real unlike (likes), unsave (saved) | Not yet live-verified | |
| Real comment deletion (own posts, and `comment_export`) | Not yet live-verified | |
| Export import against a real export | Not yet live-verified | |
| Login inside the TUI (suspend and Playwright) | Not yet live-verified | |
| Likes depth cap; archive pagination; DM thread reach | Unverified | |
| Rate-limit numbers; block behaviour under real load | Unverified | |
| Propagation / backup retention after deletion | Unverified | |
