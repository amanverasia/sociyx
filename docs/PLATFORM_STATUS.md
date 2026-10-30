# Platform status and research

Platforms that are **not implemented**, with the recommended approach, risks and what is verified.
"Verified" means read in current official documentation or observed by us; "Unverified" means
secondhand, undocumented or not checked. The only real-account results are for Instagram (see below);
nothing else here has been tested against a real account. Facts about pricing, limits and terms
change; re-check before building.

Implemented: Bluesky, Mastodon and Instagram (see [platforms/bluesky.md](platforms/bluesky.md),
[platforms/mastodon.md](platforms/mastodon.md), [platforms/instagram.md](platforms/instagram.md))
and the fixture adapter.

## Ranked next candidates

1. Threads (official API, clean delete)
2. Telegram (official user API, verifiable deletes)
3. Tumblr (official API delete)
4. X (official API, paid; awaiting user decision)
5. Reddit (export import for discovery; deletion path undecided)

## X / Twitter

- Official API is pay-per-use only (docs.x.com pricing): owned reads about $0.001/resource;
  "Interaction: Delete" about $0.010/request. **Verified** (docs). Mapping that line item to post
  deletion is our inference.
- `DELETE /2/tweets/:id`: **verified**. `GET /2/users/:id/tweets` with pagination: **verified**.
- Rate limit for delete: 50 per 15 minutes per user. **Verified**.
- The "3,200 most recent posts" timeline cap: **unverified** in current docs. Older posts need IDs from
  the user's official X archive (`tweets.js`).
- Web-UI automation (as Cyd and CleanMyPosts do) conflicts with X's terms on automation/scraping.
  **Unverified**: the ToS page could not be fetched; this is secondhand.
- Recommended: official API with the user's own developer app via OAuth2 PKCE loopback, plus archive
  import for discovery. It costs money; awaiting user decision.
- ToS risk: low via API, high via UI automation.

## Reddit

- Official docs returned 403 to our fetcher. Secondhand reports say self-service API access closed
  in Nov 2025 and now requires approval. **Unverified**.
- Listings are reportedly capped at about 1,000 items. **Unverified**.
- The GDPR data export (CSV) contains full IDs (Shreddit uses this). **Unverified here**, reported by
  the Shreddit project.
- Recommended: data-export import for discovery. Deletion path undecided: API approval versus browser
  automation with ToS risk.

## Instagram

Implemented via the unofficial private mobile API (`instagrapi`, `uv sync --extra instagram`).
See [platforms/instagram.md](platforms/instagram.md).

- Graph API media `DELETE` exists only for professional accounts via Facebook Login. **Verified**
  (docs; the docs are inconsistent between `DELETE` and `POST`). Comment deletion works only on your
  own media. Nothing official for your likes or comments on others' posts.
- Basic Display API ended Dec 4, 2024. **Verified**.
- Every `instagrapi==3.2.2` method the adapter calls was signature-checked locally. **Verified**
  (local, mock tests only; no real account).
- Live status (reported by the user on a real account): scan, list and DM unsend work. **Not yet
  live-verified**: deleting posts, reels, stories; likes (unlike); saved (unsave); comments; export
  import; login through the TUI. Bluesky and Mastodon real login and deletion are also not yet
  live-verified.
- Contents: posts, reels, stories (live + archive), likes, saved, your sent DMs (unsent),
  comments on your own posts, plus export-only comments on others' posts
  (`unpost import instagram <export_dir>`).
- Pacing: 30–45 s between writes, 1–3 min pause every 25, 200/day cap. Action blocks stop the job
  with a 24 h cool-down.
- ToS risk: moderate-high; the user accepted it for their own data. Selectors are not involved
  (API, not browser automation), but challenges/blocks remain possible.

## Threads

- Official API supports `DELETE` with the `threads_delete` permission; 100 deletes per day. **Verified**
  (docs).
- OAuth redirect must match a registered URI; loopback acceptance is **unverified**.
- Good next candidate.

## Facebook

- No API for personal posts. Activity Log UI automation only. Moderate ToS risk. Not recommended
  without further review.

## Others

| Platform | Summary |
|---|---|
| Telegram | Official MTProto user API; deletes are verifiable. Needs an API id/hash from the user. |
| Tumblr | Official API supports deleting posts. |
| YouTube | `comments.delete` exists, but no API lists your comments across videos; use Google Takeout for discovery. |
| Slack | `chat.delete` with a user token; depends on workspace policy. |
| LinkedIn, TikTok | No viable official path; automation is prohibited or risky. Not recommended. |
| Discord | User-token automation violates ToS. Not planned. (Reference: Undiscord, see REFERENCES.) |

## Discovery limits to keep in mind

Platforms may expose only recent history through their APIs, so an export-based import is often needed
to reach old content. Reports always note that deleting what you can list is not proof that nothing
else remains.
