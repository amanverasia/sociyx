"""Scrub credentials from anything that might end up in logs or error output."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    # JSON-ish "key": "value" for sensitive keys
    re.compile(
        r'(?i)(["\']?(?:access_?jwt|refresh_?jwt|access_token|refresh_token|id_token|password|'
        r'app_?password|code_verifier|client_secret|authorization|cookie|dpop|auth_?factor_?token|'
        r'session(?:id)?|csrf_?token|ct0|code)["\']?\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,&}]+)'
    ),
    # HTTP auth headers
    re.compile(r"(?i)\b(Bearer|DPoP|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    # Anything that looks like a JWT
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # Bluesky app passwords: xxxx-xxxx-xxxx-xxxx
    re.compile(r"\b[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b"),
]


def scrub(text: str) -> str:
    # Scheme+token first: otherwise "Authorization: Bearer xyz" has only "Bearer" masked by the
    # key/value pattern and the token itself leaks.
    text = _PATTERNS[1].sub(lambda m: m.group(1) + " [REDACTED]", text)
    text = _PATTERNS[0].sub(lambda m: m.group(1) + "[REDACTED]", text)
    for pattern in _PATTERNS[2:]:
        text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs full URLs at INFO; keep it quiet unless debugging.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
