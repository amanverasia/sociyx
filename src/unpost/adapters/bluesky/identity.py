"""Handle/DID -> (DID, PDS) resolution without trusting any single hop blindly."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx

from ...models import PermanentError, TransientError
from ._http import PUBLIC_APPVIEW, TIMEOUT, USER_AGENT

_HANDLE_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
PLC_DIRECTORY = "https://plc.directory"


@dataclass(frozen=True)
class Identity:
    did: str
    handle: str
    pds_url: str
    handle_verified: bool  # the DID document and handle point at each other


def _get(http: httpx.Client, url: str, **params: str) -> httpx.Response:
    try:
        resp = http.get(url, params=params or None, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    except httpx.TransportError as e:
        raise TransientError(f"network error resolving identity: {type(e).__name__}") from None
    if resp.status_code >= 500:
        raise TransientError(f"identity lookup failed with HTTP {resp.status_code}")
    return resp


def _resolve_handle(handle: str, http: httpx.Client, appview: str) -> str:
    """Handle -> DID via the AppView resolver, then the .well-known fallback.
    (Assumption: DNS TXT lookup is delegated to the AppView; we do not do DNS ourselves.)"""
    resp = _get(http, f"{appview}/xrpc/com.atproto.identity.resolveHandle", handle=handle)
    if resp.status_code == 200:
        did = resp.json().get("did", "")
        if did.startswith("did:"):
            return did
    try:
        resp = _get(http, f"https://{handle}/.well-known/atproto-did")
    except TransientError:
        raise
    text = resp.text.strip() if resp.status_code == 200 else ""
    if text.startswith("did:"):
        return text.splitlines()[0].strip()
    raise PermanentError(f"could not resolve handle {handle!r} to a DID")


def _fetch_did_doc(did: str, http: httpx.Client) -> dict[str, Any]:
    if did.startswith("did:plc:"):
        url = f"{PLC_DIRECTORY}/{did}"
    elif did.startswith("did:web:"):
        raw = did[len("did:web:") :]
        if ":" in raw:
            raise PermanentError("did:web identifiers with path segments are not supported")
        url = f"https://{unquote(raw)}/.well-known/did.json"
    else:
        raise PermanentError(f"unsupported DID method: {did.split(':')[1] if ':' in did else did!r}")
    resp = _get(http, url)
    if resp.status_code != 200:
        raise PermanentError(f"could not fetch DID document (HTTP {resp.status_code})")
    doc = resp.json()
    if doc.get("id") != did:
        raise PermanentError("DID document id does not match requested DID")
    return doc


def _pds_from_doc(doc: dict[str, Any]) -> str:
    for svc in doc.get("service", []):
        if str(svc.get("id", "")).endswith("#atproto_pds") and svc.get("type") == "AtprotoPersonalDataServer":
            endpoint = svc.get("serviceEndpoint")
            if isinstance(endpoint, str) and endpoint.startswith(("https://", "http://")):
                return endpoint.rstrip("/")
    raise PermanentError("DID document declares no atproto PDS")


def resolve_identity(handle_or_did: str, http: httpx.Client, appview: str = PUBLIC_APPVIEW) -> Identity:
    """Accepts '@alice.bsky.social', 'alice.bsky.social' or a DID."""
    value = handle_or_did.strip()
    if value.startswith("did:"):
        did, given_handle = value, None
    else:
        given_handle = value.lstrip("@").lower()
        if not _HANDLE_RE.match(given_handle):
            raise PermanentError(f"not a valid handle or DID: {handle_or_did!r}")
        did = _resolve_handle(given_handle, http, appview)

    doc = _fetch_did_doc(did, http)
    pds = _pds_from_doc(doc)
    claimed = [a[len("at://") :] for a in doc.get("alsoKnownAs", []) if str(a).startswith("at://")]

    if given_handle is not None:
        return Identity(did, given_handle, pds, given_handle in claimed)

    handle = claimed[0] if claimed else "handle.invalid"
    verified = False
    if claimed:
        try:
            verified = _resolve_handle(handle, http, appview) == did
        except (PermanentError, TransientError):
            verified = False
    return Identity(did, handle, pds, verified)
