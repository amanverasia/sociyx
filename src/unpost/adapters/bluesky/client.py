"""XRPC client with OAuth(DPoP)/app-password auth and uniform error mapping."""

from __future__ import annotations

import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from ...models import AuthRequired, PermanentError, RateLimited, TransientError
from ...redact import scrub
from . import dpop
from ._http import TIMEOUT, USER_AGENT, origin
from .oauth import refresh_session
from .session import OAuthSession, PasswordSession, save_session

# Errors that retrying (or refreshing) cannot fix: the user must log in again.
_AUTH_ERRORS = {
    "AuthenticationRequired",
    "AuthFactorTokenRequired",
    "AccountTakedown",
    "InvalidToken",
    "ExpiredToken",
}


class XrpcError(PermanentError):
    """Server-reported failure; `error` is the XRPC error name (e.g. RecordNotFound)."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(scrub(f"HTTP {status} {error}: {message}"))
        self.status = status
        self.error = error
        self.message = message


class AuthFactorRequired(AuthRequired):
    """Server wants the emailed 2FA code (createSession AuthFactorTokenRequired)."""


def _body(resp: httpx.Response) -> tuple[str, str]:
    try:
        data = resp.json()
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("error", "")), str(data.get("message", "") or data.get("error_description", ""))


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    reset = resp.headers.get("ratelimit-reset")  # epoch seconds (IETF draft)
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return None


def raise_for_response(resp: httpx.Response) -> None:
    status = resp.status_code
    if status < 400:
        return
    error, message = _body(resp)
    if status == 429:
        raise RateLimited("rate limited by Bluesky", retry_after=_retry_after(resp))
    if status >= 500:
        raise TransientError(f"Bluesky server error (HTTP {status})")
    if error == "AuthFactorTokenRequired":
        raise AuthFactorRequired("a two-factor code is required")
    if status == 401 or error in _AUTH_ERRORS:
        raise AuthRequired(scrub(f"authentication failed ({error or status}); run `unpost login bluesky`"))
    raise XrpcError(status, error, message)


class Auth(Protocol):
    def headers(self, method: str, url: str) -> dict[str, str]: ...

    def handle_response(self, resp: httpx.Response) -> bool:
        """Inspect a response; return True if the request should be retried."""


class OAuthAuth:
    def __init__(
        self,
        session: OAuthSession,
        http: httpx.Client,
        persist: Callable[[OAuthSession], None] = save_session,
    ) -> None:
        self.session = session
        self.http = http
        self.persist = persist
        self._key = dpop.key_from_pem(session.dpop_private_key_pem)
        self._fresh_token = ""  # token we just minted; if it fails too, refreshing again won't help

    def _refresh(self) -> None:
        refresh_session(self.http, self.session)
        self._fresh_token = self.session.access_token
        self.persist(self.session)

    def headers(self, method: str, url: str) -> dict[str, str]:
        if self.session.expires_at - time.time() < 30:
            self._refresh()
        s = self.session
        proof = dpop.make_proof(
            self._key, method, url, nonce=s.nonces.get(origin(url)), access_token=s.access_token
        )
        return {"Authorization": f"DPoP {s.access_token}", "DPoP": proof}

    def handle_response(self, resp: httpx.Response) -> bool:
        org = origin(str(resp.request.url))
        nonce = resp.headers.get("DPoP-Nonce")
        if nonce:
            self.session.nonces[org] = nonce
        if resp.status_code not in (400, 401):
            return False
        www = resp.headers.get("WWW-Authenticate", "")
        error, _ = _body(resp)
        if "use_dpop_nonce" in www or error == "use_dpop_nonce":
            return bool(nonce)
        if 'error="invalid_token"' in www or error in ("ExpiredToken", "InvalidToken"):
            used = resp.request.headers.get("Authorization", "").removeprefix("DPoP ")
            if used == self._fresh_token:
                return False  # a brand-new token was rejected: give up
            if used == self.session.access_token:
                self._refresh()
            return True
        return False


class PasswordAuth:
    def __init__(
        self,
        session: PasswordSession,
        http: httpx.Client,
        persist: Callable[[PasswordSession], None] = save_session,
    ) -> None:
        self.session = session
        self.http = http
        self.persist = persist
        self._fresh = ""

    def headers(self, method: str, url: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.session.access_jwt}"}

    def _refresh(self) -> None:
        url = f"{self.session.pds_url}/xrpc/com.atproto.server.refreshSession"
        try:
            resp = self.http.post(
                url,
                headers={"Authorization": f"Bearer {self.session.refresh_jwt}", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except httpx.TransportError as e:
            raise TransientError(f"network error refreshing session: {type(e).__name__}") from None
        if resp.status_code >= 500:
            raise TransientError(f"session refresh failed with HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise AuthRequired("session expired and could not be refreshed; run `unpost login bluesky`")
        data = resp.json()
        self.session.access_jwt = data["accessJwt"]
        self.session.refresh_jwt = data["refreshJwt"]
        self._fresh = self.session.access_jwt
        self.persist(self.session)

    def handle_response(self, resp: httpx.Response) -> bool:
        if resp.status_code not in (400, 401):
            return False
        error, _ = _body(resp)
        if error != "ExpiredToken":
            return False
        used = resp.request.headers.get("Authorization", "").removeprefix("Bearer ")
        if used == self._fresh:
            return False
        if used == self.session.access_jwt:
            self._refresh()
        return True


class XrpcClient:
    def __init__(self, base_url: str, auth: Auth | None = None, http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=TIMEOUT)

    def get(self, nsid: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", nsid, params=clean)

    def post(self, nsid: str, json: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", nsid, json=json)

    def _request(self, method: str, nsid: str, *, params: dict[str, Any] | None = None,
                 json: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/xrpc/{nsid}"
        resp: httpx.Response | None = None
        for attempt in range(3):  # original + nonce retry + post-refresh retry
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if self.auth:
                headers.update(self.auth.headers(method, url))
            try:
                resp = self.http.request(method, url, params=params, json=json, headers=headers, timeout=TIMEOUT)
            except httpx.TransportError as e:  # includes timeouts
                raise TransientError(f"network error calling {nsid}: {type(e).__name__}") from None
            retry = self.auth.handle_response(resp) if self.auth else False
            if retry and attempt < 2:
                continue
            break
        assert resp is not None
        raise_for_response(resp)
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
