# References and licenses

sociyx was designed after looking at how other cleanup tools work. **No code was copied from any of
them.** Ideas only, as noted.

| Project | License | What we took (ideas only) |
|---|---|---|
| Cyd | GPL-3.0 | Architecture ideas (local-first desktop approach). GPL-3.0 is incompatible with copying into an MIT project, so none of its code is used. |
| CleanMyPosts | MIT | Ideas only. Its LICENSE file contains an unfilled copyright template, so we treated its licensing as unclear and copied nothing. |
| Undiscord | MIT with an attribution clause for derivatives | Ideas only. Discord is not implemented (user-token automation violates Discord's terms). |
| Shreddit | MIT | Ideas: importing the GDPR data export for discovery (Reddit), and a dry-run mode. |

## Redact.dev

Redact.dev is proprietary. The user supplied the Redact desktop AppImage. A static look at the bundled
terms found a clause prohibiting reverse engineering and analysis, and the AppImage is only a bootstrap
that downloads its platform modules at runtime. We therefore stopped, analyzed none of Redact's code,
and use **no Redact code, branding or assets**. sociyx is inspired by the general idea of bulk-cleaning
your own social history, which is not protected, and is otherwise independent.

The AppImage, if present in a working copy, is excluded from Git (`*.AppImage` in `.gitignore`) and
is not part of this project.

## Specifications and documentation used

AT Protocol docs (atproto.com: repo, OAuth, rate limits), Mastodon API docs (docs.joinmastodon.org),
X API docs (docs.x.com), Instagram Graph API and Threads API docs (Meta developers). See the
platform pages for specifics.
