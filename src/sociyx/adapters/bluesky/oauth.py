"""AT Protocol OAuth (loopback public client): PAR + PKCE S256 + DPoP ES256.

Spec: https://atproto.com/specs/oauth. The user logs in themselves in a visible
browser; we never type credentials or touch CAPTCHAs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric import ec

from ...loopback import CallbackServer, open_visible_browser
from ...models import AuthRequired, PermanentError, TransientError
from ...redact import scrub
from . import dpop
from ._http import TIMEOUT, USER_AGENT, origin
from .identity import Identity
from .session import OAuthSession, browser_profile_dir

# "transition:generic" is a transitional scope that grants repo writes; the
# protocol may replace it with finer-grained scopes later.
SCOPE = "atproto transition:generic"

# A launcher opens `url` for the user and returns an optional cleanup callable.
BrowserLauncher = Callable[[str], Callable[[], None] | None]


class OAuthError(PermanentError):
    """Login/token failure. Messages never contain tokens, codes or verifiers."""


@dataclass(frozen=True)
class AuthServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    par_endpoint: str
    revocation_endpoint: str
    dpop_algs: tuple[str, ...]


def _get_json(http: httpx.Client, url: str) -> dict[str, Any]:
    try:
        resp = http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
    except httpx.TransportError as e:
        raise TransientError(f"network error during OAuth discovery: {type(e).__name__}") from None
    if resp.status_code >= 500:
        raise TransientError(f"OAuth discovery failed with HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise OAuthError(f"OAuth discovery failed with HTTP {resp.status_code}")
    return resp.json()


def discover(pds_url: str, http: httpx.Client) -> AuthServerMetadata:
    """PDS -> protected-resource metadata -> authorization server metadata."""
    resource = _get_json(http, f"{pds_url.rstrip('/')}/.well-known/oauth-protected-resource")
    servers = resource.get("authorization_servers") or []
    if not servers:
        raise OAuthError("PDS declares no authorization server")
    issuer = servers[0].rstrip("/")
    meta = _get_json(http, f"{issuer}/.well-known/oauth-authorization-server")
    if str(meta.get("issuer", "")).rstrip("/") != issuer:
        raise OAuthError("authorization server metadata issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "pushed_authorization_request_endpoint"):
        if not meta.get(key):
            raise OAuthError(f"authorization server metadata lacks {key}")
    return AuthServerMetadata(
        issuer=issuer,
        authorization_endpoint=meta["authorization_endpoint"],
        token_endpoint=meta["token_endpoint"],
        par_endpoint=meta["pushed_authorization_request_endpoint"],
        revocation_endpoint=meta.get("revocation_endpoint", ""),
        dpop_algs=tuple(meta.get("dpop_signing_alg_values_supported", [])),
    )


def build_client_id(redirect_uri: str, scope: str = SCOPE) -> str:
    """Loopback client id: exactly http://localhost plus redirect_uri and scope params."""
    return "http://localhost?" + urlencode({"redirect_uri": redirect_uri, "scope": scope})


def _error_name(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("error", "")) if isinstance(body, dict) else ""
    except ValueError:
        return ""


def dpop_form_post(
    http: httpx.Client,
    url: str,
    data: dict[str, str],
    key: ec.EllipticCurvePrivateKey,
    nonces: dict[str, str],
) -> httpx.Response:
    """POST a form with a DPoP proof; retry once if the server demands a (new) nonce."""
    org = origin(url)
    resp: httpx.Response | None = None
    for attempt in range(2):
        proof = dpop.make_proof(key, "POST", url, nonce=nonces.get(org))
        try:
            resp = http.post(
                url,
                data=data,
                headers={"DPoP": proof, "Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except httpx.TransportError as e:
            raise TransientError(f"network error talking to {org}: {type(e).__name__}") from None
        new_nonce = resp.headers.get("DPoP-Nonce")
        if new_nonce:
            nonces[org] = new_nonce
        if attempt == 0 and new_nonce and resp.status_code in (400, 401) and _error_name(resp) == "use_dpop_nonce":
            continue
        break
    assert resp is not None
    return resp


def _raise_oauth_error(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 500:
        raise TransientError(f"{what} failed with HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    name = str(body.get("error", "unknown")) if isinstance(body, dict) else "unknown"
    desc = str(body.get("error_description", "")) if isinstance(body, dict) else ""
    raise OAuthError(scrub(f"{what} failed: HTTP {resp.status_code} {name} {desc}".strip()))


def push_authorization_request(
    http: httpx.Client,
    meta: AuthServerMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    challenge: str,
    login_hint: str | None,
    key: ec.EllipticCurvePrivateKey,
    nonces: dict[str, str],
) -> str:
    """PAR: returns a single-use request_uri. Touches no account."""
    data = {
        "client_id": client_id,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    if login_hint:
        data["login_hint"] = login_hint
    resp = dpop_form_post(http, meta.par_endpoint, data, key, nonces)
    if resp.status_code not in (200, 201):
        _raise_oauth_error(resp, "pushed authorization request")
    request_uri = resp.json().get("request_uri")
    if not request_uri:
        raise OAuthError("PAR response lacked request_uri")
    return request_uri


def refresh_session(http: httpx.Client, session: OAuthSession) -> None:
    """Rotate tokens in place. Refresh tokens are single-use, so callers must persist
    the session immediately afterwards."""
    key = dpop.key_from_pem(session.dpop_private_key_pem)
    resp = dpop_form_post(
        http,
        session.token_endpoint,
        {"grant_type": "refresh_token", "refresh_token": session.refresh_token, "client_id": session.client_id},
        key,
        session.nonces,
    )
    if resp.status_code != 200:
        if resp.status_code >= 500:
            raise TransientError(f"token refresh failed with HTTP {resp.status_code}")
        raise AuthRequired(f"OAuth session could not be refreshed ({_error_name(resp) or resp.status_code}); log in again")
    body = resp.json()
    if body.get("sub") and body["sub"] != session.did:
        raise AuthRequired("refreshed token belongs to a different account; log in again")
    session.access_token = body["access_token"]
    session.refresh_token = body.get("refresh_token", session.refresh_token)
    session.expires_at = time.time() + float(body.get("expires_in", 300))


def default_browser_launcher(url: str) -> Callable[[], None] | None:
    """Visible browser with a persistent profile so the Bluesky web login survives runs."""
    return open_visible_browser(url, browser_profile_dir())


class OAuthLogin:
    def __init__(
        self,
        http: httpx.Client,
        identity: Identity,
        browser_launcher: BrowserLauncher | None = None,
        timeout: float = 600.0,
        say: Callable[[str], None] = print,
    ) -> None:
        self.http = http
        self.identity = identity
        self.launch = browser_launcher or default_browser_launcher
        self.timeout = timeout
        self.say = say

    def run(self) -> OAuthSession:
        meta = discover(self.identity.pds_url, self.http)
        key = dpop.generate_key()
        nonces: dict[str, str] = {}
        verifier = dpop.new_code_verifier()
        state = dpop.new_state()

        server = CallbackServer()
        server.start()
        cleanup: Callable[[], None] | None = None
        try:
            redirect_uri = server.redirect_uri
            client_id = build_client_id(redirect_uri)
            request_uri = push_authorization_request(
                self.http,
                meta,
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=SCOPE,
                state=state,
                challenge=dpop.code_challenge(verifier),
                login_hint=self.identity.handle,
                key=key,
                nonces=nonces,
            )
            auth_url = meta.authorization_endpoint + "?" + urlencode({"client_id": client_id, "request_uri": request_uri})
            self.say("Opening a browser window. Log in to Bluesky there and approve access.")
            cleanup = self.launch(auth_url)
            params = server.wait(self.timeout)
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
        if params.get("iss", "").rstrip("/") != meta.issuer:
            raise OAuthError("OAuth callback issuer mismatch; login rejected")
        code = params.get("code")
        if not code:
            raise OAuthError("OAuth callback carried no authorization code")

        resp = dpop_form_post(
            self.http,
            meta.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
                "client_id": client_id,
            },
            key,
            nonces,
        )
        if resp.status_code != 200:
            _raise_oauth_error(resp, "token exchange")
        tok = resp.json()
        if str(tok.get("token_type", "")).lower() != "dpop":
            raise OAuthError("token response is not DPoP-bound")
        if tok.get("sub") != self.identity.did:
            raise OAuthError("token subject does not match the account that was requested; login rejected")
        if "atproto" not in str(tok.get("scope", "")).split():
            raise OAuthError("token response lacks the atproto scope")
        if not tok.get("access_token") or not tok.get("refresh_token"):
            raise OAuthError("token response lacks access or refresh token")

        return OAuthSession(
            did=self.identity.did,
            handle=self.identity.handle,
            pds_url=self.identity.pds_url,
            issuer=meta.issuer,
            token_endpoint=meta.token_endpoint,
            client_id=client_id,
            redirect_uri=redirect_uri,
            access_token=tok["access_token"],
            refresh_token=tok["refresh_token"],
            expires_at=time.time() + float(tok.get("expires_in", 300)),
            dpop_private_key_pem=dpop.key_to_pem(key),
            nonces=nonces,
            revocation_endpoint=meta.revocation_endpoint,
        )
