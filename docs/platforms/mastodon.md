# Mastodon

Adapter: `src/unpost/adapters/mastodon/`. Works against the Mastodon REST API of your **home
instance**. Other servers implementing the Mastodon API may work; only Mastodon is considered tested
(mock tests, plus live read-only runs on mastodon.social).

## Authentication

`unpost login mastodon --account @you@instance` (or just the instance name).

1. Registers an OAuth application on your instance (`POST /api/v1/apps`) with scopes
   `read:accounts read:statuses read:favourites read:bookmarks write:statuses write:favourites
   write:bookmarks`. Registrations are cached per instance and redirect URI in
   `secrets/mastodon/apps/<instance>.json` (0600).
2. **Loopback (default).** Single-use callback server on `127.0.0.1` (port 53682 if free, else
   ephemeral; `/callback`). A visible Chromium window opens on your instance's authorization page; you
   log in and approve there. Authorization code flow with PKCE S256 and a random `state`.
3. If the instance rejects the loopback redirect URI at registration, unpost falls back automatically to
   the manual flow.
4. **`--method oob`.** Redirect `urn:ietf:wg:oauth:2.0:oob`: the instance displays a code, you paste
   it at a hidden prompt.
5. unpost verifies the token (`verify_credentials`), requires the username to match the one you asked
   for (otherwise it revokes the token and refuses), and stores
   `secrets/mastodon/sessions/<instance>_<id>.json` (0600). Account id is `<instance>:<numeric id>`.

`logout` revokes the token (`/oauth/revoke`, best effort) and deletes the session file.

## Content types

| Type | Meaning | Delete action |
|---|---|---|
| `post` | your own statuses, including replies (not boosts) | `DELETE /api/v1/statuses/:id` |
| `boost` | your boosts | `POST /api/v1/statuses/:original_id/unreblog` |
| `favourite` | posts you favourited | `POST .../unfavourite` |
| `bookmark` | posts you bookmarked | `POST .../unbookmark` |

Item ids: post `<id>`, boost `<boost status id>`, favourite `fav:<id>`, bookmark `bm:<id>`. `version`
for posts is `edited_at` (empty if never edited); other types have no version. Text includes the
content warning and the HTML-stripped body. **Dates for favourites and bookmarks are the date of the
status, not of when you favourited/bookmarked it**, so date filters apply to the status date.

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Scan posts/boosts | `GET /api/v1/accounts/:id/statuses?limit=40&max_id=...` (works unauthenticated on most instances) |
| Scan favourites / bookmarks | `GET /api/v1/favourites`, `GET /api/v1/bookmarks` (auth required), `Link: rel="next"` pagination |
| Session check | `GET /api/v1/accounts/verify_credentials` |
| Pre-delete check / verify | `GET /api/v1/statuses/:id` (404 means gone) |
| Delete / undo | see table above |

Pagination follows the `Link rel="next"` header's `max_id`. For posts/boosts, if there is no Link header
at all, the last status id is used as the cursor. An empty page ends the scan.

Delete flow for posts: fetch the status; 404 gives `already_deleted`; a different account id gives
`inaccessible`; `edited_at` different from the scanned version gives `changed`; else `DELETE`. A 404
on the delete is re-checked to tell "gone" from "not yours". Undo actions succeed when the returned
status shows the flag is now `false`; 404 on the original means your boost/favourite/bookmark of it is
gone too (`already_deleted`). Note that unreblog on a post you did not boost succeeds server-side, so
such an item may be reported as deleted rather than already deleted.

## Rate limits and pacing

Per the Mastodon docs, deleting a status or unreblogging is limited to 30 per 30 minutes per account.
Default pacing is therefore **60 seconds** between delete requests (about 60 per hour); 10,000 posts
take roughly a week. Unfavourite/unbookmark fall under the general limit (300 requests per 5 minutes,
seen live as `X-RateLimit-Limit: 300`) but use the same default pacing; override with `--delay`.
429 responses honour `Retry-After` or `X-RateLimit-Reset` (ISO 8601). Instances can change limits.

## Verification method

Posts: `GET /api/v1/statuses/:id` returning 404 means `gone`; any 200 means `present`. Boosts,
favourites and bookmarks: fetch the original status and check that `reblogged` / `favourited` /
`bookmarked` is `false` (`gone`), `true` (`present`), or missing (`unknown`; reported as unverified).
Errors give `unknown`.

## Coverage and gaps

Not covered: direct messages (may or may not be listed depending on server version; treat as not
covered), media attachments (the `delete_media` option of Mastodon 4.4+ is not used), poll votes,
follows, lists, filters, profile fields, and federated copies. Other servers receive a `Delete`
activity but may keep copies; this cannot be verified from your instance.

## Verified vs unverified

| Item | Status | Evidence |
|---|---|---|
| Unauthenticated account lookup and statuses with `Link rel=next` pagination | Verified (live on mastodon.social, 2026-10-30) | live test |
| `X-RateLimit-Limit: 300`, `X-RateLimit-Reset` ISO 8601 | Verified (live) | manual run |
| Nonexistent status returns 404 `{"error":"Not Found"}` | Verified (live) | live test |
| Delete/unreblog limit of 30 per 30 min; PKCE S256; per-instance app registration | Docs only | Mastodon docs |
| Loopback redirect accepted per instance | Unverified (OOB fallback exists) | |
| Favourites/bookmarks pagination against a live server | Unverified | mock tests only |
| Unreblog / unfavourite / unbookmark behaviour live | Unverified | mock tests only |
| Login, token verification, revocation, actual deletes | Unverified | mock tests only |
| Any cap on listable history on specific instances | Unverified | |
| DM listing; removal of remote federated copies | Unverified / not covered | |
