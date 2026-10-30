# sociyx

Website: <https://sociyx.com/>

A local-first, open-source (MIT) command-line tool for bulk cleanup of your **own** social media
history: scan what you have posted, filter it, review an exact preview, then delete it with
verification and a report.

## Start here: how do I use it?

**This is a terminal app.** Run `uv run sociyx` with no arguments in a terminal for the
[guided UI](#guided-tui) that walks you through login, scan, selecting items, review and deletion.
Everything is also available as plain commands (below), which are what scripts and the
step-by-step instructions use. Nothing is deleted just by logging in, scanning, or planning.

The usual flow is **log in → scan → list/filter → create a plan → review → delete → report**.
Run commands from this project folder. Use `uv run sociyx`, including the `uv run` prefix,
so the project dependencies are available.

### Set up this copy

You already have the project, so you do not need to clone it again:

```bash
cd "/home/amanverasia/Projects/personal_projects/misc/redact.dev - rev"
uv sync --extra instagram
uv run playwright install chromium
uv run sociyx --help
```

Python 3.11 or newer is required. If `uv` is missing, follow the installation instructions
below. If Chromium reports missing system libraries, run
`uv run playwright install-deps chromium` and retry.

### Practice with fake data first

These commands use the fake `fixture` account and do not contact a social platform:

```bash
uv run sociyx login fixture --account alice
uv run sociyx scan fixture
uv run sociyx list fixture --type post --limit 10
uv run sociyx plan create fixture --type post
uv run sociyx plan list
```

The output contains a **plan ID**. In the next commands, replace `PLAN_ID` with that number
(without adding brackets):

```bash
uv run sociyx plan show PLAN_ID --all
uv run sociyx plan execute PLAN_ID
```

The last command is a dry run: it shows what would happen and prints the live command.
You can stop here after learning the workflow.

### Use Instagram, step by step

Instagram support uses an unofficial API. Real Instagram login and deletion have not yet
been verified against a real account. Start with a small selection you have reviewed.

1. **Log in.** Run the following, then finish login in the browser window yourself:

   ```bash
   uv run sociyx login instagram
   ```

   If the browser session is rejected, use the password method. Replace `YOUR_USERNAME`
   with your Instagram username; the password is requested privately in the terminal:

   ```bash
   uv run sociyx login instagram --method password --account YOUR_USERNAME
   ```

2. **Fetch a small preview of your posts.** This reads data; it does not delete anything:

   ```bash
   uv run sociyx scan instagram --type post --max-pages 1
   uv run sociyx list instagram --type post --limit 20
   ```

   This is only a partial scan. Repeat the scan command to fetch the next page, or omit
   `--max-pages 1` to continue through the remaining pages. Scanning can be slow.

3. **Choose what to delete.** For example, preview posts containing a distinctive word:

   ```bash
   uv run sociyx list instagram --type post --keyword "YOUR_CHOSEN_WORD" --limit 100
   uv run sociyx plan create instagram --type post --keyword "YOUR_CHOSEN_WORD"
   ```

   Replace `YOUR_CHOSEN_WORD` with a word in posts you intend to remove. The plan includes
   **all matching stored items**, even if `list --limit` showed fewer. Choose filters that
   produce only 1–3 items for your first real run. You can also use `--since`, `--until`,
   and `--exclude-keyword` (see the filter reference below).

4. **Review the entire plan and dry run.** Replace `PLAN_ID` with the ID just printed:

   ```bash
   uv run sociyx plan show PLAN_ID --all
   uv run sociyx plan execute PLAN_ID
   ```

   Check the account, item count, and every item. If the selection is wrong, create a new
   plan with narrower filters. Creating another plan does not execute the old one.

5. **Delete only when ready.** Copy the exact live command printed by the dry run. Its
   structure is shown below; replace both placeholders with your actual values:

   ```bash
   uv run sociyx plan execute PLAN_ID --live --confirm 'EXACT_TOKEN_FROM_YOUR_PLAN'
   uv run sociyx report PLAN_ID
   ```

   **The command containing `--live` performs real deletion.** Instagram writes are paced
   30–45 seconds apart, with additional pauses and a 200-per-day cap. Keep the terminal
   open while it runs. Press Ctrl+C once to request a pause; see the resume instructions below.

For other Instagram content, change `--type post` during scanning and filtering:

| Type | Action when deleted |
|---|---|
| `post`, `reel`, `story` | Delete your posts, reels, or stories |
| `like` | Unlike posts you liked |
| `saved` | Unsave posts from all collections |
| `dm` | Unsend your messages for everyone in the conversation |
| `comment` | Delete your comments on your own posts |
| `comment_export` | Attempt to locate and delete comments from an imported export |

Comments on other people's posts need your Instagram JSON data export first. Unzip it,
then import the extracted folder. `ACCOUNT_ID` is the numeric Instagram ID shown by
`uv run sociyx accounts`, rather than your username:

```bash
uv run sociyx accounts
uv run sociyx import instagram "/path/to/extracted/export" --account ACCOUNT_ID
uv run sociyx list instagram --type comment_export
```

Importing only adds records to the local database. Use the same plan/review/delete steps
with `--type comment_export` afterward. Resolving these comments is best-effort; some may
need manual deletion. See [Instagram details](docs/platforms/instagram.md).

For Bluesky or Mastodon, use the login examples below, then the same scan, list, plan,
review, execute, and report flow with that platform name.

## Guided TUI

```bash
uv run sociyx            # opens the guided UI when run in a terminal (no args)
uv run sociyx tui        # same thing
uv run sociyx tui --demo # fake accounts and data in a throw-away folder; real data untouched
```

Flow: **Home** (accounts, add account, resume jobs) → **Account** (what is stored per content type,
Platform notes) → **Scan** → **Browse & select** → **Review** → **Run** → **Results**. Nothing is deleted
until you type `delete N` (N = the number of items) on the Review screen. Scans run in the background
with a Stop button; a running deletion can be paused (`p`), cancelled, or resumed later from Home. If you
quit during a run, the UI asks to pause the job first.

| Key | Where | Action |
|---|---|---|
| `?` | everywhere | key help |
| `Esc` | everywhere | back / close |
| `Ctrl+Q` | everywhere | quit (asks first if a scan or deletion is running) |
| `a`, `r`, `q` | Home | add account, resume jobs, quit |
| `s`, `b`, `p` | Account | scan, browse and select, platform notes |
| `Space` | Browse | tick or untick the row |
| `a` / `n` / `i` | Browse | select / deselect / invert every row currently shown |
| `x` | Browse | clear the whole selection (also rows hidden by filters) |
| `/` | Browse | search text, case-insensitive, live |
| `d` | Browse | date range (same rules as `--since` / `--until`) |
| `c` | Browse | select the whole highlighted conversation (direct messages) |
| `Enter` | Browse | full item detail |
| `r` | Browse | review the selection |
| `p` | Run | pause after the current item |

Your selection survives changing filters; the footer shows `N selected / M shown / T total` and how many
selected items are currently hidden. Login prompts and browser windows use your real terminal: the UI
steps aside while you log in and comes back afterwards. The `fixture` platform only appears in the
account picker in demo mode (or with `SOCIYX_TUI_DEMO=1`).

Known limits:

- Browse shows at most 5,000 rows (the newest); a footer says so. `a` still selects every matching
  item, not only the displayed ones. Use `/` and `d` to narrow.
- "Save plan and exit without deleting" on the Review screen creates a plan but no job. Run it later from the CLI with the
  `plan execute ... --live --confirm ...` command the dialog prints.
- Pressing Ctrl+C at a hidden password prompt may swallow a keypress; press it again if nothing
  happens.
- Logging in through the TUI (it suspends the UI and hands the terminal and Playwright browser to
  the adapter) has not been tested against a real account. Use `sociyx login` in a normal terminal
  if it misbehaves.

## What it is and is not

- It runs on your machine. Your data, sessions and exports stay in a private local directory.
- It is inspired by the idea behind Redact.dev but is independent: no Redact code, branding or assets
  are used. See [docs/REFERENCES.md](docs/REFERENCES.md).
- It has no subscription, no hosted backend, no telemetry and no AI. The only network traffic is to
  the platforms you choose to use (plus Bluesky identity lookups at plc.directory / the public
  AppView).
- It is not a tool for deleting other people's content, evading platform limits, or bypassing
  CAPTCHAs/MFA. It stops when a platform challenges it.
- It cannot delete copies it does not control: other people's quotes and screenshots, caches, search
  engines, archives, federated copies. Reports say so.
- Status: early (v0.1). The only real-account results so far are for Instagram (scan, list and DM
  unsend, reported by the account owner); everything else is untested against real accounts. See [docs/VERIFICATION.md](docs/VERIFICATION.md). Your first real run should be a
  plan of 1-3 items that you have reviewed.

## Safety model in brief

1. Scanning is read-only. Deleting is a separate step.
2. `plan create` freezes a selection, writes an export of exactly those items, and prints a
   confirmation token. Nothing is deleted.
3. `plan execute` is a dry run by default. `--live` needs `--confirm` with the exact token
   (`plan_id:account_id:count:digest8`).
4. Before deleting, sociyx checks the token, that the export file is unmodified, that the plan items
   still match their digest, and that the logged-in account is the plan's account.
5. Each item is re-checked against what you previewed; edited items are skipped, not deleted.
6. Outcomes are never overstated: "deleted and verified" differs from "deleted, unverified".
7. Auth problems, challenges and repeated rate limits stop the job; nothing is bypassed.
8. Jobs can be paused, resumed after a crash, or cancelled, and an interrupted request is verified
   before anything is retried.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Supported platforms

| Platform | Status | Notes |
|---|---|---|
| Bluesky | Working | Read-only scan verified live against public data. Real login and deletion **not yet live-verified**. [Details](docs/platforms/bluesky.md) |
| Mastodon | Working | Same caveats as Bluesky. [Details](docs/platforms/mastodon.md) |
| Instagram | Working (unofficial API) | Scan, list and DM unsend verified on a real account; other deletions **not yet live-verified**. Needs `uv sync --extra instagram`. Very slow pacing (30–45 s/write, 200/day). [Details](docs/platforms/instagram.md) |
| Fixture | Dev/test only | Fake local data; for demos and tests. |
| X, Reddit, Threads, others | Researched, not implemented | [docs/PLATFORM_STATUS.md](docs/PLATFORM_STATUS.md) |

`sociyx platforms` prints content types, deletable types and history-coverage notes for each
adapter.

## Installation on Linux Mint

Written for Linux Mint 21/22 (Ubuntu base). Playwright does not officially list Mint, but it is
Ubuntu-based, so it is expected to work; this has not been verified by the developers.

```bash
sudo apt update
sudo apt install python3 git curl

# Install uv (Python project manager). Review the script first if you prefer:
#   curl -LsSf https://astral.sh/uv/install.sh | less
curl -LsSf https://astral.sh/uv/install.sh | sh
# Alternative without piping a script to the shell:
#   sudo apt install pipx && pipx install uv
# Open a new shell (or `source ~/.local/bin/env`) so `uv` is on PATH.

git clone https://github.com/amanverasia/sociyx.git
cd sociyx
uv sync                              # Python >= 3.11 required; uv can fetch one if needed
uv run playwright install chromium   # visible browser used for logins
uv run sociyx --help
```

If Chromium fails to start because of missing system libraries:

```bash
uv run playwright install-deps chromium   # uses sudo to install apt packages
```

If Playwright's Chromium cannot be started at all, sociyx falls back to opening your default browser
for OAuth logins.

### Where data lives

`~/.local/share/sociyx` (or `$XDG_DATA_HOME/sociyx`). Override with `SOCIYX_HOME=/some/dir`.

Upgrading from the earlier name (`unpost`)? sociyx keeps using an existing `~/.local/share/unpost`
folder (and honors `UNPOST_HOME` if `SOCIYX_HOME` is unset) until you switch. Nothing is moved or
copied automatically because the folder contains credentials. To switch, rename it yourself:
`mv ~/.local/share/unpost ~/.local/share/sociyx`.
Directories are created `0700`, session and export files `0600`. See [SECURITY.md](SECURITY.md).

## Quick start

### 1. Try it safely with the fixture adapter

Everything below uses fake data. Use a throwaway home if you like: `export SOCIYX_HOME=$(mktemp -d)`.

```bash
uv run sociyx login fixture --account alice
uv run sociyx scan fixture
uv run sociyx list fixture --type post --since 2021-01-01 --until 2021-12-31 --limit 5
uv run sociyx export fixture --type post --format csv --out /tmp/alice-posts.csv

uv run sociyx plan create fixture --type post --since 2021-01-01 --until 2021-12-31 --exclude-keyword crypto
uv run sociyx plan execute 1                      # dry run, deletes nothing
# Copy the live command printed by the dry run only when ready.
uv run sociyx report 1
```

The token printed by `plan create` differs per run (it includes a digest of the items).

### 2. Bluesky

```bash
uv run sociyx login bluesky --account you.bsky.social
```

Default method is `oauth`: a Chromium window opens on Bluesky's own page, where you log in (and pass
2FA) yourself. sociyx never sees your password. Alternative:

```bash
uv run sociyx login bluesky --account you.bsky.social --method app-password
```

You create an app password in Bluesky (Settings > Privacy and security > App passwords) and type it
at sociyx's hidden local prompt (and an emailed code if Bluesky asks). **Never paste passwords or
tokens into chats or issues.**

Then `scan bluesky` and `plan create bluesky ...` as below. (The adapter can read public repos
without a session, which is what the live tests do, but the CLI always requires a verified login.)
Pace: 1 delete per second by default.

### 3. Mastodon

```bash
uv run sociyx login mastodon --account @you@your.instance
# if your instance rejects the loopback redirect:
uv run sociyx login mastodon --account @you@your.instance --method oob
```

`oob` shows an authorization page in the browser and asks you to paste the displayed code at a
hidden prompt. Pace: 1 delete per 60 seconds by default (server limit is 30 per 30 minutes), so large
histories take days. Plan around that.

### 4. Instagram (unofficial API; your own data, your risk)

```bash
uv sync --extra instagram
uv run sociyx login instagram --account you
# or: --method password (hidden prompts), --method sessionfile (existing settings JSON)
uv run sociyx scan instagram --type post --type like --type saved --type dm --type comment
uv run sociyx import instagram ~/ig-export --account 111   # "Download your information" (JSON)
```

Pace: 30–45 s between writes, 1–3 min pause every 25, 200/day cap
(`--daily-cap 100` recommended for DM jobs). Covers posts, reels, stories, likes
(unlike), saved (unsave from all collections), your sent DMs (unsend), comments on your
own posts, and export-only comments on others' posts (fuzzy-resolved, else manual steps).
See [docs/platforms/instagram.md](docs/platforms/instagram.md).

## CLI examples

```bash
uv run sociyx platforms                         # adapters, content types, history coverage notes
uv run sociyx accounts                          # known accounts
uv run sociyx whoami bluesky                    # verify the session with the platform
uv run sociyx scan bluesky --type post --type like
uv run sociyx scan bluesky --max-pages 5        # partial scan; run again to resume from the saved cursor
uv run sociyx scan bluesky --fresh              # discard the resume point and start over
uv run sociyx status bluesky                    # what is stored locally, per type and state
```

### Filters

Used by `list`, `export` and `plan create`:

| Flag | Meaning |
|---|---|
| `--account X` | account id or handle (required only if several accounts are known) |
| `--type T` | content type, repeatable |
| `--since D` | inclusive start; `YYYY-MM-DD` means 00:00:00 |
| `--until D` | inclusive end; a bare `YYYY-MM-DD` means the **end of that day** (23:59:59.999999) |
| `--keyword K` | repeatable; item matches if **any** keyword is present (case-insensitive substring) |
| `--exclude-keyword K` | repeatable; item is dropped if **any** is present |

Dates are UTC unless you give an offset (`2021-03-31T18:00:00-05:00`). Items with an unknown
timestamp never match when `--since`/`--until` is used. `--since` after `--until` is an error.

```bash
uv run sociyx list bluesky --type post --since 2019-01-01 --until 2021-12-31 --keyword crypto --keyword nft --limit 100
uv run sociyx export bluesky --type post --format jsonl            # json (default), jsonl or csv
uv run sociyx export bluesky --type like --out ~/likes.json        # files are written mode 0600
```

### Plans, dry run, live

```bash
uv run sociyx plan create bluesky --type post --until 2020-12-31 --exclude-keyword keepme
uv run sociyx plan show 1 --all           # full item list (default shows 50)
uv run sociyx plan list
uv run sociyx plan execute 1              # DRY RUN (default): prints preview and the exact command
uv run sociyx plan execute 1 --live --confirm '<token>'
uv run sociyx plan execute 1 --live --confirm '<token>' --delay 5   # override pacing (seconds between requests)
```

Only content types the adapter marks deletable are planned; matching items of other types are
reported as excluded.

### Interrupting, pausing, resuming

- **Ctrl+C once**: finish the current item, then pause. **Ctrl+C twice**: stop immediately. An item
  whose request may have been sent is left `in_progress` and is verified (not blindly retried) on
  resume. Exit code 130.
- From another terminal: `sociyx job pause <job_id>` (stops after the current item) or
  `sociyx job cancel <job_id>` (remaining items become `skipped`).
- `sociyx job list`, `sociyx job status <job_id>` (counts and recent events).
- Resume with the same token: `sociyx job resume <job_id> --confirm '<token>'` or re-run
  `plan execute ... --live --confirm ...`.
- A finished (`completed`/`cancelled`) job cannot be re-run; create a new plan to delete more.

### Reports

```bash
uv run sociyx report 1                         # text
uv run sociyx report 1 --format json --out report.json
uv run sociyx report 1 --format csv
```

Reports include per-outcome sections, the latest scan status per content type, the platform's history
notes, and a disclaimer that the report covers only the planned items.

### Outcome categories

| Outcome | Meaning |
|---|---|
| `deleted_verified` | Delete accepted and a follow-up check confirmed the item is gone. |
| `deleted_unverified` | Delete accepted, but the check failed or the platform offers no reliable check. |
| `already_deleted` | The item was gone before our request. |
| `inaccessible` | The platform does not let this account delete it (permissions, foreign item). |
| `failed` | Request failed after bounded retries, or the item was still present after a delete. |
| `skipped` | Not deleted on purpose: changed since the plan, no longer in the local DB, or cancelled. |
| `pending` | Not attempted yet. |
| `in_progress` | An attempt started and its result is unknown (crash/interrupt); verified on resume. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (job `completed`) |
| 1 | `scan` finished with at least one failed content type |
| 2 | Error or refused: bad input, wrong/missing token, tampered export, wrong account, adapter error |
| 3 | Job did not complete: `paused`, `stopped_auth`, or `cancelled` |
| 130 | Interrupted with Ctrl+C |

## Development

```bash
uv sync
uv run pytest                         # unit and mock tests; live tests are skipped
SOCIYX_LIVE_TESTS=1 uv run pytest -m live   # read-only requests to public Bluesky/Mastodon endpoints
```

Live tests perform no login and no writes. Adding a platform: [docs/ADDING_A_PLATFORM.md](docs/ADDING_A_PLATFORM.md).
More docs: [ARCHITECTURE](docs/ARCHITECTURE.md), [VERIFICATION](docs/VERIFICATION.md),
[PLATFORM_STATUS](docs/PLATFORM_STATUS.md), [REFERENCES](docs/REFERENCES.md).

## License

MIT, see [LICENSE](LICENSE).
