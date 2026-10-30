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
