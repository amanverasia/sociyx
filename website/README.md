# sociyx.com website

Static site for <https://sociyx.com/>. Hand-written HTML, one stylesheet, two tiny optional scripts
(copy buttons, and the home-page demo animation). No framework, no build step, no external requests, no
cookies, no analytics, no web fonts (system font stacks only).

## Structure

```
website/
  index.html            home: hero with illustrated run, local-first diagram, 4 steps, limits, status matrix, install, FAQ
  platforms/index.html  status per platform and content type
  safety/index.html     guards, limits, risks, the "delete N" confirmation
  install/index.html    pipx / uv, Playwright browser, first run with `sociyx tui --demo`
  privacy/index.html    what this site and the tool collect (nothing)
  404.html              not-found page
  assets/style.css      all styling; light and dark via prefers-color-scheme
  assets/copy.js        copy buttons for code blocks
  assets/demo.js        plays the illustrated run on the home page once (skipped with reduced motion)
  favicon.svg           icon
  wordmark.svg          standalone wordmark (the header uses an inline copy)
  sitemap.xml, robots.txt
  _headers              Cloudflare security headers and strict CSP
  .assetsignore         keeps README.md out of the deployed assets (Workers)
```

Links are root-absolute (`/platforms/`), so serve the folder at the site root.
Preview locally: `python -m http.server -d website 8000`.

## Editing

Pages are plain HTML; edit them directly. The header and footer are repeated in each page, so
change all five pages plus `404.html` when you touch navigation. Every claim must match
`docs/PLATFORM_STATUS.md`, `docs/VERIFICATION.md` and `docs/platforms/*.md`. Keep the
"0.0.1 alpha" wording until the version changes. The CSP forbids inline styles and scripts, so
put styles in `assets/style.css` and scripts in `assets/`.

## Serving on Cloudflare

Cloudflare is not set up yet and nothing here has been deployed.

**Workers static assets (recommended by Cloudflare for new projects).** Add a
`wrangler.jsonc` at the repository root (or inside `website/`):

```jsonc
{
  "name": "sociyx-site",
  "compatibility_date": "2026-10-01",
  "assets": {
    "directory": "./website",          // use "." if the file lives inside website/
    "html_handling": "auto-trailing-slash",
    "not_found_handling": "404-page"   // serves website/404.html
  }
}
```

then `wrangler deploy`. `_headers` is honoured by Workers static assets, and `.assetsignore`
excludes `README.md` and itself.

**Pages.** Connect the repository, framework preset "None", build command empty, build output
directory `website`. Pages serves `404.html` automatically and applies `_headers`. Pages has no
ignore file, so `README.md` will be publicly served; delete or move it if that matters.

After the domain is attached, check the headers (`curl -I https://sociyx.com/`) and confirm the
CSP does not block the page in a browser console.

## Notes

- The CSP is `default-src 'none'` with only same-origin images, styles and scripts. Adding any
  CDN, font host, analytics or inline style will break it; that is intentional.
- Dark mode follows the system setting; there is no toggle, so no script is needed for it.
- Add `<meta property="og:image">` only if an image is ever added; none is used today.
