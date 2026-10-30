# Bluesky (AT Protocol)

Adapter: `src/unpost/adapters/bluesky/`. Records live in your own repository on your PDS; unpost lists
and deletes them there.

## Authentication

Identity resolution (`identity.py`): handle to DID via the public AppView
(`https://public.api.bsky.app`, `com.atproto.identity.resolveHandle`) with a `.well-known` fallback;
DID document from plc.directory (or `did:web`) gives the PDS URL; the handle is checked against the
DID document's `alsoKnownAs` and a warning is logged if not bidirectionally verified.

**OAuth (default, `--method oauth`).** Loopback public client with PAR, PKCE S256 and DPoP (ES256):

1. Discover the authorization server from the PDS (protected-resource metadata, then auth server
   metadata). For Bluesky-hosted PDSs the server is `https://bsky.social`.
2. Start a single-use callback server on `127.0.0.1` (ephemeral port, `/callback`).
3. `client_id` is `http://localhost?redirect_uri=<callback>&scope=<scope>`; scope is
   `atproto transition:generic` (transitional; may change).
4. Push the authorization request (PAR) with a DPoP proof, retrying once with the server-provided nonce.
5. A visible Chromium window opens (persistent profile in `secrets/bluesky/browser-profile`). You
   log in, pass 2FA and approve on Bluesky's page. unpost never sees your password.
6. The callback must carry the expected `state` and `iss`; the code is exchanged for tokens; the
   returned `sub` must equal the DID you asked for.
7. Session (access/refresh tokens, DPoP key, nonces) is saved to `secrets/bluesky/<did>.json` (0600).
   Access tokens are refreshed on 401 and the refreshed tokens are persisted.

**App password (`--method app-password`).** You create an app password in Bluesky (Settings >
Privacy and security > App passwords) and type it at a hidden prompt. unpost calls
`com.atproto.server.createSession` on your PDS; if the server demands an emailed code
(`AuthFactorTokenRequired`) you are prompted for it. The DID in the response must match. Tokens
(`accessJwt`/`refreshJwt`) are stored 0600 and refreshed with `refreshSession`.

`logout` revokes the OAuth refresh token (or calls `deleteSession`) best effort and deletes the local
session. The Chromium profile stays until you remove it.

## Content types

| Type | Collection | Notes |
|---|---|---|
| `post` | `app.bsky.feed.post` | posts, replies, quote posts; text is the record text |
| `like` | `app.bsky.feed.like` | URL points at the liked post when it is a post |
| `repost` | `app.bsky.feed.repost` | |
| `follow` | `app.bsky.graph.follow` | |
| `block` | `app.bsky.graph.block` | |
| `list` | `app.bsky.graph.list` | |
| `listitem` | `app.bsky.graph.listitem` | |

All are deletable. `item_id` is the record `at://` URI, `version` is the record CID, `created_at` is
the record's `createdAt`.

## Endpoints used

| Purpose | Endpoint |
|---|---|
| Scan | `com.atproto.repo.listRecords` on the PDS (`limit=100`, `cursor`); unauthenticated is fine for public repos |
| Session check | `com.atproto.server.getSession` |
| Pre-delete check / verify | `com.atproto.repo.getRecord` (missing record: HTTP 400 `RecordNotFound`) |
| Delete | `com.atproto.repo.deleteRecord` with `swapRecord` = scanned CID |
| Password login | `createSession`, `refreshSession`, `deleteSession` |
| OAuth | auth server PAR, authorization, token and revocation endpoints |

`delete()` first fetches the record: not found gives `already_deleted`; CID different gives
`changed` (skipped); otherwise deletes with `swapRecord`, and an `InvalidSwap` response is
re-classified by looking again (gone: already deleted; else changed). 403 maps to `inaccessible`.

## Rate limits and pacing

From the official docs (not measured under load): 5,000 write points/hour and 35,000/day per account,
a delete costs 1 point; 3,000 requests per 5 minutes per IP (the PDS returned `ratelimit-limit: 3000`
in our live run); `createSession` 30 per 5 minutes and 300 per day. Default pacing is 1 delete per
second (about 3,600/hour, below the hourly budget; each delete also issues a `getRecord` read and a
verification read). 429 responses honour `Retry-After` / `ratelimit-reset`. A 10,000-record cleanup
is expected to take a few hours; the 35,000/day budget is the practical ceiling.

## Verification method

After a delete, unpost reads the record directly from the PDS via `getRecord` (the source of truth)
and reports `deleted_verified` on `RecordNotFound`, retrying a few seconds apart if it still appears.
Network failures during the check give `UNKNOWN` (`deleted_unverified`). AppView, search and feeds may
lag behind the PDS.

## Coverage and gaps

Not covered: direct messages (`chat.bsky` is a separate service), the profile record, threadgates,
postgates, starter packs and other collections, media blobs (unreferenced after deletion; when the PDS
garbage-collects them is PDS-dependent), copies on relays, AppViews and third-party archives, other
people's quotes, replies and screenshots. Deletions propagate over the firehose; third parties are
expected, not guaranteed, to honour them.

## Verified vs unverified

| Item | Status | Evidence |
|---|---|---|
| `listRecords` on a PDS, `limit` up to 100, cursor, no auth needed | Verified (live, 2026-10-30) | live tests |
| AppView returns 501 for `listRecords` | Verified (live) | manual run |
| `getRecord` for a missing record returns 400 `RecordNotFound` | Verified (live) | live test |
| `deleteRecord` supports `swapRecord` / `InvalidSwap` | Verified (docs only) | atproto docs |
| Loopback `client_id` accepted by bsky.social PAR with DPoP; nonce challenge (400 `use_dpop_nonce` + `DPoP-Nonce`) then success on retry | Verified (live, no user login) | live test |
| Rate-limit numbers | Documentation only; PDS `ratelimit-limit: 3000` seen live | |
| Token exchange, DPoP-bound resource requests, refresh, revocation | Unverified (needs a real account) | mock tests only |
| App-password login incl. emailed-code flow | Unverified | mock tests only |
| Actual deletes | Unverified | mock tests only |
| Visible-browser login on Linux Mint | Unverified | |
| AppView propagation timing; blob garbage collection | Unverified | |
| Any practical cap on listable history | Unverified | |
| Scope `atproto transition:generic` remains valid | Transitional; may change | |
