# Security

sociyx handles credentials for your social media accounts and can permanently delete content. This
page describes where sensitive data lives and how to handle it.

## Local data layout

Everything lives under one directory, created with mode `0700`:

- Default: `$XDG_DATA_HOME/sociyx`, usually `~/.local/share/sociyx`
- Override: set `SOCIYX_HOME`

```
~/.local/share/sociyx/
  sociyx.sqlite3        items, scans, plans, jobs, events (your content as text; private, not secret)
  exports/              plan and export files (mode 0600); contain your content
  locks/                per-job lock files
  fixture/state.json    fake data for the fixture adapter
  secrets/<platform>/   CREDENTIALS - treat like passwords
    bluesky/<did>.json            OAuth tokens + DPoP private key, or app-password JWTs (0600)
    bluesky/browser-profile/      Chromium profile with your Bluesky web login (credential)
    mastodon/sessions/<id>.json   access token + app client secret (0600)
    mastodon/apps/<instance>.json per-instance app registration (0600)
```

Notes:

- `secrets/` files are written atomically with mode `0600`. Browser-profile directories hold cookies
  and are equivalent to a logged-in session: protect them the same way.
- The database and exports contain the text of your own posts. They are not credentials, but they are
  personal data. Exports refuse to be written inside `secrets/`.
- Back up or sync this directory only to places you trust, and never commit it.

## Git exclusions

`.gitignore` excludes `.sociyx/`, `*.sqlite`, `*.sqlite3`, `*.db`, `exports/`, `browser-profile*/`,
`session*.json`, and `*.AppImage`. If you set `SOCIYX_HOME` to a path inside a repository, add it to
`.gitignore` yourself.

## Logs and error output

- `src/sociyx/redact.py` scrubs bearer/DPoP/Basic tokens, JWTs, app-password-shaped strings
  (`xxxx-xxxx-xxxx-xxxx`) and sensitive `key=value` pairs (tokens, passwords, cookies, codes) from log
  records, CLI error messages and stored job details.
- The loopback callback server does not log request URLs (they contain the authorization code) and
  accepts a single callback only.
- Scrubbing is best effort. Review output before sharing it anywhere.

## Local web UI threat model

`sociyx ui` runs an HTTP server on your own computer, so the realistic attackers are **web pages open in your
browser** (including malicious ones trying to reach `127.0.0.1`) and **other local users**, not the internet.

Defences:

- Listens on `127.0.0.1` only, on a random port unless you pass `--port`.
- A one-time launch secret in the URL opens the session; it works once, then the server only accepts the session
  cookie (`HttpOnly`, `SameSite=Strict`, random id). Without the cookie every request, static files included, gets 401.
- DNS rebinding: the `Host` header must be exactly `127.0.0.1:PORT` or `localhost:PORT`.
- Cross-site requests: every POST must carry a same-origin `Origin` (or `Referer`) and the per-session CSRF token;
  there are no CORS headers and no OPTIONS handling, so other sites cannot read responses.
- A strict Content-Security-Policy (no inline script, no external hosts, `frame-ancestors 'none'`), `nosniff`,
  `no-referrer` and `no-store`. All HTML is autoescaped.
- Deletions need the typed `delete N` phrase, checked on the server against the server-side selection. The plan's
  confirmation token never reaches the browser.
- Passwords typed into the Instagram password form are posted to the local server, used for the one login call and
  not stored or logged. Prefer the browser-session method, where you type credentials only on the platform's own page.
- Uvicorn access logs are off; secrets, cookies, CSRF tokens and passwords are never logged; error text is scrubbed.

Limits you should know about:

- The launch URL is passed to your browser as a command-line argument, so another local user could see it in the
  process list for a moment and race you to it. On a shared machine use `--no-open`, copy the printed link yourself,
  and open it immediately. Anyone who can already read your files or run code as you can read the data folder anyway.
- Anyone who obtains the session cookie (for example malware running as you) controls the UI. It is not a
  defence against a compromised account on your computer.
- The Import page reads a folder path you type, on the machine running sociyx; it only parses JSON and adds records
  to the local database.
- Sessions live in memory and end when the server stops or after 12 hours.
- No TLS: traffic never leaves the loopback interface. Do not forward the port, tunnel it or put it behind a proxy.
- This model was tested with Starlette's test client and one real-HTTP check of the demo server. It has not had an
  independent security review, and no real browser or real platform account was involved.

## What not to do

- Never paste passwords, app passwords, access/refresh tokens, authorization codes, session files,
  or browser profile contents into a chat, an issue, or a pull request. Type secrets only at sociyx's
  local hidden prompt, or into the platform's own login page.
- Do not run sociyx against accounts you do not own.

## No bypassing

sociyx never solves or bypasses CAPTCHAs, MFA, "suspicious login" challenges, or rate limits. If a
platform asks for any of these, the job stops (`stopped_auth`) and you resolve it yourself in a
normal browser.

## Removing all local data

```bash
sociyx accounts                      # see what is logged in
sociyx logout bluesky  --account <handle-or-did>   # revokes tokens best effort, deletes the session file
sociyx logout mastodon --account <@you@instance>
rm -rf ~/.local/share/sociyx         # or $SOCIYX_HOME
```

`logout` revokes tokens on the server on a best-effort basis (network failures do not block the local
removal) and deletes the local session file. For Bluesky it does not remove the Chromium profile
under `secrets/bluesky/browser-profile`; the final `rm -rf` does. You can also revoke access on the
platform: Bluesky Settings > Privacy and security > App passwords / sessions; Mastodon Preferences >
Account > Authorized applications.

## Reporting a vulnerability

Open a private security advisory on the project's repository if available, or an issue that says only
"security report, please contact me" with no details. Do not include secrets, tokens, session files,
real account identifiers you want kept private, or exports of your content in any report. Describe
the problem, the version (`pyproject.toml`), and steps to reproduce using the fixture adapter where
possible (`SOCIYX_HOME=$(mktemp -d) sociyx ...`).
