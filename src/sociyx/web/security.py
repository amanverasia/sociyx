"""The localhost threat model, enforced in one pure-ASGI middleware that runs before any route.

Order of checks for every request:
  1. Host header must be exactly 127.0.0.1:PORT or localhost:PORT (defeats DNS rebinding).
  2. Only GET, HEAD and POST exist (no OPTIONS, so there is nothing to preflight and no CORS).
  3. `/?launch=SECRET` is the only unauthenticated entry: a correct, unused secret mints a session cookie.
  4. Everything else needs a valid session cookie (HttpOnly, SameSite=Strict).
  5. POST additionally needs a same-origin Origin (or Referer), a urlencoded body, and the session's CSRF
     token in the X-CSRF-Token header or the `csrf_token` form field.
Every response, including refusals, gets the security headers and no CORS headers.
"""

from __future__ import annotations

import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from .state import WebState

COOKIE = "sociyx_session"
MAX_BODY = 1_000_000

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
]


class SecurityMiddleware:
    def __init__(self, app, state: WebState):
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return  # no websockets: drop the connection
        path = scope["path"]

        async def send_secure(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message["headers"]
                           if not k.lower().startswith(b"access-control-") and k.lower() not in
                           {h for h, _ in SECURITY_HEADERS} | {b"server"}]
                headers += SECURITY_HEADERS
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-cache" if path.startswith("/static/") else b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        async def refuse(status: int, text: str):
            body = (text + "\n").encode()
            await send_secure({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
            await send_secure({"type": "http.response.body", "body": body})

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "").lower()
        if host not in self.state.allowed_hosts():
            await refuse(400, "Bad Host header. This server only answers to 127.0.0.1 and localhost.")
            return
        method = scope["method"]
        if method not in ("GET", "HEAD", "POST"):
            await refuse(405, "Method not allowed.")
            return

        # ---- the one unauthenticated door: the single-use launch secret ----
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        if path == "/" and method == "GET" and "launch" in query:
            sess = self.state.exchange_launch_secret(query["launch"][0])
            if sess is None:
                await refuse(403, "This launch link is invalid or was already used. "
                                  "Restart `sociyx ui` to get a new one.")
                return
            await send_secure({"type": "http.response.start", "status": 303, "headers": [
                (b"location", b"/"), (b"content-length", b"0"),
                (b"set-cookie", f"{COOKIE}={sess.sid}; HttpOnly; SameSite=Strict; Path=/".encode())]})
            await send_secure({"type": "http.response.body", "body": b""})
            return

        # ---- everything else needs the session cookie ----
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(COOKIE)
        sess = self.state.session_for(morsel.value if morsel else None)
        if sess is None:
            await refuse(401, "Not signed in. Open the launch link printed by `sociyx ui`.")
            return
        scope["sociyx.session"] = sess
        scope["sociyx.form"] = {}

        if method == "POST":
            expected = f"http://{host}"
            origin = headers.get("origin")
            if origin is None and headers.get("referer"):
                u = urlsplit(headers["referer"])
                origin = f"{u.scheme}://{u.netloc}"
            if origin is None or origin.lower() != expected:
                await refuse(403, "Cross-origin or origin-less request refused.")
                return
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            body = b""
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body += msg.get("body", b"")
                if len(body) > MAX_BODY:
                    await refuse(413, "Request too large.")
                    return
                if not msg.get("more_body"):
                    break
            if body and ctype != "application/x-www-form-urlencoded":
                await refuse(415, "Forms must be urlencoded.")
                return
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}
            token = headers.get("x-csrf-token") or form.get("csrf_token") or ""
            if not token or not secrets.compare_digest(token.encode(), sess.csrf.encode()):
                await refuse(403, "Missing or wrong CSRF token.")
                return
            form.pop("csrf_token", None)
            scope["sociyx.form"] = form
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send_secure)
            return

        await self.app(scope, receive, send_secure)
