"""One shared network policy for Gabriel catalog/resource HTTP.

Credential-free public HTTPS only; no localhost/private/internal
literal or DNS-resolved non-global endpoints; redirects rejected;
``trust_env=False``; response streamed and capped *before* full
buffering; bounded time; no retries. DNS failure fails closed.

Residual risk (documented, not solved here): pre-resolution does not
pin the address, so DNS rebinding/TOCTOU remains possible -- the
transport may resolve a different address at connect time; a pinned/
verified transport would be needed to close that.

Transports/resolvers are injectable callables for offline tests:
``transport(url, params, timeout)`` returns an object/dict with
``status_code`` and ``iter_bytes()``/``content``, or raw bytes.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urlparse


class HttpPolicyError(ValueError):
    """Endpoint or response refused by the shared network policy."""


def validate_endpoint(raw: str, *, name: str = "endpoint",
                      resolver: Callable[..., Any] | None = None) -> str:
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HttpPolicyError(f"{name} must be a credential-free https URL")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise HttpPolicyError(f"{name} must not name a private/internal endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise HttpPolicyError(f"{name} must not name a private/internal endpoint")
        return url
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, 443, proto=socket.IPPROTO_TCP)
    except Exception as exc:
        raise HttpPolicyError(f"{name} DNS resolution failed; refusing without network") from exc
    if not infos:
        raise HttpPolicyError(f"{name} DNS resolution returned no addresses; refusing without network")
    for info in infos:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            continue
        if not resolved.is_global:
            raise HttpPolicyError(f"{name} must not resolve to a private/internal address")
    return url


def _parts(resp: Any) -> tuple[int, Any]:
    if isinstance(resp, dict):
        return int(resp.get("status_code", 0)), resp.get("content", resp.get("text", b""))
    return int(getattr(resp, "status_code", 0)), resp


def fetch_bytes(url: str, *, params: dict | None = None, timeout: float = 10.0,
                max_bytes: int = 1_000_000, transport: Callable[..., Any] | None = None,
                resolver: Callable[..., Any] | None = None,
                name: str = "endpoint") -> bytes:
    """GET ``url`` under the shared policy; return bounded body bytes."""
    validate_endpoint(url, name=name, resolver=resolver)
    if transport is None:
        import httpx

        def transport(u, params=None, timeout=None):  # type: ignore[misc]
            client = httpx.Client(trust_env=False, follow_redirects=False,
                                  headers={"User-Agent": "GrowUp-Gabriel/0.3"})
            return _HttpxStream(client, u, params, timeout)
    try:
        resp = transport(url, params=params, timeout=timeout)
    except HttpPolicyError:
        raise
    except Exception as exc:
        raise HttpPolicyError(f"{name} transport failed: {type(exc).__name__}") from exc
    if isinstance(resp, (bytes, bytearray)):
        body = bytes(resp)
        if len(body) > max_bytes:
            raise HttpPolicyError(f"{name} response exceeds the byte bound")
        return body
    status, raw = _parts(resp)
    if status != 200:
        closer = getattr(resp, "close", None)
        if callable(closer):
            closer()
        if 300 <= status < 400:
            raise HttpPolicyError(f"{name} redirects are not followed")
        raise HttpPolicyError(f"{name} returned HTTP {status}")
    if isinstance(raw, (bytes, bytearray, str)):
        body = raw.encode() if isinstance(raw, str) else bytes(raw)
        if len(body) > max_bytes:
            raise HttpPolicyError(f"{name} response exceeds the byte bound")
        return body
    streamer = getattr(raw, "iter_bytes", None) or getattr(raw, "iter_content", None)
    if callable(streamer):
        chunks: list[bytes] = []
        total = 0
        for chunk in streamer():
            data = chunk.encode() if isinstance(chunk, str) else bytes(chunk or b"")
            total += len(data)
            if total > max_bytes:
                raise HttpPolicyError(f"{name} response exceeds the byte bound")
            chunks.append(data)
        return b"".join(chunks)
    body = raw.get("content", raw.get("text", b"")) if isinstance(raw, dict) else getattr(raw, "content", None)
    if body is None:
        body = getattr(raw, "text", "")
    if isinstance(body, str):
        body = body.encode()
    body = bytes(body or b"")
    if len(body) > max_bytes:
        raise HttpPolicyError(f"{name} response exceeds the byte bound")
    return body


class _HttpxStream:
    """Lazily-streaming httpx response wrapper (cap enforced by caller)."""

    def __init__(self, client: Any, url: str, params: Any, timeout: Any) -> None:
        self._client = client
        self._ctx = client.stream("GET", url, params=params, timeout=timeout)
        self._resp = self._ctx.__enter__()
        self.status_code = self._resp.status_code
        self.headers = dict(self._resp.headers)

    def iter_bytes(self):  # type: ignore[no-untyped-def]
        try:
            yield from self._resp.iter_bytes(chunk_size=65536)
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._ctx.__exit__(None, None, None)
        finally:
            self._client.close()
