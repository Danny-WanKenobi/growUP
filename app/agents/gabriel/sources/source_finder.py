"""Bounded candidate finding for Gabriel source selection.

Provider order (owner-directed): P1 configured official catalog API,
P2 configured/approved SearXNG, P3 budget-approved Brave fallback.
Default network count is zero: finding requires explicit mission
approval and a positive request budget, and refuses when no safe
configured provider exists. Unknown fields stay unknown; a search
result or ``.gov`` spelling never proves publisher authority, and
nothing here enrolls a source in the trusted registry.

HTTP is injectable for tests: ``http_get(url, params, timeout)`` must
return an object/dict with ``status_code``, ``headers`` and ``content``
(bytes) or ``text``. Redirects are never followed.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from typing import Any, Callable
from urllib.parse import urlparse

from app.agents.gabriel.sources.qualification import SourceCandidate
from app.agents.gabriel.sources.source_selection import (
    FoundCandidate, SourceMission, SourceSelectionError, UncertainFindingError,
)

CATALOG_URL_ENV = "GABRIEL_CATALOG_API_URL"
SEARXNG_URL_ENV = "GABRIEL_SEARXNG_URL"
HttpGet = Callable[..., Any]


def _validate_endpoint(raw: str, *, name: str) -> str:
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SourceSelectionError(f"{name} must be a credential-free https URL")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        raise SourceSelectionError(f"{name} must not name a private/internal endpoint")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise SourceSelectionError(f"{name} must not name a private/internal endpoint")
        return url.rstrip("/")
    # DNS-resolved addresses are validated as well as literals: any
    # resolved non-global address refuses before connecting. DNS
    # failure fails closed (no transmission). Residual risk, stated
    # honestly: pre-resolution does not solve DNS rebinding/TOCTOU --
    # the transport may resolve a different address at connect time;
    # pinning/verified transport would be needed to close that.
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise SourceSelectionError(
            f"{name} DNS resolution failed; refusing without network") from exc
    if not infos:
        raise SourceSelectionError(
            f"{name} DNS resolution returned no addresses; refusing without network")
    for info in infos or []:
        try:
            resolved = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError, TypeError):
            continue
        if not resolved.is_global:
            raise SourceSelectionError(f"{name} must not resolve to a private/internal address")
    return url.rstrip("/")


def _response_parts(resp: Any) -> tuple[int, dict, bytes]:
    if isinstance(resp, dict):
        status, headers, body = resp.get("status_code", 0), resp.get("headers") or {}, resp.get("content", resp.get("text", b""))
    else:
        status, headers, body = getattr(resp, "status_code", 0), getattr(resp, "headers", {}) or {}, getattr(resp, "content", None)
        if body is None:
            body = getattr(resp, "text", "")
    if isinstance(body, str):
        body = body.encode()
    return int(status), dict(headers), bytes(body or b"")


def _read_body(resp: Any, bound: int) -> bytes:
    """Read a (possibly streaming) body, enforcing the bound before buffering."""
    streamer = getattr(resp, "iter_bytes", None) or getattr(resp, "iter_content", None)
    if callable(streamer) and not isinstance(resp, dict):
        chunks: list[bytes] = []
        total = 0
        for chunk in streamer():
            data = chunk.encode() if isinstance(chunk, str) else bytes(chunk or b"")
            total += len(data)
            if total > bound:
                raise SourceSelectionError("source finding response exceeds the byte bound")
            chunks.append(data)
        return b"".join(chunks)
    _status, _headers, body = _response_parts(resp)
    if len(body) > bound:
        raise SourceSelectionError("source finding response exceeds the byte bound")
    return body


_EVIDENCE_EXCERPT_BYTES = 8192


def _record_evidence(sink: list | None, *, provider: str, url: str,
                     status: int | None, body: bytes, error: str = "") -> None:
    if sink is None:
        return
    import hashlib
    sink.append({
        "provider": provider, "url_host": urlparse(url).hostname or "",
        "status": status, "response_bytes": len(body),
        "response_digest": hashlib.sha256(body).hexdigest(),
        "response_text": body.decode("utf-8", "replace"),
        "response_excerpt": body[:_EVIDENCE_EXCERPT_BYTES].decode("utf-8", "replace"),
        "truncated": len(body) > _EVIDENCE_EXCERPT_BYTES,
        "error": error,
    })


def _get_json(url: str, params: dict, mission: SourceMission, http_get: HttpGet | None,
              *, evidence_sink: list | None = None, provider: str = "") -> Any:
    if http_get is None:
        import httpx
        def http_get(u, params=None, timeout=None):  # type: ignore[misc]
            with httpx.Client(trust_env=False, follow_redirects=False,
                              headers={"User-Agent": "GrowUp-Gabriel/0.3 source-finder"}) as client:
                with client.stream("GET", u, params=params, timeout=timeout) as resp:
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > mission.budget.max_response_bytes:
                            break
                    return {"status_code": resp.status_code, "headers": dict(resp.headers), "content": body}
    try:
        resp = http_get(url, params=params, timeout=mission.budget.timeout_seconds)
    except Exception as exc:
        # Transport raised: the request may or may not have reached the
        # provider, so the attempt is uncertain and must never be
        # retried or silently fallen back from.
        _record_evidence(evidence_sink, provider=provider, url=url, status=None,
                         body=b"", error=type(exc).__name__)
        raise UncertainFindingError(
            "source finding transmission outcome is uncertain; "
            "not retried and no further provider calls are made") from exc
    status, _headers, _ = _response_parts(resp) if not callable(getattr(resp, "iter_bytes", None)) else (
        int(getattr(resp, "status_code", 0)), dict(getattr(resp, "headers", {}) or {}), b"")
    body = _read_body(resp, mission.budget.max_response_bytes)
    _record_evidence(evidence_sink, provider=provider, url=url, status=status, body=body)
    if 300 <= status < 400:
        raise SourceSelectionError("redirects are not followed for source finding")
    if status != 200:
        raise SourceSelectionError(f"source finding provider returned HTTP {status}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceSelectionError("source finding response is not valid JSON") from exc


def _bool_or(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _candidate(raw: dict, mission: SourceMission, *, tier: int, provider: str) -> FoundCandidate:
    domain = str(raw.get("publisher_domain") or "").strip()
    url = str(raw.get("provenance_url") or raw.get("url") or "").strip()
    if not domain and url:
        domain = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    age = raw.get("update_age_days")
    record_types = raw.get("record_types")
    formats = raw.get("formats")
    candidate = SourceCandidate(
        name=str(raw.get("name") or raw.get("title") or domain or "unnamed candidate")[:200],
        publisher_name=str(raw.get("publisher_name") or raw.get("publisher") or "")[:200],
        publisher_domain=domain[:200], claimed_official=_bool_or(raw.get("claimed_official"), False),
        # Observed metadata only: jurisdiction/country are never filled
        # from the search query or mission context.
        jurisdiction=str(raw.get("jurisdiction") or "")[:200],
        country=str(raw.get("country") or "")[:20],
        record_types=tuple(str(x) for x in record_types)[:8] if isinstance(record_types, list) else (),
        access_method=str(raw.get("access_method") or "portal_search")[:50],
        free=_bool_or(raw.get("free"), True), requires_auth=_bool_or(raw.get("requires_auth"), False),
        robots_restricted=_bool_or(raw.get("robots_restricted"), False),
        license_clear=_bool_or(raw.get("license_clear"), False),
        formats=tuple(str(x) for x in formats)[:8] if isinstance(formats, list) else (),
        property_level=_bool_or(raw.get("property_level"), True),
        update_age_days=age if isinstance(age, int) and not isinstance(age, bool) else None,
        provenance_url=url[:500],
    )
    adapter = raw.get("adapter") or raw.get("adapter_name")
    return FoundCandidate(candidate=candidate, tier=tier, provider=provider,
                          adapter=str(adapter)[:100] if adapter else None,
                          evidence_ref=(url or domain)[:500])


# License identifiers whose terms are known-clear without further
# review. Any other observed license id is recorded as observed only
# (license_clear stays False); absence never becomes clearance.
_KNOWN_CLEAR_LICENSES = frozenset({
    "cc0-1.0", "cc0", "cc-by-4.0", "cc-by", "odc-by-1.0", "odc-by",
    "open-government-licence", "ogl-uk-3.0",
})


def _extras_map(pkg: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    extras = pkg.get("extras")
    if isinstance(extras, list):
        for item in extras:
            if isinstance(item, dict) and item.get("key") is not None:
                out[str(item["key"]).strip().lower()] = str(item.get("value") or "")
    return out


def _observed_age_days(pkg: dict, extras: dict[str, str]) -> int | None:
    """Age in days from observed modification metadata, else unknown."""
    from datetime import datetime, timezone
    raw = str(pkg.get("metadata_modified") or extras.get("metadata_modified")
              or extras.get("last_modified") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed).days
    return age if age >= 0 else 0


def _ckan_rows(payload: Any, mission: SourceMission) -> list[FoundCandidate] | None:
    """Parse an actual CKAN ``package_search`` response, observed fields only.

    Supported observed metadata is extracted: jurisdiction/country and
    record types from package fields or ``extras``, freshness from
    observed modification timestamps, and licensing only when the
    observed license id is a known-clear identifier. Anything absent
    stays unknown (``license_clear=False``, freshness ``None``,
    jurisdiction ``""``) and therefore routes to review -- observed
    metadata never enrolls a publisher or invents clearance.
    """
    if not isinstance(payload, dict) or payload.get("success") is False:
        return None
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return None
    found: list[FoundCandidate] = []
    for pkg in result["results"]:
        if not isinstance(pkg, dict):
            continue
        org = pkg.get("organization") if isinstance(pkg.get("organization"), dict) else {}
        resources = [r for r in (pkg.get("resources") or []) if isinstance(r, dict)]
        first = resources[0] if resources else {}
        url = str(first.get("url") or pkg.get("url") or "").strip()
        extras = _extras_map(pkg)
        record_types = pkg.get("record_types")
        if not isinstance(record_types, list):
            extra_rts = extras.get("record_types") or extras.get("record_type") or ""
            record_types = [x.strip() for x in extra_rts.split(",") if x.strip()]
        license_id = str(pkg.get("license_id") or extras.get("license_id")
                         or extras.get("license") or "").strip()
        raw = {
            "name": pkg.get("title") or pkg.get("name"),
            "publisher_name": org.get("title") or org.get("name") or "",
            "provenance_url": url,
            "formats": [str(r.get("format")) for r in resources if r.get("format")],
            "record_types": record_types,
            "access_method": "api",
            "jurisdiction": str(pkg.get("jurisdiction") or extras.get("jurisdiction")
                                or extras.get("spatial_coverage") or "")[:200],
            "country": str(pkg.get("country") or extras.get("country") or "")[:20],
            "license_clear": license_id.lower() in _KNOWN_CLEAR_LICENSES,
            "update_age_days": _observed_age_days(pkg, extras),
        }
        found.append(_candidate(raw, mission, tier=1, provider="official_catalog"))
    return found


def find_candidates(mission: SourceMission, *, http_get: HttpGet | None = None,
                    brave_execute: Callable[..., Any] | None = None,
                    reserve: Callable[..., Any] | None = None,
                    env: Any = None,
                    evidence_sink: list | None = None) -> list[FoundCandidate]:
    """Find candidates with P1 -> P2 -> P3 precedence, bounded in total.

    Fallback happens only on a definitive provider failure (endpoint
    refused before transmission, DNS/validation refusal, or an HTTP
    error response) and only while the approved total request limit
    (``max_requests``) still has room. A successful response -- even an
    empty one -- ends finding. An uncertain transport outcome raises
    :class:`UncertainFindingError` immediately: no fallback, no extra
    calls. If no fallback is authorized (no further configured provider
    within limits / no paid allowance), the original refusal stands.
    """
    mission.validate()
    if not mission.approved:
        raise SourceSelectionError("source finding requires explicit approval")
    if mission.budget.max_requests < 1:
        raise SourceSelectionError("source finding budget allows zero requests; refusing without network")
    env = os.environ if env is None else env
    catalog = str(env.get(CATALOG_URL_ENV, "") or "").strip()
    searxng = str(env.get(SEARXNG_URL_ENV, "") or "").strip()
    query = f"{mission.jurisdiction} official {' '.join(mission.record_types)} dataset"
    attempts = 0
    last_error: SourceSelectionError | None = None

    def _catalog() -> list[FoundCandidate]:
        url = _validate_endpoint(catalog, name=CATALOG_URL_ENV)
        payload = _get_json(url, {"q": mission.jurisdiction, "jurisdiction": mission.jurisdiction,
                                  "record_types": ",".join(mission.record_types),
                                  "limit": mission.budget.max_results}, mission, http_get,
                            evidence_sink=evidence_sink, provider="official_catalog")
        ckan = _ckan_rows(payload, mission)
        if ckan is not None:
            return ckan[:mission.budget.max_results]
        rows = payload.get("sources", payload.get("results", [])) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise SourceSelectionError("catalog response has no source list")
        return [_candidate(r, mission, tier=1, provider="official_catalog")
                for r in rows[:mission.budget.max_results] if isinstance(r, dict)]

    def _searxng() -> list[FoundCandidate]:
        url = _validate_endpoint(searxng, name=SEARXNG_URL_ENV)
        payload = _get_json(f"{url}/search", {"q": query, "format": "json"}, mission, http_get,
                            evidence_sink=evidence_sink, provider="searxng")
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            raise SourceSelectionError("SearXNG response has no result list")
        found = []
        for r in rows[:mission.budget.max_results]:
            if isinstance(r, dict):
                # Search results claim nothing: official/license/freshness stay unknown.
                found.append(_candidate({"name": r.get("title"), "url": r.get("url")},
                                        mission, tier=2, provider="searxng"))
        return found

    for provider_fn in ([_catalog] if catalog else []) + ([_searxng] if searxng else []):
        if attempts >= mission.budget.max_requests:
            break
        attempts += 1
        try:
            return provider_fn()
        except UncertainFindingError:
            raise
        except SourceSelectionError as exc:
            last_error = exc
    # P3 Brave only with a paid allowance, reserved before transmission,
    # and only within the remaining approved total limit.
    if attempts < mission.budget.max_requests and mission.budget.max_paid_requests >= 1:
        if reserve is None:
            from app.research_budget_db import reserve_requests as reserve
        reserve(1)  # shared durable reservation; raises when no allowance remains
        if brave_execute is None:
            from app.agents.gabriel.sources.brave import execute_query as brave_execute
        try:
            results = brave_execute(query, "gabriel-source-finder", mission.budget.timeout_seconds)
        except Exception as exc:
            _record_evidence(evidence_sink, provider="brave", url="", status=None,
                             body=b"", error=type(exc).__name__)
            raise UncertainFindingError(
                "Brave transmission outcome is uncertain; not retried") from exc
        rows = list(results)[:mission.budget.max_results]
        _record_evidence(
            evidence_sink, provider="brave", url="", status=200,
            body="\n".join(f"{getattr(r, 'title', '')} {getattr(r, 'url', '')}"
                           for r in rows).encode())
        return [_candidate({"name": getattr(r, "title", ""), "url": getattr(r, "url", "")},
                           mission, tier=3, provider="brave") for r in rows]
    if last_error is not None:
        raise last_error
    raise SourceSelectionError("no safe configured discovery provider exists; refusing without network")
