"""Mastodon OAuth 2 authorization-code flow with PKCE S256.

Default redirect is a loopback URL (http://127.0.0.1:<port>/callback); whether every
instance accepts it is UNVERIFIED, so `--method oob` uses urn:ietf:wg:oauth:2.0:oob and the
user types the displayed one-time code into a hidden terminal prompt.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import httpx

from ...loopback import CallbackServer, open_visible_browser
from ...models import PermanentError
from ...redact import scrub
from . import client
from .session import AppRegistration, load_app, save_app

SCOPE = (
    "read:accounts read:statuses read:favourites read:bookmarks "
    "write:statuses write:favourites write:bookmarks"
)
OOB = "urn:ietf:wg:oauth:2.0:oob"
PREFERRED_PORT = 53682  # stable port so the per-redirect-URI app registration can be cached
BrowserLauncher = Callable[[str], Callable[[], None] | None]


class OAuthError(PermanentError):
    """Login failure. Messages never contain tokens, codes or verifiers."""


class RedirectRejected(OAuthError):
    """The instance refused to register the loopback redirect URI."""


def new_verifier() -> str:
    return secrets.token_urlsafe(64)


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def default_browser_launcher(url: str) -> Callable[[], None] | None:
    """Visible (non-persistent) browser if available, else the system browser."""
    return open_visible_browser(url)


def register_app(
    http: httpx.Client, base: str, instance: str, redirect_uri: str, register_uris: list[str]
) -> AppRegistration:
    """Cached per (instance, redirect URI); otherwise POST /api/v1/apps with `register_uris`."""
    cached = load_app(instance, redirect_uri)
    if cached:
        return cached
    try:
        resp = client.request(
            http,
            "POST",
            base,
            "/api/v1/apps",
            data={
                "client_name": "unpost",
                "redirect_uris": "\n".join(register_uris),
                "scopes": SCOPE,
                "website": "https://github.com/amanverasia/unpost",
            },
        )
    except client.ApiError as e:
        if e.status in (400, 422) and len(register_uris) > 1:
            raise RedirectRejected("instance rejected the loopback redirect URI") from None
        raise OAuthError(f"app registration failed: {e}") from None
    if resp.status_code != 200:
        raise OAuthError(f"app registration failed: HTTP {resp.status_code}")
    body = client.json_body(resp)
    if not body.get("client_id") or not body.get("client_secret"):
        raise OAuthError("app registration response lacked client credentials")
    app = AppRegistration(body["client_id"], body["client_secret"])
    save_app(instance, register_uris, app)
    return app


def authorize_url(base: str, app: AppRegistration, redirect_uri: str, state: str, verifier: str) -> str:
    return base + "/oauth/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": app.client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
    )


def exchange_code(
    http: httpx.Client, base: str, app: AppRegistration, redirect_uri: str, code: str, verifier: str
) -> str:
    try:
        resp = client.request(
            http,
            "POST",
            base,
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": app.client_id,
                "client_secret": app.client_secret,
                "code_verifier": verifier,
            },
        )
    except client.ApiError as e:
        raise OAuthError(scrub(f"token exchange failed: {e}")) from None
    if resp.status_code != 200:
        raise OAuthError(f"token exchange failed: HTTP {resp.status_code}")
    token = client.json_body(resp).get("access_token")
    if not token:
        raise OAuthError("token response lacked an access token")
    return token


def run_loopback(
    http: httpx.Client,
    base: str,
    instance: str,
    launcher: BrowserLauncher,
    say: Callable[[str], None],
    timeout: float = 600.0,
) -> tuple[AppRegistration, str]:
    """Returns (app, access_token). Raises RedirectRejected if the instance won't take loopback."""
    server = CallbackServer(PREFERRED_PORT)
    server.start()
    cleanup: Callable[[], None] | None = None
    try:
        redirect_uri = server.redirect_uri
        app = register_app(http, base, instance, redirect_uri, [redirect_uri, OOB])
        state = secrets.token_urlsafe(24)
        verifier = new_verifier()
        say("Opening a browser window. Log in to Mastodon there and approve access.")
        cleanup = launcher(authorize_url(base, app, redirect_uri, state, verifier))
        params = server.wait(timeout)
    finally:
        server.stop()
        if cleanup:
            cleanup()
    if params is None:
        raise OAuthError("timed out waiting for the browser login to finish")
    if "error" in params:
        raise OAuthError(scrub(f"authorization was not granted: {params['error']}"))
    if params.get("state") != state:
        raise OAuthError("OAuth callback state mismatch; login rejected")
    code = params.get("code")
    if not code:
        raise OAuthError("OAuth callback carried no authorization code")
    return app, exchange_code(http, base, app, redirect_uri, code, verifier)


def run_oob(
    http: httpx.Client,
    base: str,
    instance: str,
    launcher: BrowserLauncher,
    prompt_secret: Callable[[str], str],
    say: Callable[[str], None],
) -> tuple[AppRegistration, str]:
    app = register_app(http, base, instance, OOB, [OOB])
    verifier = new_verifier()
    say("Opening a browser window. Approve access; the instance will show a one-time code.")
    cleanup = launcher(authorize_url(base, app, OOB, secrets.token_urlsafe(24), verifier))
    try:
        code = prompt_secret("Authorization code (hidden): ").strip()
    finally:
        if cleanup:
            cleanup()
    if not code:
        raise OAuthError("no authorization code entered")
    return app, exchange_code(http, base, app, OOB, code, verifier)
