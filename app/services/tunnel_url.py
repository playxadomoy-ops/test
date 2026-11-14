"""Strict validation of a Cloudflare Quick Tunnel URL. Mirrors cloudflare/worker/src/index.js (keep both in sync).

Accepted: ``https://<one-label>.trycloudflare.com`` and nothing else -- no port, userinfo, path, query or fragment.
Anything that came from the network (Worker answer, cache) must pass this before it is ever used as a base URL.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit

_SUFFIX = ".trycloudflare.com"
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
#: Hostnames that belong to Cloudflare itself (cloudflared error messages mention https://api.trycloudflare.com).
_RESERVED_LABELS = frozenset({"api", "www", "dash", "admin", "mail", "ftp", "status", "support"})
_FORBIDDEN_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f\\@]")


def validate_tunnel_url(raw: object) -> Optional[str]:
    """Return the normalised origin (``https://x.trycloudflare.com``) or None if ``raw`` is not an acceptable tunnel URL."""
    if not isinstance(raw, str) or not raw or len(raw) > 200 or _FORBIDDEN_CHARS_RE.search(raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.query or parts.fragment:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/"):
        return None
    host = (parts.hostname or "").lower()
    if not host.endswith(_SUFFIX):
        return None
    label = host[: -len(_SUFFIX)]
    if not _LABEL_RE.match(label) or label in _RESERVED_LABELS:
        return None
    return f"https://{host}"
