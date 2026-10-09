"""Outbound-request and API-access protection.

The agent fetches URLs that come from outside (merchant discovery) and later
sends delivery details to those merchants, so two things matter:

* **SSRF** - a caller must not be able to make the agent request internal
  addresses (cloud metadata, localhost, private networks). :class:`MerchantURLGuard`
  checks every outbound request: only http(s), no credentials in the URL, and
  every address the host resolves to must be public, unless the origin was
  configured by the operator (``KNOWN_MERCHANT_URLS``).
* **Admin actions** - registering merchants is an operator action, protected
  by an API key compared in constant time.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

Resolver = Callable[[str, int], Awaitable[list[str]]]


class MerchantURLError(ValueError):
    """The URL is not allowed as a merchant address."""


async def system_resolver(host: str, port: int) -> list[str]:
    """Resolve *host* to IP address strings using the system resolver."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def origin_of(url: str) -> str:
    """Return ``scheme://host:port`` for *url* (lower-case, default ports filled in)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


class MerchantURLGuard:
    """Decides whether the agent may send a request to a given URL."""

    def __init__(
        self,
        trusted_urls: Iterable[str] = (),
        allow_private_hosts: bool = False,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._trusted = {origin_of(u) for u in trusted_urls}
        self._allow_private = allow_private_hosts
        self._resolver = resolver

    async def check(self, url: str) -> None:
        """Raise :class:`MerchantURLError` unless *url* may be requested."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise MerchantURLError("Merchant URL is not valid.") from exc
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise MerchantURLError("Merchant URL must be an http(s) URL with a host.")
        if parts.username or parts.password:
            raise MerchantURLError("Merchant URL must not contain credentials.")

        if origin_of(url) in self._trusted or self._allow_private:
            return

        if parts.scheme.lower() != "https":
            raise MerchantURLError("Untrusted merchants must use https.")

        host = parts.hostname
        if host.lower() == "localhost" or host.lower().endswith(".localhost"):
            raise MerchantURLError("Merchant host is not a public address.")
        try:
            ipaddress.ip_address(host)
            addresses = [host]  # an IP literal is checked directly, never resolved
        except ValueError:
            try:
                addresses = await self._resolver(host, port or 443)
            except OSError as exc:
                raise MerchantURLError("Merchant host could not be resolved.") from exc
        if not addresses or not all(_is_public(a) for a in addresses):
            raise MerchantURLError("Merchant host is not a public address.")


def make_admin_dependency(admin_api_key: str) -> Callable[..., Awaitable[None]]:
    """Build a FastAPI dependency that requires the admin API key.

    With no key configured the protected routes are disabled (403) rather than
    left open.
    """

    async def require_admin(request: Request, x_api_key: str | None = Header(default=None)) -> None:
        if not admin_api_key:
            raise HTTPException(
                status_code=403,
                detail="Admin endpoints are disabled: set ADMIN_API_KEY to enable them.",
            )
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), admin_api_key.encode()):
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")

    return require_admin
