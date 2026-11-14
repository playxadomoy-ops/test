"""Discovery of the home PC's CURRENT public gateway address (Cloudflare Quick Tunnel URLs change on every restart).

    Worker  GET <bootstrap_url>/v1/gateway  ->  {"v":1,"url":"https://<name>.trycloudflare.com",...}

Trust model: the Worker answer is only a HINT about where to knock. It is validated syntactically here and the server
behind it must still prove its identity through the certificate-pinned end-to-end handshake
(``ServerClient._probe`` -> ``SecureApiClient.ensure_session``) before anything is sent. No update credential exists on the
phone: the phone can only READ the registry.
"""

from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from app.services.tunnel_url import validate_tunnel_url

_WORKER_PATH = "/v1/gateway"
_MAX_RESPONSE_BYTES = 4096
_FETCH_TIMEOUT_SECONDS = 5.0

UrlValidator = Callable[[object], Optional[str]]


def is_usable_bootstrap_url(url: str) -> bool:
    """A bootstrap URL is an https URL of the Worker (workers.dev or a custom domain); never a placeholder."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and bool(host) and not host.endswith(".example") and host != "example"


class GatewayBootstrap:
    """Resolves the list of gateway base URLs to try, freshest first: Worker answer, then the last URL that worked."""

    def __init__(self, bootstrap_url: str, cache, validator: UrlValidator = validate_tunnel_url,
                 timeout: float = _FETCH_TIMEOUT_SECONDS) -> None:
        self._url = bootstrap_url.rstrip("/")
        self._cache = cache
        self._validate = validator
        self._timeout = timeout
        self.last_fetch_failed = False   # True when the Worker could not be reached/understood on the last candidates() call

    @property
    def configured(self) -> bool:
        return is_usable_bootstrap_url(self._url)

    async def fetch_worker_url(self, client: httpx.AsyncClient) -> Optional[str]:
        """Ask the Worker. Any failure (network, status, size, JSON, invalid URL) -> None; never raises."""
        try:
            resp = await client.get(self._url + _WORKER_PATH, timeout=self._timeout, follow_redirects=False,
                                    headers={"accept": "application/json"})
        except (httpx.TransportError, httpx.TimeoutException, httpx.InvalidURL):
            return None
        if resp.status_code != 200 or len(resp.content) > _MAX_RESPONSE_BYTES:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or body.get("v") != 1:
            return None
        return self._validate(body.get("url"))

    async def candidates(self, client: httpx.AsyncClient) -> list[str]:
        fresh = await self.fetch_worker_url(client)
        self.last_fetch_failed = fresh is None
        cached_raw = await self._cache.load()
        cached = self._validate(cached_raw) if cached_raw else None   # a corrupted/poisoned cache entry is simply ignored
        out: list[str] = []
        for url in (fresh, cached):
            if url and url not in out:
                out.append(url)
        return out

    async def remember(self, url: str) -> None:
        """Called ONLY after the server behind ``url`` proved its identity."""
        if await self._cache.load() != url:
            await self._cache.save(url)
