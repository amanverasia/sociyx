"""DPoP (RFC 9449) proofs with ES256, plus PKCE helpers. Uses `cryptography` only."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_to_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def key_from_pem(pem: str) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("DPoP key is not an EC key")
    return key


def public_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    nums = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
    }


def _sign_es256(key: ec.EllipticCurvePrivateKey, signing_input: bytes) -> bytes:
    """JWS wants raw r||s (32+32 bytes), not the DER encoding cryptography returns."""
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def make_proof(
    key: ec.EllipticCurvePrivateKey,
    htm: str,
    htu: str,
    nonce: str | None = None,
    access_token: str | None = None,
) -> str:
    """Build a DPoP proof JWT. htu is stripped of query/fragment as the RFC requires;
    a fresh random jti makes every proof unique."""
    parts = urlsplit(htu)
    clean_htu = f"{parts.scheme}://{parts.netloc}{parts.path}"
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": public_jwk(key)}
    claims: dict[str, object] = {
        "jti": b64url(secrets.token_bytes(16)),
        "htm": htm.upper(),
        "htu": clean_htu,
        "iat": int(time.time()),
    }
    if nonce:
        claims["nonce"] = nonce
    if access_token:
        claims["ath"] = b64url(hashlib.sha256(access_token.encode("ascii")).digest())
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    return signing_input + "." + b64url(_sign_es256(key, signing_input.encode("ascii")))


# ---- PKCE (RFC 7636, S256 only) ----


def new_code_verifier() -> str:
    return b64url(secrets.token_bytes(48))  # 64 chars, within the 43..128 limit


def code_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    return b64url(secrets.token_bytes(24))
