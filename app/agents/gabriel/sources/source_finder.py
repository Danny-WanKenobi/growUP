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
    # Shared helper (app...http_policy): credential-free public HTTPS,
    # no private/internal literal or DNS-resolved endpoints, DNS
    # failure fails closed. DNS rebinding/TOCTOU is documented there.
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, validate_endpoint

    try:
        return validate_endpoint(raw, name=name).rstrip("/")
    except HttpPolicyError as exc:
        raise SourceSelectionError(str(exc)) from exc


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


def _jurisdiction_from_observed(raw: dict, domain: str, mission: SourceMission) -> str:
    """Jurisdiction from the candidate's own observed metadata.

    Established only when the mission jurisdiction text appears in
    the dataset's observed name, publisher, or portal domain (e.g.
    "Allegheny County Property Assessments", data.seattle.gov). The
    search query alone never establishes jurisdiction.
    """
    stated = str(raw.get("jurisdiction") or "").strip()
    if stated:
        return stated
    wanted = " ".join(str(mission.jurisdiction or "").lower().split())
    if not wanted:
        return ""
    observed = " ".join([
        str(raw.get("name") or raw.get("title") or "").lower(),
        str(raw.get("publisher_name") or raw.get("publisher") or "").lower(),
        domain.lower(),
    ])
    return str(mission.jurisdiction) if wanted in observed else ""


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
        # Observed metadata only: jurisdiction comes from the
        # candidate's own stated metadata or its observed title,
        # publisher, or portal domain -- never from the query alone.
        jurisdiction=_jurisdiction_from_observed(raw, domain, mission)[:200],
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
    resource = dict(raw.get("resource") or {})
    if resource:
        resource.setdefault("resource_url", url)
        resource.setdefault("platform", provider)
    return FoundCandidate(candidate=candidate, tier=tier, provider=provider,
                          adapter=str(adapter)[:100] if adapter else None,
                          evidence_ref=(url or domain)[:500],
                          resource=resource)


# License identifiers whose terms are known-clear without further
# review. Any other observed license id is recorded as observed only
# (license_clear stays False); absence never becomes clearance.
_KNOWN_CLEAR_LICENSES = frozenset({
    "cc0-1.0", "cc0", "cc-zero", "cc-by-4.0", "cc-by", "odc-by-1.0",
    "odc-by", "open-government-licence", "ogl-uk-3.0",
    # Observed portal license labels whose terms are known-clear.
    "public domain", "public-domain", "us public domain",
    "u.s. public domain",
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
        csv_res = next((r for r in resources
                        if str(r.get("format") or "").lower() == "csv" and r.get("url")), None)
        first = csv_res or (resources[0] if resources else {})
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
            "adapter": "generic_ckan" if csv_res else None,
            "resource": {
                "resource_url": url, "platform": "ckan",
                "dataset_id": str(pkg.get("id") or pkg.get("name") or ""),
                "license_id": license_id,
                "field_mapping": {},
                "fields": [str(f) for f in (first.get("fields") or [])] if isinstance(first.get("fields"), list) else [],
            } if url else {},
        }
        found.append(_candidate(raw, mission, tier=1, provider="official_catalog"))
    return found


def _age_from_iso(raw: Any) -> int | None:
    from datetime import datetime, timezone
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - parsed).days
    return age if age >= 0 else 0


def _socrata_rows(payload: Any, mission: SourceMission) -> list[FoundCandidate] | None:
    """Parse a Socrata catalog payload; observed metadata only."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    found: list[FoundCandidate] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        domain = str(meta.get("domain") or "").strip()
        dataset_id = str(resource.get("id") or "")
        if not domain or not dataset_id:
            continue
        resource_url = f"https://{domain}/resource/{dataset_id}.json"
        license_id = str(meta.get("license") or resource.get("license") or "").strip()
        fields = [str(x) for x in (resource.get("columns_field_name") or []) if x]
        raw = {
            "name": resource.get("name") or domain,
            "publisher_name": meta.get("publisher") or domain,
            "publisher_domain": domain,
            "provenance_url": resource_url,
            "claimed_official": domain.endswith(".gov"),
            # Jurisdiction/record types only when the catalog itself
            # states them; never copied from the search query.
            "jurisdiction": str(meta.get("jurisdiction") or resource.get("jurisdiction") or "")[:200],
            "country": str(meta.get("country") or resource.get("country") or "")[:20],
            "record_types": resource.get("record_types") if isinstance(resource.get("record_types"), list)
                            else (["parcels", "appraisal"] if meta.get("record_types") == "parcels,appraisal" else []),
            "access_method": "api",
            "license_clear": license_id.lower() in _KNOWN_CLEAR_LICENSES,
            "formats": ["json", "csv"] if resource.get("type") == "dataset" else [],
            "property_level": resource.get("type") == "dataset",
            "update_age_days": _age_from_iso(resource.get("updatedAt") or resource.get("updated_at")),
            "adapter": "generic_socrata" if resource.get("type") == "dataset" else None,
            "resource": {"resource_url": resource_url, "platform": "socrata",
                         "dataset_id": dataset_id, "license_id": license_id,
                         "field_mapping": {}, "fields": fields},
        }
        found.append(_candidate(raw, mission, tier=1, provider="socrata_catalog"))
    return found


def _arcgis_rows(payload: Any, mission: SourceMission) -> list[FoundCandidate] | None:
    """Parse an ArcGIS Hub search payload; observed metadata only."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    found: list[FoundCandidate] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        url = str(attrs.get("url") or "").strip()
        if not url:
            continue
        domain = urlparse(url).hostname or ""
        license_text = str(attrs.get("licenseInfo") or attrs.get("accessInformation") or "").strip()
        license_id = license_text if license_text.lower() in _KNOWN_CLEAR_LICENSES else ""
        fields = [str(f.get("name")) for f in (attrs.get("fields") or [])
                  if isinstance(f, dict) and f.get("name")]
        modified = attrs.get("modified")
        age = None
        if isinstance(modified, (int, float)) and not isinstance(modified, bool):
            from datetime import datetime, timezone
            age = max(0, (datetime.now(timezone.utc)
                          - datetime.fromtimestamp(modified / 1000, tz=timezone.utc)).days)
        raw = {
            "name": attrs.get("name") or domain,
            "publisher_name": attrs.get("owner") or domain,
            "publisher_domain": domain,
            "provenance_url": url,
            "claimed_official": domain.endswith(".gov"),
            "jurisdiction": str(attrs.get("jurisdiction") or "")[:200],
            "country": str(attrs.get("country") or "")[:20],
            "record_types": attrs.get("record_types") if isinstance(attrs.get("record_types"), list) else [],
            "access_method": "api",
            "license_clear": bool(license_id),
            "formats": ["geojson", "json"],
            "property_level": True,
            "update_age_days": age,
            "adapter": "generic_arcgis",
            "resource": {"resource_url": url, "platform": "arcgis",
                         "dataset_id": str(item.get("id") or ""),
                         "license_id": license_id,
                         "field_mapping": {}, "fields": fields},
        }
        found.append(_candidate(raw, mission, tier=1, provider="arcgis_catalog"))
    return found


def _dedupe_identity(found: list[FoundCandidate]) -> list[FoundCandidate]:
    """Resolve duplicate dataset titles by identity, not name."""
    seen: set[tuple[str, str, str]] = set()
    out: list[FoundCandidate] = []
    for item in found:
        res = item.resource or {}
        key = (item.candidate.publisher_domain.lower(),
               str(res.get("resource_url") or item.evidence_ref or ""),
               str(res.get("dataset_id") or item.candidate.name))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def find_candidates(mission: SourceMission, *, http_get: HttpGet | None = None,
                    brave_execute: Callable[..., Any] | None = None,
                    reserve: Callable[..., Any] | None = None,
                    env: Any = None,
                    evidence_sink: list | None = None,
                    catalog_urls: list | None = None) -> list[FoundCandidate]:
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

    if catalog_urls:
        # P1 tier: query *all* configured national catalogs, but only
        # within the shared finite allowance. Candidates aggregate;
        # P2/P3 run only when this tier yields nothing.
        collected: list[FoundCandidate] = []
        parsers = {"socrata": _socrata_rows, "arcgis": _arcgis_rows, "ckan": _ckan_rows}
        for entry in catalog_urls:
            if attempts >= mission.budget.max_requests:
                break
            provider_name, raw_url, kind = (list(entry) + ["", "", ""])[:3] if not isinstance(entry, dict) \
                else (entry.get("provider", ""), entry.get("url", ""), entry.get("kind", ""))
            if not raw_url:
                continue
            attempts += 1
            try:
                url = _validate_endpoint(str(raw_url), name=str(provider_name or "catalog"))
                # Property subject terms steer catalog full-text
                # search to property datasets instead of generic city
                # data (catalogs match plain subjects, not record-type
                # jargon; ArcGIS/CKAN keep the record-type phrasing).
                catalog_query = (
                    f"{mission.jurisdiction} property buildings"
                    if kind == "socrata"
                    else f"{mission.jurisdiction} parcels property"
                    if kind == "arcgis"
                    else f"{mission.jurisdiction} {' '.join(mission.record_types)}"
                ).strip()
                params = ({"q": catalog_query, "page[size]": mission.budget.max_results}
                          if kind == "arcgis"
                          else {"q": catalog_query, "limit": mission.budget.max_results})
                payload = _get_json(url, params, mission, http_get,
                                    evidence_sink=evidence_sink, provider=str(provider_name))
                parsed = parsers.get(str(kind), lambda p, m: None)(payload, mission)
                if parsed is None:
                    ckan = _ckan_rows(payload, mission)
                    parsed = ckan if ckan is not None else []
                collected.extend(parsed)
            except UncertainFindingError:
                raise
            except SourceSelectionError as exc:
                last_error = exc
        if collected:
            # Interleave providers round-robin before capping: a
            # chatty first catalog must not starve later P1 catalogs
            # out of the shared result budget entirely.
            deduped = _dedupe_identity(collected)
            by_provider: dict[str, list] = {}
            for item in deduped:
                by_provider.setdefault(item.provider, []).append(item)
            queues = list(by_provider.values())
            merged: list = []
            while queues and len(merged) < mission.budget.max_results:
                for queue in queues:
                    if queue and len(merged) < mission.budget.max_results:
                        merged.append(queue.pop(0))
                queues = [q for q in queues if q]
            return merged
        catalog = ""  # P1 exhausted; fall through to P2/P3 below.

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
