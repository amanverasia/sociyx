"""Bluesky adapter tests. No network: everything goes through httpx.MockTransport
(the loopback OAuth callback uses a real socket on 127.0.0.1 only)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from sociyx.adapters.base import ScanPage
from sociyx.adapters.bluesky import BlueskyAdapter
from sociyx.adapters.bluesky import dpop
from sociyx.adapters.bluesky.client import OAuthAuth, XrpcClient, XrpcError
from sociyx.adapters.bluesky.identity import resolve_identity
from sociyx.adapters.bluesky.oauth import (
    OAuthError,
    OAuthLogin,
    build_client_id,
    discover,
    push_authorization_request,
)
from sociyx.adapters.bluesky.session import (
    OAuthSession,
    PasswordSession,
    load_session,
    save_session,
    session_path,
)
from sociyx.models import (
    Account,
    AuthRequired,
    ContentItem,
    DeleteStatus,
    PermanentError,
    RateLimited,
    TransientError,
    VerifyStatus,
)

DID = "did:plc:abc123"
HANDLE = "alice.test"
PDS = "https://pds.example"
ISSUER = "https://auth.example"
APP_PASSWORD = "abcd-efgh-ijkl-mnop"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIYX_HOME", str(tmp_path))


class Router:
    """Tiny request router for MockTransport: key is 'METHOD host/path'."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.log: list[httpx.Request] = []

    def add(self, key: str, fn: Callable[[httpx.Request], httpx.Response]) -> None:
        self.routes[key] = fn

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        key = f"{request.method} {request.url.host}{request.url.path}"
        if key not in self.routes:
            return httpx.Response(404, json={"error": "NotFound", "message": key})
        return self.routes[key](request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def did_doc(handle: str = HANDLE, did: str = DID) -> dict:
    return {
        "id": did,
        "alsoKnownAs": [f"at://{handle}"],
        "service": [
            {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": PDS}
        ],
    }


def identity_routes(r: Router, handle: str = HANDLE, doc_handle: str | None = None) -> None:
    r.add("GET public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
          lambda req: httpx.Response(200, json={"did": DID}))
    r.add(f"GET plc.directory/{DID}", lambda req: httpx.Response(200, json=did_doc(doc_handle or handle)))


# ---------------------------------------------------------------- identity


def test_identity_plc_via_appview():
    r = Router()
    identity_routes(r)
    ident = resolve_identity("@Alice.Test", r.client())
    assert (ident.did, ident.handle, ident.pds_url, ident.handle_verified) == (DID, HANDLE, PDS, True)


def test_identity_well_known_fallback():
    r = Router()
    r.add("GET public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle",
          lambda req: httpx.Response(400, json={"error": "InvalidRequest"}))
    r.add("GET alice.test/.well-known/atproto-did", lambda req: httpx.Response(200, text=DID + "\n"))
    r.add(f"GET plc.directory/{DID}", lambda req: httpx.Response(200, json=did_doc()))
    assert resolve_identity("alice.test", r.client()).did == DID


def test_identity_alsoknownas_mismatch_is_unverified():
    r = Router()
    identity_routes(r, doc_handle="someone-else.test")
    assert resolve_identity("alice.test", r.client()).handle_verified is False


def test_identity_from_did_verifies_handle_back():
    r = Router()
    identity_routes(r)
    ident = resolve_identity(DID, r.client())
    assert ident.handle == HANDLE and ident.handle_verified is True


def test_identity_rejects_garbage():
    with pytest.raises(PermanentError):
        resolve_identity("not a handle", Router().client())


# ---------------------------------------------------------------- DPoP


def decode_jwt(token: str) -> tuple[dict, dict, bytes, bytes]:
    h, c, s = token.split(".")
    return (json.loads(dpop.b64url_decode(h)), json.loads(dpop.b64url_decode(c)),
            dpop.b64url_decode(s), f"{h}.{c}".encode())


def verify_proof(token: str) -> tuple[dict, dict]:
    header, claims, sig, signing_input = decode_jwt(token)
    jwk = header["jwk"]
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(dpop.b64url_decode(jwk["x"]), "big"),
        int.from_bytes(dpop.b64url_decode(jwk["y"]), "big"),
        ec.SECP256R1(),
    ).public_key()
    assert len(sig) == 64
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))  # raises if invalid
    return header, claims


def test_dpop_proof_structure_and_signature():
    key = dpop.generate_key()
    proof = dpop.make_proof(key, "get", "https://pds.example/xrpc/x?a=1#frag", nonce="n1", access_token="tok")
    header, claims = verify_proof(proof)
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    assert header["jwk"]["kty"] == "EC" and header["jwk"]["crv"] == "P-256"
    assert claims["htm"] == "GET"
    assert claims["htu"] == "https://pds.example/xrpc/x"
    assert claims["nonce"] == "n1"
    assert claims["ath"] == dpop.b64url(hashlib.sha256(b"tok").digest())
    assert abs(claims["iat"] - time.time()) < 5
    other = verify_proof(dpop.make_proof(key, "GET", "https://pds.example/xrpc/x"))[1]
    assert other["jti"] != claims["jti"] and "ath" not in other and "nonce" not in other
    assert "=" not in proof


def test_dpop_key_pem_roundtrip():
    key = dpop.generate_key()
    again = dpop.key_from_pem(dpop.key_to_pem(key))
    assert dpop.public_jwk(key) == dpop.public_jwk(again)


def test_pkce_challenge_matches_rfc_vector():
    # RFC 7636 appendix B
    assert dpop.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


# ---------------------------------------------------------------- OAuth fake server


def check_proof(req: httpx.Request, htm: str, url: str, token: str | None = None) -> dict:
    _, claims = verify_proof(req.headers["DPoP"])
    assert claims["htm"] == htm and claims["htu"] == url
    if token:
        assert claims["ath"] == dpop.b64url(hashlib.sha256(token.encode()).digest())
    return claims


class FakeAS:
    """Authorization server (+ PDS discovery) with DPoP nonce enforcement on PAR/token."""

    def __init__(self, sub: str = DID, require_nonce: bool = True) -> None:
        self.sub = sub
        self.require_nonce = require_nonce
        self.router = Router()
        self.par: dict[str, str] = {}
        self.par_attempts: list[dict] = []
        self.token_bodies: list[dict[str, str]] = []
        r = self.router
        identity_routes(r)
        r.add(f"GET pds.example/.well-known/oauth-protected-resource",
              lambda q: httpx.Response(200, json={"authorization_servers": [ISSUER]}))
        r.add("GET auth.example/.well-known/oauth-authorization-server", lambda q: httpx.Response(200, json={
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "pushed_authorization_request_endpoint": f"{ISSUER}/par",
            "revocation_endpoint": f"{ISSUER}/revoke",
            "dpop_signing_alg_values_supported": ["ES256"],
        }))
        r.add("POST auth.example/par", self._par)
        r.add("POST auth.example/token", self._token)
        r.add("POST auth.example/revoke", lambda q: httpx.Response(200))

    def _nonce_gate(self, req: httpx.Request, url: str) -> httpx.Response | None:
        claims = check_proof(req, "POST", url)
        if self.require_nonce and claims.get("nonce") != "server-nonce":
            return httpx.Response(400, json={"error": "use_dpop_nonce"}, headers={"DPoP-Nonce": "server-nonce"})
        return None

    def _par(self, req: httpx.Request) -> httpx.Response:
        denied = self._nonce_gate(req, f"{ISSUER}/par")
        self.par_attempts.append(dict(parse_qsl(req.content.decode())))
        if denied:
            return denied
        self.par = dict(parse_qsl(req.content.decode()))
        return httpx.Response(201, json={"request_uri": "urn:par:123", "expires_in": 60},
                              headers={"DPoP-Nonce": "server-nonce"})

    def _token(self, req: httpx.Request) -> httpx.Response:
        denied = self._nonce_gate(req, f"{ISSUER}/token")
        if denied:
            return denied
        body = dict(parse_qsl(req.content.decode()))
        self.token_bodies.append(body)
        if body["grant_type"] == "authorization_code":
            assert dpop.code_challenge(body["code_verifier"]) == self.par["code_challenge"]
            assert body["redirect_uri"] == self.par["redirect_uri"] and body["code"] == "authcode"
            return httpx.Response(200, json={
                "access_token": "access-1", "refresh_token": "refresh-1", "token_type": "DPoP",
                "expires_in": 1800, "scope": "atproto transition:generic", "sub": self.sub})
        assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "refresh-1"
        return httpx.Response(200, json={
            "access_token": "access-2", "refresh_token": "refresh-2", "token_type": "DPoP",
            "expires_in": 1800, "scope": "atproto transition:generic", "sub": self.sub})


def make_launcher(fa: FakeAS, **overrides: str | None) -> Callable[[str], None]:
    """Simulates the user finishing login: hits the loopback callback like the AS would."""

    def launch(url: str) -> None:
        q = parse_qs(urlsplit(url).query)
        assert q["request_uri"] == ["urn:par:123"]
        assert url.startswith(f"{ISSUER}/authorize?")
        params = {"code": "authcode", "state": fa.par["state"], "iss": ISSUER}
        params.update({k: v for k, v in overrides.items() if v is not None})
        with httpx.Client(trust_env=False) as c:
            resp = c.get(fa.par["redirect_uri"], params=params)
        assert resp.status_code == 200
        assert "close this window" in resp.text
        assert "authcode" not in resp.text  # never echo the code
        with httpx.Client(trust_env=False) as c:  # single use
            assert c.get(fa.par["redirect_uri"], params=params).status_code == 410

    return launch


def test_client_id_construction():
    rid = "http://127.0.0.1:5555/callback"
    cid = build_client_id(rid)
    parts = urlsplit(cid)
    assert (parts.scheme, parts.hostname, parts.port, parts.path) == ("http", "localhost", None, "")
    q = parse_qs(parts.query)
    assert q["redirect_uri"] == [rid] and q["scope"] == ["atproto transition:generic"]


def test_par_retries_with_server_nonce():
    fa = FakeAS()
    http = fa.router.client()
    meta = discover(PDS, http)
    assert "ES256" in meta.dpop_algs
    key = dpop.generate_key()
    nonces: dict[str, str] = {}
    uri = push_authorization_request(
        http, meta, client_id=build_client_id("http://127.0.0.1:1/callback"),
        redirect_uri="http://127.0.0.1:1/callback", scope="atproto transition:generic",
        state="s", challenge="c", login_hint=HANDLE, key=key, nonces=nonces)
    assert uri == "urn:par:123"
    par_reqs = [q for q in fa.router.log if q.url.path == "/par"]
    assert len(par_reqs) == 2
    assert "nonce" not in verify_proof(par_reqs[0].headers["DPoP"])[1]
    assert verify_proof(par_reqs[1].headers["DPoP"])[1]["nonce"] == "server-nonce"
    assert fa.par["login_hint"] == HANDLE and fa.par["code_challenge_method"] == "S256"
    assert nonces[ISSUER] == "server-nonce"


def run_login(fa: FakeAS, launcher) -> OAuthSession:
    http = fa.router.client()
    ident = resolve_identity(HANDLE, http)
    return OAuthLogin(http, ident, launcher, timeout=10, say=lambda s: None).run()


def test_full_oauth_login():
    fa = FakeAS()
    session = run_login(fa, make_launcher(fa))
    assert session.did == DID and session.issuer == ISSUER and session.access_token == "access-1"
    assert session.refresh_token == "refresh-1"
    assert session.client_id == build_client_id(session.redirect_uri)
    assert session.redirect_uri.startswith("http://127.0.0.1:") and session.redirect_uri.endswith("/callback")
    assert dpop.key_from_pem(session.dpop_private_key_pem)
    assert "access-1" not in repr(session) and "refresh-1" not in repr(session)


@pytest.mark.parametrize("override", [{"state": "evil"}, {"iss": "https://evil.example"}])
def test_oauth_rejects_bad_callback(override):
    fa = FakeAS()
    with pytest.raises(OAuthError):
        run_login(fa, make_launcher(fa, **override))
    assert fa.token_bodies == []  # code was never exchanged


def test_oauth_rejects_sub_mismatch():
    fa = FakeAS(sub="did:plc:someoneelse")
    with pytest.raises(OAuthError, match="subject"):
        run_login(fa, make_launcher(fa))


def test_oauth_timeout():
    fa = FakeAS()
    http = fa.router.client()
    ident = resolve_identity(HANDLE, http)
    with pytest.raises(OAuthError, match="timed out"):
        OAuthLogin(http, ident, lambda url: None, timeout=0.2, say=lambda s: None).run()


def test_adapter_oauth_login_persists_0600():
    fa = FakeAS()
    a = BlueskyAdapter(http=fa.router.client(), browser_launcher=make_launcher(fa))
    acct = a.login(HANDLE, "oauth")
    assert acct == Account("bluesky", DID, HANDLE)
    path = session_path(DID)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_session(DID).access_token == "access-1"


# ---------------------------------------------------------------- token refresh at resource server


def oauth_session(**kw) -> OAuthSession:
    base = dict(
        did=DID, handle=HANDLE, pds_url=PDS, issuer=ISSUER, token_endpoint=f"{ISSUER}/token",
        client_id=build_client_id("http://127.0.0.1:9/callback"), redirect_uri="http://127.0.0.1:9/callback",
        access_token="access-old", refresh_token="refresh-1", expires_at=time.time() + 3600,
        dpop_private_key_pem=dpop.key_to_pem(dpop.generate_key()), nonces={})
    base.update(kw)
    return OAuthSession(**base)


def test_refresh_on_401_and_persisted():
    fa = FakeAS(require_nonce=False)
    seen: list[str] = []

    def get_session(req: httpx.Request) -> httpx.Response:
        auth = req.headers["Authorization"]
        seen.append(auth)
        claims = check_proof(req, "GET", f"{PDS}/xrpc/com.atproto.server.getSession",
                             token=auth.removeprefix("DPoP "))
        if claims.get("nonce") != "pds-nonce":
            return httpx.Response(401, headers={"DPoP-Nonce": "pds-nonce",
                                                "WWW-Authenticate": 'DPoP error="use_dpop_nonce"'},
                                  json={"error": "use_dpop_nonce"})
        if auth != "DPoP access-2":
            return httpx.Response(401, headers={"WWW-Authenticate": 'DPoP error="invalid_token"'},
                                  json={"error": "InvalidToken"})
        return httpx.Response(200, json={"did": DID, "handle": HANDLE})

    fa.router.add("GET pds.example/xrpc/com.atproto.server.getSession", get_session)
    session = oauth_session()
    save_session(session)
    a = BlueskyAdapter(http=fa.router.client())
    assert a.current_account(DID) == Account("bluesky", DID, HANDLE)
    assert seen == ["DPoP access-old", "DPoP access-old", "DPoP access-2"]
    stored = load_session(DID)
    assert stored.access_token == "access-2" and stored.refresh_token == "refresh-2"


def test_unrecoverable_401_is_auth_required():
    fa = FakeAS(require_nonce=False)
    fa.router.add("GET pds.example/xrpc/com.atproto.server.getSession",
                  lambda q: httpx.Response(401, headers={"WWW-Authenticate": 'DPoP error="invalid_token"'},
                                           json={"error": "InvalidToken"}))
    save_session(oauth_session())
    with pytest.raises(AuthRequired):
        BlueskyAdapter(http=fa.router.client()).current_account(DID)


def test_current_account_without_session():
    with pytest.raises(AuthRequired, match="sociyx login bluesky"):
        BlueskyAdapter(http=Router().client()).current_account(DID)


# ---------------------------------------------------------------- app-password login


def test_app_password_login_with_2fa(caplog, capsys):
    r = Router()
    identity_routes(r)
    bodies: list[dict] = []

    def create(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "authFactorToken" not in body:
            return httpx.Response(401, json={"error": "AuthFactorTokenRequired", "message": "need code"})
        assert body["authFactorToken"] == "123456" and body["password"] == APP_PASSWORD
        return httpx.Response(200, json={"did": DID, "handle": HANDLE, "accessJwt": "eyJhbGciOi.aaaaaaaa.bbbbbbbb",
                                         "refreshJwt": "eyJhbGciOi.cccccccc.dddddddd"})

    r.add("POST pds.example/xrpc/com.atproto.server.createSession", create)
    secrets = iter([APP_PASSWORD, "123456"])
    calls: list[str] = []

    def prompt_secret(prompt: str) -> str:
        calls.append(prompt)
        return next(secrets)

    caplog.set_level(logging.DEBUG)
    a = BlueskyAdapter(http=r.client(), prompt_secret=prompt_secret)
    acct = a.login(HANDLE, "app-password")
    assert acct.account_id == DID and len(calls) == 2 and len(bodies) == 2
    out = capsys.readouterr()
    assert APP_PASSWORD not in caplog.text + out.out + out.err
    assert "123456" not in caplog.text + out.out + out.err
    path = session_path(DID)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert APP_PASSWORD not in path.read_text()
    s = load_session(DID)
    assert isinstance(s, PasswordSession) and s.access_jwt.startswith("eyJ")
    assert "eyJ" not in repr(s)


def test_app_password_wrong_password_does_not_leak():
    r = Router()
    identity_routes(r)
    r.add("POST pds.example/xrpc/com.atproto.server.createSession",
          lambda q: httpx.Response(401, json={"error": "AuthenticationRequired", "message": "Invalid identifier or password"}))
    a = BlueskyAdapter(http=r.client(), prompt_secret=lambda p: APP_PASSWORD)
    with pytest.raises(AuthRequired) as exc:
        a.login(HANDLE, "app-password")
    assert APP_PASSWORD not in str(exc.value) and APP_PASSWORD not in repr(exc.value)
    assert load_session(DID) is None


def test_app_password_did_mismatch_rejected():
    r = Router()
    identity_routes(r)
    r.add("POST pds.example/xrpc/com.atproto.server.createSession",
          lambda q: httpx.Response(200, json={"did": "did:plc:other", "handle": "x", "accessJwt": "a", "refreshJwt": "b"}))
    with pytest.raises(PermanentError):
        BlueskyAdapter(http=r.client(), prompt_secret=lambda p: APP_PASSWORD).login(HANDLE, "app-password")


def test_password_session_refreshes_on_expired_token():
    r = Router()
    save_session(PasswordSession(DID, HANDLE, PDS, "old-access", "old-refresh"))

    def get_session(req: httpx.Request) -> httpx.Response:
        if req.headers["Authorization"] == "Bearer old-access":
            return httpx.Response(400, json={"error": "ExpiredToken", "message": "expired"})
        return httpx.Response(200, json={"did": DID, "handle": HANDLE})

    def refresh(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer old-refresh"
        return httpx.Response(200, json={"accessJwt": "new-access", "refreshJwt": "new-refresh"})

    r.add("GET pds.example/xrpc/com.atproto.server.getSession", get_session)
    r.add("POST pds.example/xrpc/com.atproto.server.refreshSession", refresh)
    BlueskyAdapter(http=r.client()).current_account(DID)
    assert load_session(DID).access_jwt == "new-access"


# ---------------------------------------------------------------- scanning


def post_rec(n: int, did: str = DID) -> dict:
    return {"uri": f"at://{did}/app.bsky.feed.post/r{n}", "cid": f"cid{n}",
            "value": {"$type": "app.bsky.feed.post", "text": f"hello {n}", "createdAt": "2024-05-01T12:00:00.000Z"}}


def test_scan_pagination_unauthenticated():
    """No session on disk: public read against the PDS resolved from the DID."""
    r = Router()
    identity_routes(r)
    pages = {None: ([post_rec(1), post_rec(2)], "c1"), "c1": ([post_rec(3)], "c2"), "c2": ([post_rec(4)], None)}
    seen_params: list[dict] = []

    def list_records(req: httpx.Request) -> httpx.Response:
        assert "Authorization" not in req.headers
        q = dict(req.url.params)
        seen_params.append(q)
        assert q["repo"] == DID and q["collection"] == "app.bsky.feed.post" and q["limit"] == "100"
        recs, cur = pages[q.get("cursor")]
        body = {"records": recs}
        if cur:
            body["cursor"] = cur
        return httpx.Response(200, json=body)

    r.add("GET pds.example/xrpc/com.atproto.repo.listRecords", list_records)
    a = BlueskyAdapter(http=r.client())
    acct = Account("bluesky", DID, HANDLE)
    cursor, items = None, []
    for _ in range(3):
        page = a.scan_page(acct, "post", cursor)
        assert isinstance(page, ScanPage)
        items += page.items
        cursor = page.next_cursor
    assert cursor is None and [i.item_id.rsplit("/", 1)[1] for i in items] == ["r1", "r2", "r3", "r4"]
    assert [p.get("cursor") for p in seen_params] == [None, "c1", "c2"]
    first = items[0]
    assert first.version == "cid1" and first.text == "hello 1" and first.account_id == DID
    assert first.created_at.isoformat() == "2024-05-01T12:00:00+00:00"
    assert first.url == f"https://bsky.app/profile/{DID}/post/r1"
    assert first.raw["uri"] == first.item_id and first.raw["value"]["text"] == "hello 1"


def test_scan_mapping_of_each_type():
    r = Router()
    identity_routes(r)
    other_post = "at://did:plc:bob/app.bsky.feed.post/p9"
    records = {
        "like": {"subject": {"uri": other_post, "cid": "x"}, "createdAt": "2024-01-01T00:00:00Z"},
        "repost": {"subject": {"uri": other_post, "cid": "x"}, "createdAt": "2024-01-01T00:00:00+02:00"},
        "follow": {"subject": "did:plc:bob", "createdAt": "garbage"},
        "block": {"subject": "did:plc:bob", "createdAt": "2024-01-01T00:00:00Z"},
        "list": {"name": "Friends", "description": "close ones", "createdAt": "2024-01-01T00:00:00Z"},
        "listitem": {"subject": "did:plc:bob", "list": f"at://{DID}/app.bsky.graph.list/l1",
                     "createdAt": "2024-01-01T00:00:00Z"},
    }
    colls = {"like": "app.bsky.feed.like", "repost": "app.bsky.feed.repost", "follow": "app.bsky.graph.follow",
             "block": "app.bsky.graph.block", "list": "app.bsky.graph.list", "listitem": "app.bsky.graph.listitem"}

    def list_records(req: httpx.Request) -> httpx.Response:
        coll = req.url.params["collection"]
        t = next(k for k, v in colls.items() if v == coll)
        return httpx.Response(200, json={"records": [{"uri": f"at://{DID}/{coll}/k1", "cid": "cidk", "value": records[t]}]})

    r.add("GET pds.example/xrpc/com.atproto.repo.listRecords", list_records)
    a = BlueskyAdapter(http=r.client())
    acct = Account("bluesky", DID, HANDLE)
    got = {t: a.scan_page(acct, t, None).items[0] for t in colls}
    assert got["like"].url == "https://bsky.app/profile/did:plc:bob/post/p9" and got["like"].text == ""
    assert got["repost"].created_at.isoformat() == "2023-12-31T22:00:00+00:00"
    assert got["follow"].url == "https://bsky.app/profile/did:plc:bob" and got["follow"].created_at is None
    assert got["block"].url == "https://bsky.app/profile/did:plc:bob"
    assert got["list"].text == "Friends\nclose ones"
    assert got["listitem"].text == "" and got["listitem"].content_type == "listitem"
    with pytest.raises(PermanentError):
        a.scan_page(acct, "nonsense", None)


def test_scan_rejects_foreign_did_record():
    r = Router()
    identity_routes(r)
    r.add("GET pds.example/xrpc/com.atproto.repo.listRecords",
          lambda q: httpx.Response(200, json={"records": [post_rec(1, did="did:plc:mallory")]}))
    with pytest.raises(PermanentError, match="different repo"):
        BlueskyAdapter(http=r.client()).scan_page(Account("bluesky", DID, HANDLE), "post", None)


# ---------------------------------------------------------------- delete / verify


class FakePDS:
    def __init__(self) -> None:
        self.router = Router()
        self.records: dict[str, str] = {"r1": "cid1"}  # rkey -> cid
        self.delete_bodies: list[dict] = []
        self.delete_response: Callable[[], httpx.Response] | None = None
        self.get_response: Callable[[], httpx.Response] | None = None
        self.router.add("GET pds.example/xrpc/com.atproto.repo.getRecord", self._get)
        self.router.add("POST pds.example/xrpc/com.atproto.repo.deleteRecord", self._delete)
        identity_routes(self.router)
        save_session(PasswordSession(DID, HANDLE, PDS, "acc", "ref"))

    def _get(self, req: httpx.Request) -> httpx.Response:
        if self.get_response:
            return self.get_response()
        q = req.url.params
        assert q["repo"] == DID and q["collection"] == "app.bsky.feed.post"
        rkey = q["rkey"]
        if rkey not in self.records:
            return httpx.Response(400, json={"error": "RecordNotFound", "message": "Could not locate record"})
        return httpx.Response(200, json={"uri": f"at://{DID}/app.bsky.feed.post/{rkey}", "cid": self.records[rkey], "value": {}})

    def _delete(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.delete_bodies.append(body)
        if self.delete_response:
            return self.delete_response()
        del self.records[body["rkey"]]
        return httpx.Response(200, json={})

    def adapter(self) -> BlueskyAdapter:
        return BlueskyAdapter(http=self.router.client())


def item(rkey: str = "r1", cid: str = "cid1", did: str = DID, account: str = DID) -> ContentItem:
    return ContentItem("bluesky", account, f"at://{did}/app.bsky.feed.post/{rkey}", "post", None,
                       version=cid)


def test_delete_success_uses_swap_record():
    p = FakePDS()
    res = p.adapter().delete(item())
    assert res.status == DeleteStatus.DELETED
    assert p.delete_bodies == [{"repo": DID, "collection": "app.bsky.feed.post", "rkey": "r1", "swapRecord": "cid1"}]


def test_delete_already_deleted():
    p = FakePDS()
    assert p.adapter().delete(item("gone")).status == DeleteStatus.ALREADY_DELETED
    assert p.delete_bodies == []


def test_delete_changed_cid():
    p = FakePDS()
    p.records["r1"] = "cid-new"
    assert p.adapter().delete(item()).status == DeleteStatus.CHANGED
    assert p.delete_bodies == []


def test_delete_invalid_swap_race_reclassified():
    p = FakePDS()

    def swap_fail_gone() -> httpx.Response:
        p.records.pop("r1")
        return httpx.Response(400, json={"error": "InvalidSwap", "message": "swap"})

    p.delete_response = swap_fail_gone
    assert p.adapter().delete(item()).status == DeleteStatus.ALREADY_DELETED

    p2 = FakePDS()

    def swap_fail_changed() -> httpx.Response:
        p2.records["r1"] = "cid-other"
        return httpx.Response(400, json={"error": "InvalidSwap"})

    p2.delete_response = swap_fail_changed
    assert p2.adapter().delete(item()).status == DeleteStatus.CHANGED


def test_delete_foreign_repo_inaccessible():
    p = FakePDS()
    res = p.adapter().delete(item(did="did:plc:bob"))
    assert res.status == DeleteStatus.INACCESSIBLE and "another repo" in res.detail
    assert p.router.log == []  # no network call at all


def test_delete_forbidden_is_inaccessible():
    p = FakePDS()
    p.delete_response = lambda: httpx.Response(403, json={"error": "Forbidden", "message": "scope missing"})
    res = p.adapter().delete(item())
    assert res.status == DeleteStatus.INACCESSIBLE and "scope missing" in res.detail


def test_delete_rate_limited_retry_after_header():
    p = FakePDS()
    p.delete_response = lambda: httpx.Response(429, headers={"Retry-After": "42"}, json={"error": "RateLimitExceeded"})
    with pytest.raises(RateLimited) as e:
        p.adapter().delete(item())
    assert e.value.retry_after == 42


def test_delete_rate_limited_ratelimit_reset():
    p = FakePDS()
    reset = str(int(time.time()) + 100)
    p.delete_response = lambda: httpx.Response(429, headers={"ratelimit-reset": reset})
    with pytest.raises(RateLimited) as e:
        p.adapter().delete(item())
    assert 90 <= e.value.retry_after <= 101


def test_rate_limited_without_hints():
    p = FakePDS()
    p.delete_response = lambda: httpx.Response(429)
    with pytest.raises(RateLimited) as e:
        p.adapter().delete(item())
    assert e.value.retry_after is None


def test_delete_502_is_transient():
    p = FakePDS()
    p.delete_response = lambda: httpx.Response(502, text="bad gateway")
    with pytest.raises(TransientError):
        p.adapter().delete(item())


def test_transport_error_is_transient():
    p = FakePDS()

    def boom() -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    p.get_response = boom
    with pytest.raises(TransientError):
        p.adapter().delete(item())


def test_delete_unrecoverable_401_is_auth_required():
    p = FakePDS()
    p.get_response = lambda: httpx.Response(401, json={"error": "AuthenticationRequired", "message": "no"})
    with pytest.raises(AuthRequired):
        p.adapter().delete(item())


def test_delete_without_session_is_auth_required():
    p = FakePDS()
    from sociyx.adapters.bluesky.session import delete_session
    delete_session(DID)
    with pytest.raises(AuthRequired):
        p.adapter().delete(item())


def test_other_xrpc_errors_expose_error_name():
    p = FakePDS()
    p.get_response = lambda: httpx.Response(400, json={"error": "InvalidRequest", "message": "bad"})
    with pytest.raises(XrpcError) as e:
        p.adapter().delete(item())
    assert e.value.error == "InvalidRequest" and e.value.status == 400


def test_verify_deleted():
    p = FakePDS()
    a = p.adapter()
    assert a.verify_deleted(item()) == VerifyStatus.PRESENT
    assert a.verify_deleted(item("nope")) == VerifyStatus.GONE
    p.get_response = lambda: httpx.Response(500)
    assert a.verify_deleted(item()) == VerifyStatus.UNKNOWN
    p.get_response = lambda: httpx.Response(400, json={"error": "InvalidRequest"})
    assert a.verify_deleted(item()) == VerifyStatus.UNKNOWN


def test_verify_without_session_uses_public_read():
    p = FakePDS()
    from sociyx.adapters.bluesky.session import delete_session
    delete_session(DID)
    assert p.adapter().verify_deleted(item("nope")) == VerifyStatus.GONE


# ---------------------------------------------------------------- logout / misc


def test_logout_revokes_and_deletes_oauth_session():
    fa = FakeAS()
    save_session(oauth_session(revocation_endpoint=f"{ISSUER}/revoke"))
    BlueskyAdapter(http=fa.router.client()).logout(DID)
    assert any(q.url.path == "/revoke" for q in fa.router.log)
    assert load_session(DID) is None


def test_logout_survives_network_failure():
    save_session(PasswordSession(DID, HANDLE, PDS, "a", "b"))

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    BlueskyAdapter(http=httpx.Client(transport=httpx.MockTransport(boom))).logout(DID)
    assert not session_path(DID).exists()


def test_class_metadata():
    assert BlueskyAdapter.platform == "bluesky" and BlueskyAdapter.min_delete_interval == 1.0
    assert set(BlueskyAdapter.content_types) == BlueskyAdapter.deletable_types
    assert "DMs" in BlueskyAdapter.discovery_notes or "direct messages" in BlueskyAdapter.discovery_notes


def test_xrpc_client_get_drops_none_params():
    r = Router()
    seen = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={})

    r.add("GET pds.example/xrpc/x.y", h)
    XrpcClient(PDS, http=r.client()).get("x.y", a="1", cursor=None)
    assert seen == {"a": "1"}
