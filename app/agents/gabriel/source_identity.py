"""Canonical source-host identity (B5-2A step 2A.1).

One shared hostname canonicalization used by every domain producer and
corroboration consumer. Canonicalization answers only "same hostname
representation" -- never "same publisher", never "authentic source",
never "same report". Syndication detection is explicitly out of scope.

Fail-closed throughout: anything that cannot be validated as a hostname
yields "", and "" never counts as a corroborating voice.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_DEFAULT_PORTS = {"http": 80, "https": 443}

# Lowercase ASCII hostnames after IDNA encoding: letters, digits, dots,
# hyphens, underscores (common in practice), and colons (IPv6 literals).
_HOST_CHARS_RE = re.compile(r"[a-z0-9._\-:]+")

# Distinct textual forms of one IPv6 address (e.g. "::1" vs the
# fully expanded form) are NOT unified: textual comparison only.
# This is a documented approximation, not an equivalence proof.


def _normalize_host(host: str) -> str:
    """Validate and normalize one hostname string, or return ""."""
    if not isinstance(host, str):
        return ""
    cleaned = host.strip().lower().strip(".")
    if not cleaned:
        return ""
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError:
        try:
            cleaned = cleaned.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return ""
    if not _HOST_CHARS_RE.fullmatch(cleaned):
        return ""
    if ".." in cleaned or cleaned.startswith(("-", "_")) or cleaned.endswith("-"):
        return ""
    return cleaned


def _has_forbidden_chars(text: str) -> bool:
    """Whether text holds ASCII whitespace/control characters.

    ``urlparse`` silently elides ``\\n\\t\\r`` before validation, so
    malformed input must be rejected before parsing, not after.
    """
    return any(ord(char) < 0x21 or ord(char) == 0x7F for char in text)


def _with_port(normalized: str, port: int | None, defaults: set[int]) -> str:
    """Append a non-default port to a normalized host (IPv6 bracketed)."""
    if port is not None and port not in defaults:
        if ":" in normalized:
            return f"[{normalized}]:{port}"
        return f"{normalized}:{port}"
    return normalized


def _split_authority(parsed) -> tuple[str, int | None] | None:
    """Extract (host, port) from a parsed URL, or None when unusable."""
    try:
        host = parsed.hostname or ""
    except ValueError:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    return host, port


def canonical_hostname(url: str) -> str:
    """Derive the canonical hostname from a URL, or "" when unusable.

    Lowercases, strips userinfo/trailing dots/default ports, keeps
    non-default ports, IDNA-encodes internationalized names, and
    ignores paths, queries, and fragments. Only http/https URLs are
    accepted. No network access, no redirects, no authority inference.
    """
    if not isinstance(url, str):
        return ""
    stripped = url.strip()
    if not stripped or _has_forbidden_chars(stripped):
        return ""
    try:
        parsed = urlparse(stripped)
    except (ValueError, TypeError):
        return ""
    scheme = (parsed.scheme or "").lower()
    if scheme not in _DEFAULT_PORTS:
        return ""
    split = _split_authority(parsed)
    if split is None:
        return ""
    host, port = split
    normalized = _normalize_host(host)
    if not normalized:
        return ""
    return _with_port(normalized, port, {_DEFAULT_PORTS[scheme]})


def canonical_host_text(text: str) -> str:
    """Canonicalize a bare hostname or URL string, or return "".

    Accepts the values historically stored in ``domain`` fields (bare
    hosts, hosts with ports) as well as full URLs. Bare host text
    carries no scheme, so both conventional default ports (80, 443)
    are dropped: unification errs toward fewer corroboration voices,
    never more.
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if "://" in cleaned:
        return canonical_hostname(cleaned)
    if _has_forbidden_chars(cleaned):
        return ""
    try:
        parsed = urlparse("http://" + cleaned)
    except (ValueError, TypeError):
        return ""
    split = _split_authority(parsed)
    if split is None:
        return ""
    host, port = split
    normalized = _normalize_host(host)
    if not normalized:
        return ""
    return _with_port(normalized, port, set(_DEFAULT_PORTS.values()))


def evidence_host(domain: str | None, url: str | None) -> str:
    """Canonical corroboration identity for one evidence item.

    A valid source URL determines the identity: the canonical host is
    derived from the URL (the primary provenance) whether the supplied
    domain agrees, differs only in representation, conflicts, or is
    malformed or missing. A conflicting domain can never create an
    additional voice. Only when the URL is missing or invalid does a
    validated supplied domain serve as a fallback. A raw URL string is
    never returned, and "" must never be counted as a corroborating
    voice. Hostname identity implies nothing about URL, content, or
    publisher authenticity.
    """
    if isinstance(url, str) and url.strip():
        url_host = canonical_hostname(url)
        if url_host:
            return url_host
    if isinstance(domain, str) and domain.strip():
        return canonical_host_text(domain)
    return ""
