"""Autonomous publisher verification and enrollment for Gabriel.

Gabriel may decide enrollment *on his own*, but only from evidence he
retrieved himself, directly from the publisher's own domain, under the
shared network policy -- never from a catalog claim, a domain suffix
alone, or an LLM judgment. The decision is deterministic: every
criterion below must pass, on observed values, or the outcome is
``needs_more_evidence`` (fail closed). A host-spoof (evidence served
by a different host than the resource claims) is ``rejected``.

An autonomous enrollment is a narrow, fact-level authority grant: it
covers only the recorded facts the observed dataset fields support
(parcel/account identity, situs address, coordinates, recorded
use classification, areas, years -- as observed). It never covers
vacancy, occupancy, availability, ownership, zoning permission,
structural suitability, agricultural feasibility, or profitability.

Enrollments are durable: claim before transmission, evidence and
decision persisted as digest-bound workflow records, replay returns
the retained decision with zero network, and an uncertain attempt
stays consumed. The static curated registry
(``PRODUCTION_REGISTRY``) is never mutated; autonomous enrollments
live in the database and are merged at verification time, with the
curated entries always winning a hostname conflict. Every enrollment
carries a review-due date and expires -- stale evidence must be
re-verified, not trusted forever.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

ENROLLMENT_POLICY_VERSION = "gabriel-autonomous-enrollment-1"

# Published Texas appraisal category vocabulary (Comptroller/PTD
# property category codes used by Texas appraisal districts). Only
# real-property categories relevant to the canonical Grow Up
# categories are mapped; residential, agricultural, personal-
# property, and inventory codes stay unmapped on purpose. This is a
# public code standard applied to codes *observed* in a layer's own
# distribution during verification -- never assumed for a source
# whose fields do not carry it, and recorded in the selection
# resource next to the observed distribution.
APPRAISAL_CATEGORY_MAP = {
    "B": "commercial_property",    # multifamily residential real
    "F": "commercial_property",    # commercial/industrial real
    "F1": "commercial_property",   # commercial real property
    "F2": "warehouse_industrial",  # industrial real property
}


def standard_appraisal_category(code: str) -> str | None:
    """Canonical category for one observed Texas appraisal code."""
    normalized = (code or "").strip().upper()
    if normalized in APPRAISAL_CATEGORY_MAP:
        return APPRAISAL_CATEGORY_MAP[normalized]
    return None


def _category_code_field(fields: tuple[str, ...]) -> str | None:
    for name in fields:
        lowered = str(name).lower()
        if lowered in ("propcategorycode", "categorycode", "catcode",
                       "appraisalcategory", "propcatcode"):
            return str(name)
        if "category" in lowered and lowered.endswith("code"):
            return str(name)
    return None
MAX_FRESHNESS_DAYS = 800
REVIEW_DUE_DAYS = 180

_IDENTITY_TOKENS = ("parcel", "account", "apn", "pin", "taxparcel", "folio", "reid")
_LOCATION_TOKENS = ("address", "situs", "latitude", "longitude", "lat", "lon")
_STOPWORDS = {"county", "city", "of", "the", "and", "for", "tx", "texas", "us", "usa"}


class EnrollmentError(ValueError):
    """Autonomous enrollment refused or could not be persisted."""


@dataclass(frozen=True)
class PublisherEvidence:
    """Observed facts retrieved from the publisher's own domain."""

    domain: str
    dataset_id: str
    resource_url: str
    publisher_name: str
    attribution: str
    dataset_name: str
    license_id: str
    license_clear: bool
    freshness_days: int | None
    fields: tuple[str, ...] = ()
    sample_rows: int = 0
    sample_geo_values: tuple[str, ...] = ()
    evidence_digest: str = ""
    retrieved_at: str = ""
    license_basis: str = ""  # dataset_metadata | data_json | portal_terms | ""
    terms_digest: str = ""
    item_bound: bool = False  # ArcGIS item record fetched and bound to this layer
    item_credit: str = ""  # item accessInformation (publisher-stated credit)
    item_tags: tuple[str, ...] = ()
    category_distribution: dict = field(default_factory=dict)


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _jurisdiction_tokens(jurisdiction: str) -> list[str]:
    parts = [p for p in re.split(r"[^a-z]+", (jurisdiction or "").lower()) if p]
    return [p for p in parts if p not in _STOPWORDS and len(p) > 2]


def _field_tokens(fields: tuple[str, ...]) -> tuple[bool, bool]:
    identity = location = False
    for name in fields:
        parts = [p for p in re.split(r"[^a-z]+", str(name).lower()) if p]
        joined = "".join(parts)
        if any(tok in joined for tok in _IDENTITY_TOKENS):
            identity = True
        if any(tok in parts or tok in joined for tok in _LOCATION_TOKENS):
            location = True
    return identity, location


_TERMS_AFFIRMATIVE = (
    "public domain", "free to use", "may be used", "may use, copy",
    "reuse", "re-use", "redistribute", "open government licence",
    "open government license", "creative commons",
    "no restrictions on use", "free for any use",
    "for public use", "available to the public",
)
_TERMS_PROHIBITED = (
    "may not reproduce", "must not reproduce", "shall not reproduce",
    "no part may be reproduced", "prior written permission is required",
    "written permission is required", "permission must be obtained",
    "for personal use only", "non-commercial use only",
    "noncommercial use only",
)


def _terms_license_clear(text: str) -> bool:
    """Deterministic terms evaluation: affirmative grant, no prohibition."""
    lowered = re.sub(r"\s+", " ", (text or "").lower())
    if any(phrase in lowered for phrase in _TERMS_PROHIBITED):
        return False
    return any(phrase in lowered for phrase in _TERMS_AFFIRMATIVE)


def verify_socrata_resource(
    resource: dict[str, Any],
    *,
    jurisdiction: str,
    fetch: Callable[..., bytes] | None = None,
    timeout: float = 15.0,
) -> PublisherEvidence:
    """Retrieve publisher-served metadata + a bounded sample.

    Two core requests, both to the publisher's own domain under
    ``http_policy``: the Socrata view metadata (publisher identity,
    license, freshness, columns) and a 5-row sample (geography is
    checked against observed values when geographic fields exist).
    Only when the dataset metadata itself carries no known-clear
    license, up to three further publisher-domain requests seek the
    license where the portal actually publishes it: the portal's
    machine-readable ``data.json`` catalog entry for this dataset,
    then a same-host terms page linked from the portal root (owner-
    approved portal-terms rule, 2026-11-03). Silence, a JS-only page,
    or prohibitive terms never clear a license. Any transport/policy
    failure raises ``EnrollmentError`` -- the attempt is recorded by
    the caller's durable claim, not retried here.
    """
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes
    from app.agents.gabriel.sources.source_finder import _KNOWN_CLEAR_LICENSES

    domain = str(resource.get("publisher_domain") or "").strip().lower()
    resource_url = str(resource.get("resource_url") or "").strip()
    dataset_id = str(resource.get("dataset_id") or "").strip()
    if not domain or not dataset_id or not resource_url:
        raise EnrollmentError("resource lacks publisher domain, dataset id, or URL")
    if (urlparse(resource_url).hostname or "").lower() != domain:
        raise EnrollmentError("resource URL host does not match claimed publisher domain")

    def _get(url: str, params: dict | None = None) -> bytes:
        if fetch is not None:
            return fetch(url, params or {})
        try:
            return fetch_bytes(url, params=params, timeout=timeout,
                               max_bytes=1_000_000, name="publisher evidence")
        except HttpPolicyError as exc:
            raise EnrollmentError(f"publisher evidence refused: {exc}") from exc

    view_url = f"https://{domain}/api/views/{dataset_id}"
    try:
        view = json.loads(_get(view_url) or b"{}")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher metadata is not valid JSON") from exc
    if not isinstance(view, dict) or str(view.get("id") or "") != dataset_id:
        raise EnrollmentError("publisher metadata does not describe the retained dataset")

    license_raw = view.get("license")
    license_id = ""
    if isinstance(license_raw, dict):
        license_id = str(license_raw.get("name") or license_raw.get("id") or "")
    elif license_raw:
        license_id = str(license_raw)
    columns = view.get("columns") or []
    fields = tuple(
        str(c.get("fieldName") or "") for c in columns
        if isinstance(c, dict) and c.get("fieldName")
    )
    freshness: int | None = None
    updated = view.get("rowsUpdatedAt")
    if isinstance(updated, (int, float)) and updated > 0:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            float(updated), tz=timezone.utc)
        freshness = max(age.days, 0)

    sample_rows = 0
    geo_values: list[str] = []
    try:
        rows = json.loads(_get(resource_url, {"$limit": 5}) or b"[]")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher sample is not valid JSON") from exc
    if isinstance(rows, list):
        sample_rows = len([r for r in rows if isinstance(r, dict)])
        geo_keys = ("city", "county", "state", "municipality")
        for row in rows:
            if not isinstance(row, dict):
                continue
            lowered = {str(k).lower(): v for k, v in row.items()}
            for key in geo_keys:
                value = lowered.get(key)
                if value not in (None, ""):
                    geo_values.append(str(value))

    license_basis = "dataset_metadata" if (
        license_id.strip().lower() in _KNOWN_CLEAR_LICENSES) else ""
    terms_digest = ""
    if not license_basis:
        # Portal-terms avenue 1: the portal's own data.json catalog.
        try:
            catalog = json.loads(_get(f"https://{domain}/data.json") or b"{}")
        except Exception:
            catalog = None
        if isinstance(catalog, dict):
            for entry in catalog.get("dataset") or []:
                if not isinstance(entry, dict):
                    continue
                blob = json.dumps(entry)
                if dataset_id not in blob:
                    continue
                cat_license = str(entry.get("license") or "").strip()
                if cat_license:
                    license_id = cat_license
                    normalized = cat_license.lower()
                    if normalized in _KNOWN_CLEAR_LICENSES or any(
                            tok in normalized for tok in
                            ("cc0", "publicdomain", "public domain", "cc-by", "odc-by")):
                        license_basis = "data_json"
                    terms_digest = hashlib.sha256(blob.encode()).hexdigest()
                break
    if not license_basis:
        # Portal-terms avenue 2: a terms page linked from the portal
        # root, on the publisher's own host only.
        try:
            root = _get(f"https://{domain}/").decode("utf-8", "replace")
            hrefs = re.findall(r'href="([^"]+)"', root)
            terms_path = next(
                (h for h in hrefs
                 if re.search(r"terms", h, re.I)
                 and (urlparse(h if "://" in h else f"https://{domain}{h}").hostname or "") == domain),
                None)
            if terms_path:
                terms_url = terms_path if "://" in terms_path else f"https://{domain}{terms_path}"
                raw = _get(terms_url)
                terms_digest = hashlib.sha256(raw).hexdigest()
                text = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
                if _terms_license_clear(text):
                    license_basis = "portal_terms"
                    license_id = license_id or "portal terms of use"
        except Exception:
            pass

    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "domain": domain, "dataset_id": dataset_id, "resource_url": resource_url,
        "view_url": view_url, "retrieved_at": retrieved,
        "view_name": str(view.get("name") or ""),
        "attribution": str(view.get("attribution") or ""),
        "license_id": license_id, "license_basis": license_basis,
        "terms_digest": terms_digest, "freshness_days": freshness,
        "fields": list(fields), "sample_rows": sample_rows,
        "sample_geo_values": geo_values[:10],
    }
    return PublisherEvidence(
        domain=domain,
        dataset_id=dataset_id,
        resource_url=resource_url,
        publisher_name=str(resource.get("publisher_name") or ""),
        attribution=str(view.get("attribution") or ""),
        dataset_name=str(view.get("name") or ""),
        license_id=license_id,
        license_clear=bool(license_basis),
        freshness_days=freshness,
        fields=fields,
        sample_rows=sample_rows,
        sample_geo_values=tuple(geo_values[:10]),
        evidence_digest=hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        retrieved_at=retrieved,
        license_basis=license_basis,
        terms_digest=terms_digest,
    )


def verify_arcgis_resource(
    resource: dict[str, Any],
    *,
    jurisdiction: str,
    fetch: Callable[..., bytes] | None = None,
    timeout: float = 15.0,
) -> PublisherEvidence:
    """Publisher-served evidence for one retained ArcGIS layer.

    Two core requests under ``http_policy``: the layer metadata
    (``?f=pjson``: name, fields, last-edit freshness, copyright) and
    a bounded 5-feature sample query. When the resource carries an
    ArcGIS item id, one further request fetches the item record
    (owner, title, license/access information, public access) from
    the sharing API -- the publisher-maintained record for exactly
    this layer. License clears only on a known-clear identifier or
    an affirmative publisher-served terms statement; silence never
    clears. Geography checks observed feature properties only.
    """
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes
    from app.agents.gabriel.sources.source_finder import _KNOWN_CLEAR_LICENSES

    layer_url = str(resource.get("resource_url") or "").strip().rstrip("/")
    dataset_id = str(resource.get("dataset_id") or "").strip()
    if not layer_url:
        raise EnrollmentError("arcgis resource lacks a layer URL")
    domain = (urlparse(layer_url).hostname or "").lower()
    if not domain:
        raise EnrollmentError("arcgis layer URL has no host")

    def _get(url: str, params: dict | None = None) -> bytes:
        if fetch is not None:
            return fetch(url, params or {})
        try:
            return fetch_bytes(url, params=params, timeout=timeout,
                               max_bytes=1_000_000, name="publisher evidence")
        except HttpPolicyError as exc:
            raise EnrollmentError(f"publisher evidence refused: {exc}") from exc

    try:
        layer = json.loads(_get(layer_url, {"f": "pjson"}) or b"{}")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher layer metadata is not valid JSON") from exc
    if not isinstance(layer, dict) or not (layer.get("fields") or layer.get("name")):
        raise EnrollmentError("publisher layer metadata does not describe a layer")
    fields = tuple(
        str(f.get("name") or "") for f in (layer.get("fields") or [])
        if isinstance(f, dict) and f.get("name"))
    freshness: int | None = None
    edited = (layer.get("editingInfo") or {}).get("lastEditDate") or layer.get("lastEditDate")
    if isinstance(edited, (int, float)) and edited > 0:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            float(edited) / 1000.0, tz=timezone.utc)
        freshness = max(age.days, 0)
    license_text = str(layer.get("licenseInfo") or layer.get("copyrightText") or "").strip()
    license_basis = ""
    publisher_name = str(resource.get("publisher_name") or "")
    attribution = publisher_name
    dataset_name = str(layer.get("name") or "")
    item_digest = ""
    item_bound = False
    item_credit = ""
    item_tags: tuple[str, ...] = ()
    if dataset_id and re.fullmatch(r"[0-9a-fA-F]{32}", dataset_id):
        try:
            item = json.loads(_get(
                f"https://www.arcgis.com/sharing/rest/content/items/{dataset_id}",
                {"f": "pjson"}) or b"{}")
        except Exception:
            item = None
        if isinstance(item, dict) and item.get("id"):
            item_digest = hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
            item_url = str(item.get("url") or "").rstrip("/")
            layer_service = layer_url.rsplit("/", 1)[0] if layer_url.rsplit("/", 1)[-1].isdigit() else layer_url
            item_bound = bool(item_url) and (
                item_url == layer_url or item_url == layer_service)
            item_credit = re.sub(r"<[^>]+>", " ", str(item.get("accessInformation") or "")).strip()
            item_tags = tuple(str(t) for t in (item.get("tags") or []) if t)
            publisher_name = str(item.get("owner") or publisher_name)
            attribution = str(item.get("title") or dataset_name or attribution)
            dataset_name = str(item.get("title") or dataset_name)
            item_license = str(item.get("licenseInfo") or item.get("accessInformation") or "").strip()
            if item_license:
                license_text = item_license
    if license_text:
        if license_text.lower() in _KNOWN_CLEAR_LICENSES:
            license_basis = "dataset_metadata"
        elif _terms_license_clear(license_text):
            license_basis = "portal_terms"

    sample_rows = 0
    geo_values: list[str] = []
    query_url = layer_url + "/query"
    observed_states: set[str] = set()
    try:
        payload = json.loads(_get(query_url, {
            "where": "1=1", "outFields": "*", "resultRecordCount": 5,
            "f": "geojson", "outSR": "4326"}) or b"{}")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher sample is not valid JSON") from exc
    features = payload.get("features") if isinstance(payload, dict) else None
    if isinstance(features, list):
        sample_rows = len([f for f in features if isinstance(f, dict)])
        for feature in features:
            props = (feature or {}).get("properties") or {}
            if not isinstance(props, dict):
                continue
            lowered = {str(k).lower(): v for k, v in props.items()}
            for key in ("state", "st", "situsstate", "situs_state"):
                value = str(lowered.get(key) or "").strip().upper()
                if value:
                    observed_states.add(value)
            for key in ("city", "county", "municipality", "situscity", "city_name"):
                value = lowered.get(key)
                if value not in (None, ""):
                    geo_values.append(str(value))

    # Observed category-code distribution (one bounded statistics
    # query, only when the layer carries a category-code field):
    # this is what lets a later selection record an appraisal-code
    # vocabulary mapping from the layer's own codes, not an assumption.
    category_distribution: dict = {}
    code_field = _category_code_field(fields)
    if code_field:
        try:
            stats = json.loads(_get(query_url, {
                "where": "1=1", "groupByFieldsForStatistics": code_field,
                "outStatistics": json.dumps([{
                    "statisticType": "count", "onStatisticField": code_field,
                    "outStatisticFieldName": "n"}]),
                "f": "json"}) or b"{}")
            values: dict[str, int] = {}
            for feature in (stats.get("features") or []):
                attrs = (feature or {}).get("attributes") or {}
                code = attrs.get(code_field)
                if code not in (None, ""):
                    values[str(code)] = int(attrs.get("n") or 0)
            if values:
                category_distribution = {"field": code_field, "values": values}
                metadata_text = " ".join(str(layer.get(k) or "") for k in
                                         ("name", "description", "copyrightText"))
                if item_bound:
                    metadata_text += " " + " ".join((dataset_name, attribution,
                                                       item_credit, " ".join(item_tags)))
                if observed_states == {"TX"} or observed_states == {"TEXAS"}:
                    category_distribution["state"] = "TX"
                elif not observed_states and re.search(r"\bTexas\b", metadata_text, re.I):
                    category_distribution["state"] = "TX"
        except Exception:
            category_distribution = {}

    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "domain": domain, "dataset_id": dataset_id, "resource_url": layer_url,
        "layer_url": layer_url, "retrieved_at": retrieved,
        "dataset_name": dataset_name, "attribution": attribution,
        "license_id": license_text[:200], "license_basis": license_basis,
        "terms_digest": item_digest, "item_bound": item_bound,
        "item_credit": item_credit, "item_tags": list(item_tags),
        "category_distribution": category_distribution,
        "freshness_days": freshness,
        "fields": list(fields), "sample_rows": sample_rows,
        "sample_geo_values": geo_values[:10],
    }
    return PublisherEvidence(
        domain=domain, dataset_id=dataset_id, resource_url=layer_url,
        publisher_name=publisher_name, attribution=attribution,
        dataset_name=dataset_name,
        license_id=license_text[:200], license_clear=bool(license_basis),
        freshness_days=freshness, fields=fields, sample_rows=sample_rows,
        sample_geo_values=tuple(geo_values[:10]),
        evidence_digest=hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        retrieved_at=retrieved,
        license_basis=license_basis, terms_digest=item_digest,
        item_bound=item_bound, item_credit=item_credit, item_tags=item_tags,
        category_distribution=category_distribution,
    )


def verify_directory_corroboration(
    domain: str,
    *,
    publisher_name: str,
    jurisdiction: str,
    directory_urls: tuple[str, ...] | list[str],
    fetch: Callable[..., bytes] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Corroborate a non-``.gov`` publisher domain against directories.

    A directory is authoritative only if it is itself served from a
    ``.gov`` host (owner-approved rule, 2026-11-03). Corroboration
    requires a directory page that links to the candidate domain with
    anchor text naming the publisher or jurisdiction. Directory URLs
    are configuration (``GABRIEL_DIRECTORY_URLS``), never a hard-coded
    county list. Returns digest-bound evidence; absence of a match is
    an empty ``matched`` flag, never an error or an inference.
    """
    from app.agents.gabriel.sources.http_policy import (
        HttpPolicyError, fetch_bytes, validate_endpoint,
    )
    target = (domain or "").strip().lower()

    def _same_site(host_a: str, host_b: str) -> bool:
        if not host_a or not host_b:
            return False
        if host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a):
            return True
        # Same registrable domain for common US suffixes lets an
        # official www.<county>.com listing corroborate a service
        # subdomain such as mapit.<county>.com.
        def _reg(h: str) -> str:
            parts = h.split(".")
            return ".".join(parts[-2:]) if len(parts) >= 2 else h
        return _reg(host_a) == _reg(host_b) and host_a.rsplit(".", 1)[-1] in (
            "com", "org", "net", "us", "edu")

    tokens = set(_jurisdiction_tokens(jurisdiction))
    tokens.update(
        t for t in re.split(r"[^a-z]+", (publisher_name or "").lower())
        if len(t) > 2 and t not in _STOPWORDS)
    for url in directory_urls or []:
        raw_url = str(url or "").strip()
        if not raw_url:
            continue
        try:
            validate_endpoint(raw_url, name="publisher directory")
        except HttpPolicyError:
            continue
        host = (urlparse(raw_url).hostname or "").lower()
        if not host.endswith(".gov"):
            continue
        try:
            if fetch is not None:
                body = fetch(raw_url, {})
            else:
                body = fetch_bytes(raw_url, timeout=timeout,
                                   max_bytes=1_000_000,
                                   name="publisher directory")
        except Exception:
            continue
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        anchors = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,120})<', text)
        for href, label in anchors:
            link_host = (urlparse(href if "://" in href else f"https://{href}").hostname or "").lower()
            if not _same_site(link_host, target):
                continue
            label_tokens = {t for t in re.split(r"[^a-z]+", label.lower()) if t}
            if tokens & label_tokens:
                return {
                    "matched": True, "directory_url": raw_url,
                    "directory_host": host, "matched_domain": target,
                    "matched_text": label.strip()[:120],
                    "digest": hashlib.sha256(bytes(body)).hexdigest(),
                }
        # Two-level directories: an index page links to a per-
        # jurisdiction page (same .gov host, anchor naming the
        # jurisdiction), and that page links the publisher's site.
        # The jurisdiction-named path from an official directory is
        # the corroboration; bounded at two subpages per directory.
        subpages = 0
        for href, label in anchors:
            if subpages >= 2:
                break
            label_tokens = {t for t in re.split(r"[^a-z]+", label.lower()) if t}
            if not (tokens & label_tokens):
                continue
            parsed = urlparse(href if "://" in href else raw_url.rsplit("/", 1)[0] + "/" + href)
            if (parsed.hostname or "").lower() != host:
                continue
            subpages += 1
            try:
                if fetch is not None:
                    sub = fetch(parsed.geturl(), {})
                else:
                    sub = fetch_bytes(parsed.geturl(), timeout=timeout,
                                      max_bytes=1_000_000,
                                      name="publisher directory")
            except Exception:
                continue
            sub_text = sub.decode("utf-8", "replace") if isinstance(sub, (bytes, bytearray)) else str(sub)
            for sub_href, sub_label in re.findall(r'href="([^"]+)"[^>]*>([^<]{0,120})<', sub_text):
                sub_host = (urlparse(sub_href if "://" in sub_href else f"https://{sub_href}").hostname or "").lower()
                if _same_site(sub_host, target):
                    return {
                        "matched": True, "directory_url": parsed.geturl(),
                        "directory_host": host, "matched_domain": target,
                        "matched_text": sub_label.strip()[:120],
                        "digest": hashlib.sha256(bytes(sub)).hexdigest(),
                    }
    return {"matched": False, "directory_url": "", "directory_host": "",
            "matched_domain": target, "matched_text": "", "digest": ""}


def _acronyms_of(*texts: str) -> set[str]:
    out: set[str] = set()
    for text in texts:
        for token in re.split(r"[^A-Za-z]+", text or ""):
            if len(token) >= 3 and token.isupper():
                out.add(token)
    return out


def verify_directory_identity(
    acronyms: set[str],
    *,
    jurisdiction: str,
    directory_urls: tuple[str, ...] | list[str],
    fetch: Callable[..., bytes] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Bind an acronym-named publisher to a directory-named district.

    Chain (every hop fetched under ``http_policy``, digest-bound):
    a ``.gov`` directory's per-jurisdiction page names its appraisal
    district's website in the district section; that publisher site
    itself states its full district name (containing the
    jurisdiction token) and uses the acronym. Identity is matched
    only when the acronym equals the initials of the site's own
    stated district name *and* the acronym appears on that site.
    An acronym alone, or a directory listing alone, proves nothing.
    """
    from app.agents.gabriel.sources.http_policy import (
        HttpPolicyError, fetch_bytes, validate_endpoint,
    )
    empty = {"identity_matched": False, "identity_name": "",
             "identity_acronym": "", "identity_domain": "",
             "identity_digest": ""}
    if not acronyms:
        return empty
    tokens = set(_jurisdiction_tokens(jurisdiction))

    def _get(url: str) -> bytes | None:
        try:
            if fetch is not None:
                return fetch(url, {})
            return fetch_bytes(url, timeout=timeout, max_bytes=1_000_000,
                               name="publisher identity")
        except Exception:
            return None

    for raw_url in directory_urls or []:
        raw_url = str(raw_url or "").strip()
        if not raw_url:
            continue
        try:
            validate_endpoint(raw_url, name="publisher directory")
        except HttpPolicyError:
            continue
        host = (urlparse(raw_url).hostname or "").lower()
        if not host.endswith(".gov"):
            continue
        body = _get(raw_url)
        if body is None:
            continue
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
        anchors = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,120})<', text)
        sub_url = ""
        for href, label in anchors:
            label_tokens = {t for t in re.split(r"[^a-z]+", label.lower()) if t}
            if not (tokens & label_tokens):
                continue
            parsed = urlparse(href if "://" in href else raw_url.rsplit("/", 1)[0] + "/" + href)
            if (parsed.hostname or "").lower() == host:
                sub_url = parsed.geturl()
                break
        if not sub_url:
            continue
        sub = _get(sub_url)
        if sub is None:
            continue
        sub_text = sub.decode("utf-8", "replace") if isinstance(sub, (bytes, bytearray)) else str(sub)
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", sub_text))
        segment_match = re.search(r"Appraisal District(.{0,600})", plain)
        segment = segment_match.group(1) if segment_match else plain[:1200]
        domain_match = re.search(r"(www\.[a-z0-9.-]+\.(?:org|com|net))", segment)
        if not domain_match:
            continue
        publisher_domain = domain_match.group(1).lower()
        site = _get(f"https://{publisher_domain}/")
        if site is None:
            continue
        site_text = site.decode("utf-8", "replace") if isinstance(site, (bytes, bytearray)) else str(site)
        site_plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", site_text))
        for name_match in re.finditer(
                r"([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){1,5} Appraisal District)", site_plain):
            full_name = name_match.group(1)
            if not any(tok in full_name.lower() for tok in tokens):
                continue
            initials = "".join(word[0] for word in full_name.split()).upper()
            for acronym in acronyms:
                if acronym == initials and re.search(
                        rf"\b{re.escape(acronym)}\b", site_plain):
                    return {"identity_matched": True,
                            "identity_name": full_name,
                            "identity_acronym": acronym,
                            "identity_domain": publisher_domain,
                            "identity_digest": hashlib.sha256(bytes(site)).hexdigest()}
    return empty


def decide_enrollment(evidence: PublisherEvidence, *, jurisdiction: str,
                      evidence_directory: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic enrollment decision over observed evidence only.

    Every criterion must pass for ``enrolled``. A host mismatch is
    impossible here (verification refuses it earlier); all other
    shortfalls are ``needs_more_evidence`` with the exact missing
    criterion named -- Gabriel records what he still lacks instead of
    guessing.
    """
    tokens = _jurisdiction_tokens(jurisdiction)
    identity_fields, location_fields = _field_tokens(evidence.fields)
    haystack = " ".join([
        evidence.domain, evidence.attribution, evidence.dataset_name,
        evidence.publisher_name,
    ]).lower()
    domain_jurisdiction = any(tok in evidence.domain for tok in tokens)
    named_jurisdiction = any(tok in haystack for tok in tokens)
    geo_match = True
    if evidence.sample_geo_values:
        geo_match = any(
            tok in value.lower()
            for value in evidence.sample_geo_values for tok in tokens
        )
    corroborated = bool(
        (evidence_directory or {}).get("matched")
        and (evidence_directory or {}).get("matched_domain") == evidence.domain)
    identity_bound = bool((evidence_directory or {}).get("identity_matched"))
    # Publisher-served item identity: the ArcGIS item record itself
    # credits a full publisher name containing the jurisdiction, and
    # the acronym formed by that name's initials is the acronym the
    # item publisher uses (owner/title/tags). The record that binds
    # the layer also binds the identity -- no website hop required.
    item_identity = False
    if evidence.item_bound and evidence.item_credit:
        credit = evidence.item_credit
        credit_initials = "".join(
            word[0] for word in re.split(r"[^A-Za-z]+", credit) if word).upper()
        acronyms = _acronyms_of(evidence.publisher_name, evidence.dataset_name,
                                " ".join(evidence.item_tags))
        item_identity = bool(
            any(tok in credit.lower() for tok in tokens)
            and credit_initials in acronyms)
    identity_bound = identity_bound or item_identity
    shared_host = evidence.domain == "arcgis.com" or evidence.domain.endswith(".arcgis.com")
    item_scoped = bool(evidence.item_bound and shared_host and identity_bound)
    criteria = {
        "government_domain": evidence.domain.endswith(".gov"),
        "directory_corroborated_official_domain": corroborated,
        "item_scoped_authority": item_scoped,
        "publisher_identity_matches_jurisdiction": bool(
            domain_jurisdiction or named_jurisdiction or identity_bound),
        "license_known_clear": bool(evidence.license_clear),
        "freshness_known_and_recent": bool(
            evidence.freshness_days is not None
            and evidence.freshness_days <= MAX_FRESHNESS_DAYS),
        "property_fields_observed": bool(identity_fields or location_fields)
        and bool(location_fields),
        "sample_rows_observed": evidence.sample_rows > 0,
        "sample_geography_consistent": geo_match,
    }
    missing = [name for name, ok in criteria.items() if not ok
               and name not in ("government_domain",
                                "directory_corroborated_official_domain",
                                "item_scoped_authority")]
    if not (criteria["government_domain"]
            or criteria["directory_corroborated_official_domain"]
            or criteria["item_scoped_authority"]):
        missing.append("publisher_authority_basis")
    now = datetime.now(timezone.utc)
    decision: dict[str, Any] = {
        "policy_version": ENROLLMENT_POLICY_VERSION,
        "status": "enrolled" if not missing else "needs_more_evidence",
        "domain": evidence.domain,
        "dataset_id": evidence.dataset_id,
        "resource_url": evidence.resource_url,
        "jurisdiction": jurisdiction,
        "publisher_name": evidence.publisher_name or evidence.attribution,
        "criteria": criteria,
        "missing": missing,
        "license_basis": evidence.license_basis,
        "authority_scope": "item" if item_scoped else "domain",
        "item_identity": item_identity,
        "directory_evidence": dict(evidence_directory or {}),
        "evidence_digest": evidence.evidence_digest,
        "retrieved_at": evidence.retrieved_at,
        "decided_at": now.isoformat(timespec="seconds"),
        "review_due": (now + timedelta(days=REVIEW_DUE_DAYS)).date().isoformat()
        if not missing else "",
        "scope_note": (
            "Autonomous enrollment (owner-authorized 2026-11-03): narrow "
            "authority for recorded facts supported by the observed "
            "fields only. Never vacancy, occupancy, availability, "
            "ownership, zoning, structural, agricultural, or financial "
            "authority."),
    }
    decision["digest"] = _digest(decision)
    return decision


class EnrollmentStore:
    """Durable enrollment store on the existing workflow tables.

    Mirrors the source-selection claim pattern: an exclusive claim
    (with a conservative two-request reservation) commits before any
    publisher transmission; evidence and decision persist as
    digest-bound ``research_artifact`` records in that run. No DDL.
    """

    _KIND = "research_artifact"

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        from app.properties_db import get_connection
        return get_connection()

    @staticmethod
    def _idem(domain: str, dataset_id: str) -> str:
        return f"gabriel-enrollment:{domain}:{dataset_id}"[:512]

    def claim(self, domain: str, dataset_id: str, fingerprint: str) -> dict[str, Any]:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import (
            create_run_in_transaction, get_record_in_transaction,
            insert_record_if_absent_in_transaction, add_usage_in_transaction,
        )
        conn = self._open()
        try:
            with conn:
                run = create_run_in_transaction(
                    conn, idempotency_key=self._idem(domain, dataset_id),
                    limits={"mission_fingerprint": fingerprint,
                            "domain": domain, "dataset_id": dataset_id})
                if (run.limits or {}).get("mission_fingerprint") != fingerprint:
                    raise EnrollmentError(
                        "enrollment key was already used for a different resource; refusing")
                existing = get_record_in_transaction(
                    conn, run.run_id, KIND_RESEARCH_ARTIFACT, "decision", 1)
                if existing is not None:
                    conn.commit()
                    return {"status": "completed", "run_id": str(run.run_id),
                            "decision": dict(existing.payload or {})}
                claim_rec = get_record_in_transaction(
                    conn, run.run_id, KIND_RESEARCH_ARTIFACT, "claim", 1)
                if claim_rec is not None:
                    conn.commit()
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                payload = {"state": "claimed", "domain": domain,
                           "dataset_id": dataset_id,
                           "mission_fingerprint": fingerprint}
                inserted = insert_record_if_absent_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="claim", version=1, payload=payload,
                    digest=digest_payload(payload))
                if inserted is not None:
                    add_usage_in_transaction(
                        conn, run.run_id, "source_requests_reserved", 6)
                conn.commit()
                if inserted is None:
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                return {"status": "winner", "run_id": str(run.run_id)}
        except EnrollmentError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise EnrollmentError(
                "durable enrollment claim could not be committed; refusing without network") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def complete(self, domain: str, dataset_id: str, *, decision: dict[str, Any],
                 evidence_payload: dict[str, Any]) -> None:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import get_run_by_key_in_transaction, put_record_in_transaction
        conn = self._open()
        try:
            with conn:
                run = get_run_by_key_in_transaction(conn, self._idem(domain, dataset_id))
                if run is None:
                    raise EnrollmentError("enrollment run is missing; cannot persist decision")
                if decision.get("digest") != _digest(decision):
                    raise EnrollmentError("enrollment decision digest does not verify")
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="evidence", version=1, payload=evidence_payload,
                    digest=digest_payload(evidence_payload))
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="decision", version=1, payload=decision,
                    digest=str(decision.get("digest") or ""))
                conn.commit()
        except EnrollmentError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise EnrollmentError("enrollment decision could not be persisted") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def load_enrollments(self) -> list[dict[str, Any]]:
        """All retained enrollment decisions (digest-verified)."""
        conn = self._open()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT r.payload, r.digest FROM workflow_records r "
                        "JOIN workflow_runs w ON w.run_id = r.run_id "
                        "WHERE w.idempotency_key LIKE 'gabriel-enrollment:%' "
                        "AND r.kind = 'research_artifact' AND r.record_key = 'decision'")
                    rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                payload = row["payload"] if isinstance(row, dict) else row[0]
                digest = row["digest"] if isinstance(row, dict) else row[1]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    continue
                if payload.get("digest") != digest or _digest(payload) != digest:
                    continue
                out.append(payload)
            return out
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _directory_evidence_for(evidence: PublisherEvidence, *, jurisdiction: str,
                            directory_urls, fetch) -> dict[str, Any]:
    if evidence.domain.endswith(".gov") or not directory_urls:
        return {}
    evidence_out = verify_directory_corroboration(
        evidence.domain,
        publisher_name=evidence.publisher_name or evidence.attribution,
        jurisdiction=jurisdiction, directory_urls=tuple(directory_urls),
        fetch=fetch)
    acronyms = _acronyms_of(evidence.publisher_name, evidence.attribution,
                            evidence.dataset_name)
    if acronyms:
        evidence_out.update(verify_directory_identity(
            acronyms, jurisdiction=jurisdiction,
            directory_urls=tuple(directory_urls), fetch=fetch))
    return evidence_out


def run_autonomous_enrollment(
    resource: dict[str, Any],
    *,
    jurisdiction: str,
    store: EnrollmentStore | None = None,
    fetch: Callable[..., bytes] | None = None,
    directory_urls: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Verify one retained generic resource and decide enrollment.

    Socrata resources only in this version (publisher-served view
    metadata is the evidence); other platforms return
    ``needs_more_evidence`` without network. Replay of a decided
    resource returns the retained decision with zero requests.
    """
    platform = str(resource.get("platform") or "").strip().lower()
    domain = str(resource.get("publisher_domain") or "").strip().lower()
    dataset_id = str(resource.get("dataset_id") or "").strip()
    if platform == "arcgis" and not domain and resource.get("resource_url"):
        domain = (urlparse(str(resource["resource_url"])).hostname or "").lower()
    if platform not in ("socrata", "arcgis") or not domain:
        return {"policy_version": ENROLLMENT_POLICY_VERSION,
                "status": "needs_more_evidence",
                "missing": ["publisher-served evidence path not implemented "
                            "for this platform"],
                "domain": domain, "dataset_id": dataset_id}
    if platform == "socrata" and not dataset_id:
        return {"policy_version": ENROLLMENT_POLICY_VERSION,
                "status": "needs_more_evidence",
                "missing": ["publisher-served evidence path not implemented "
                            "for this platform"],
                "domain": domain, "dataset_id": dataset_id}
    resource_key = dataset_id or hashlib.sha256(
        str(resource.get("resource_url") or "").encode()).hexdigest()[:16]
    fingerprint = _digest({
        "domain": domain, "dataset_id": dataset_id,
        "resource_url": str(resource.get("resource_url") or ""),
        "jurisdiction": jurisdiction,
        "policy_version": ENROLLMENT_POLICY_VERSION,
    })
    store = store if store is not None else EnrollmentStore()
    claim = store.claim(domain, resource_key, fingerprint)
    if claim.get("status") == "completed":
        return claim["decision"]
    if claim.get("status") != "winner":
        raise EnrollmentError(
            "enrollment for this resource was already claimed and its "
            "outcome is uncertain; it will not be auto-retransmitted")
    if platform == "arcgis":
        evidence = verify_arcgis_resource(resource, jurisdiction=jurisdiction, fetch=fetch)
    else:
        evidence = verify_socrata_resource(resource, jurisdiction=jurisdiction, fetch=fetch)
    decision = decide_enrollment(
        evidence, jurisdiction=jurisdiction,
        evidence_directory=_directory_evidence_for(
            evidence, jurisdiction=jurisdiction,
            directory_urls=directory_urls, fetch=fetch))
    decision["run_id"] = claim.get("run_id", "")
    decision["digest"] = _digest(decision)
    store.complete(domain, resource_key, decision=decision, evidence_payload={
        "domain": evidence.domain, "dataset_id": evidence.dataset_id,
        "resource_url": evidence.resource_url,
        "publisher_name": evidence.publisher_name,
        "attribution": evidence.attribution,
        "dataset_name": evidence.dataset_name,
        "license_id": evidence.license_id,
        "license_clear": evidence.license_clear,
        "license_basis": evidence.license_basis,
        "terms_digest": evidence.terms_digest,
        "directory_evidence": decision.get("directory_evidence"),
        "freshness_days": evidence.freshness_days,
        "fields": list(evidence.fields),
        "sample_rows": evidence.sample_rows,
        "sample_geo_values": list(evidence.sample_geo_values),
        "evidence_digest": evidence.evidence_digest,
        "retrieved_at": evidence.retrieved_at,
        "fingerprint": fingerprint,
    })
    return decision


def verify_and_select(
    ranked_entry: dict[str, Any],
    *,
    jurisdiction: str,
    record_types: tuple[str, ...] = ("parcels", "appraisal"),
    selection_store: Any = None,
    assignment_key: str,
    fetch: Callable[..., bytes] | None = None,
    directory_urls: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Publisher-direct requalification of one catalog candidate.

    The catalog could only claim; the publisher's own domain now
    answers. If (and only if) the enrollment decision over that
    direct evidence is ``enrolled``, a new candidate carrying only
    observed values is run through the shared ``select_sources``
    policy and persisted as a durable selection decision under its
    own assignment key -- so collection enforcement, replay, and
    digest binding work exactly as for a catalog-selected resource.
    Otherwise the enrollment decision is returned and nothing is
    selected.
    """
    from app.agents.gabriel.sources.source_selection import (
        DiscoveryBudget, FoundCandidate, SourceMission, SourceSelectionError,
        mission_fingerprint, mission_payload, select_sources, _digest,
    )
    from app.agents.gabriel.sources.qualification import SourceCandidate

    resource_url = str(ranked_entry.get("resource_url") or "").strip()
    domain = str(ranked_entry.get("publisher_domain") or "").strip().lower()
    platform = str(ranked_entry.get("platform") or "socrata").strip().lower()
    is_arcgis = platform == "arcgis" or ranked_entry.get("adapter") == "generic_arcgis"
    if is_arcgis:
        platform = "arcgis"
    dataset_id = str(ranked_entry.get("dataset_id") or "")
    if not dataset_id and "/resource/" in resource_url:
        dataset_id = resource_url.rsplit("/resource/", 1)[-1].split(".")[0]
    resource = {
        "platform": platform, "publisher_domain": domain,
        "publisher_name": str(ranked_entry.get("publisher_name") or ""),
        "dataset_id": dataset_id, "resource_url": resource_url,
    }
    if is_arcgis:
        evidence = verify_arcgis_resource(resource, jurisdiction=jurisdiction, fetch=fetch)
    else:
        evidence = verify_socrata_resource(resource, jurisdiction=jurisdiction, fetch=fetch)
    enrollment = decide_enrollment(
        evidence, jurisdiction=jurisdiction,
        evidence_directory=_directory_evidence_for(
            evidence, jurisdiction=jurisdiction,
            directory_urls=directory_urls, fetch=fetch))
    out: dict[str, Any] = {"enrollment": enrollment, "selection": None}
    if enrollment["status"] != "enrolled":
        return out
    # Persist the enrollment decision itself (best effort): the
    # verification path reads authority from the enrollment store.
    # Failure here never fabricates authority -- Raphael simply keeps
    # returning needs_evidence.
    try:
        estore = EnrollmentStore()
        eclaim = estore.claim(domain, dataset_id, _digest({
            "domain": domain, "dataset_id": dataset_id,
            "resource_url": resource_url, "jurisdiction": jurisdiction,
            "policy_version": ENROLLMENT_POLICY_VERSION}))
        if eclaim.get("status") == "winner":
            enrollment["run_id"] = eclaim.get("run_id", "")
            enrollment["digest"] = _digest(enrollment)
            estore.complete(domain, dataset_id, decision=enrollment,
                            evidence_payload={
                                "domain": evidence.domain,
                                "dataset_id": evidence.dataset_id,
                                "resource_url": evidence.resource_url,
                                "publisher_name": evidence.publisher_name,
                                "attribution": evidence.attribution,
                                "dataset_name": evidence.dataset_name,
                                "license_id": evidence.license_id,
                                "license_clear": evidence.license_clear,
                                "freshness_days": evidence.freshness_days,
                                "fields": list(evidence.fields),
                                "category_distribution": dict(evidence.category_distribution),
                                "sample_rows": evidence.sample_rows,
                                "sample_geo_values": list(evidence.sample_geo_values),
                                "evidence_digest": evidence.evidence_digest,
                                "retrieved_at": evidence.retrieved_at})
            clear_registry_cache()
    except Exception:
        pass

    identity_fields, _loc = _field_tokens(evidence.fields)
    observed_record_types = ("parcels",) if identity_fields else ()
    candidate = SourceCandidate(
        name=evidence.dataset_name or str(ranked_entry.get("name") or domain),
        publisher_name=evidence.attribution or evidence.publisher_name or domain,
        # Official-by-domain only for .gov; a directory-corroborated
        # non-.gov publisher's identity rests on the enrollment
        # evidence, not on a domain claim qualify_source would flag.
        publisher_domain=domain, claimed_official=domain.endswith(".gov"),
        jurisdiction=jurisdiction, country="US",
        record_types=observed_record_types or tuple(record_types),
        access_method="api", free=True, requires_auth=False,
        robots_restricted=False, license_clear=True,
        formats=("geojson", "json") if is_arcgis else ("json", "csv"),
        property_level=True,
        update_age_days=evidence.freshness_days,
        provenance_url=resource_url,
    )
    adapter_name = "generic_arcgis" if is_arcgis else "generic_socrata"
    resource_payload: dict[str, Any] = {
        "resource_url": resource_url, "platform": platform,
        "dataset_id": dataset_id, "license_id": evidence.license_id,
        "field_mapping": {}, "fields": list(evidence.fields)}
    # Resource-level appraisal vocabulary: only codes actually
    # observed in this layer's distribution, mapped by the published
    # standard. Unobserved or unmapped codes are never invented.
    distribution = evidence.category_distribution or {}
    if (distribution.get("state") == "TX" and distribution.get("field")
            and distribution.get("values")):
        code_map = {}
        for code in distribution["values"]:
            canonical = standard_appraisal_category(str(code))
            if canonical:
                code_map[str(code).strip().upper()] = canonical
        if code_map:
            resource_payload["category_codes"] = {
                "field": distribution["field"], "map": code_map,
                "observed": distribution["values"],
                "state": "TX", "evidence_digest": evidence.evidence_digest,
                "standard": "Texas appraisal category codes (Comptroller/PTD)"}
    found = FoundCandidate(
        candidate=candidate, tier=1, provider="publisher_direct",
        adapter=adapter_name, evidence_ref=evidence.evidence_digest,
        resource=resource_payload,
    )
    mission = SourceMission(jurisdiction=jurisdiction,
                            record_types=tuple(record_types), country="US",
                            approved=True, budget=DiscoveryBudget(max_requests=0))
    decision = select_sources(mission, [found],
                              supported_adapters={adapter_name})
    if decision.get("status") != "selected":
        raise EnrollmentError(
            "publisher-verified candidate still not selectable: "
            + "; ".join(decision.get("blockers") or []))
    # Persist durably under the verified assignment key (same pattern
    # as select_for_mission: claim, evidence, decision, digests).
    from app.agents.gabriel.sources.source_selection import (
        WorkflowSelectionStore, _RETAINED, _digest,
    )
    store = selection_store if selection_store is not None else WorkflowSelectionStore()
    fingerprint = mission_fingerprint(mission)
    claim = store.claim(assignment_key, fingerprint, mission_payload(mission))
    if claim.get("status") == "completed":
        out["selection"] = claim["decision"]
        return out
    if claim.get("status") != "winner":
        raise EnrollmentError(
            "verified selection for this assignment was already claimed "
            "and its outcome is uncertain; not retransmitted")
    evidence_payload = {
        "assignment_key": str(assignment_key), "run_id": claim.get("run_id", ""),
        "mission_fingerprint": fingerprint,
        "publisher_evidence": {
            "domain": evidence.domain, "dataset_id": evidence.dataset_id,
            "dataset_name": evidence.dataset_name,
            "attribution": evidence.attribution,
            "license_id": evidence.license_id,
            "freshness_days": evidence.freshness_days,
            "fields": list(evidence.fields),
            "category_distribution": dict(evidence.category_distribution),
            "sample_rows": evidence.sample_rows,
            "sample_geo_values": list(evidence.sample_geo_values),
            "evidence_digest": evidence.evidence_digest,
            "retrieved_at": evidence.retrieved_at,
        },
        "enrollment_digest": enrollment["digest"],
    }
    evidence_digest = hashlib.sha256(
        json.dumps(evidence_payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()
    _RETAINED.pop(decision["digest"], None)
    decision.update({"assignment_key": str(assignment_key),
                     "run_id": str(claim.get("run_id") or ""),
                     "mission_fingerprint": fingerprint,
                     "evidence_digest": evidence_digest})
    decision["digest"] = _digest(decision)
    store.complete(assignment_key, decision=decision, evidence=evidence_payload)
    _RETAINED[decision["digest"]] = json.loads(json.dumps(decision))
    out["selection"] = decision
    return out


_REGISTRY_CACHE: dict[str, Any] = {"at": None, "registry": None}


def effective_registry(*, store: EnrollmentStore | None = None):
    """Curated registry merged with unexpired autonomous enrollments.

    Curated entries always win. Autonomous entries add only hosts the
    curated registry does not know, only while their decision is
    ``enrolled`` and their review-due date has not passed. A database
    failure yields the curated registry alone (fail closed).
    """
    from app.agents.gabriel.source_registry import (
        PRODUCTION_REGISTRY, RegistryEntry, SourceClass, SourceRegistry,
    )
    now = datetime.now(timezone.utc)
    cached_at = _REGISTRY_CACHE.get("at")
    if cached_at is not None and (now - cached_at).total_seconds() < 60:
        return _REGISTRY_CACHE["registry"]
    registry = PRODUCTION_REGISTRY
    try:
        decisions = (store or EnrollmentStore()).load_enrollments()
    except Exception:
        decisions = []
    today = now.date().isoformat()
    entries = dict(PRODUCTION_REGISTRY.entries)
    for decision in decisions:
        if decision.get("status") != "enrolled":
            continue
        if str(decision.get("review_due") or "") < today:
            continue
        domain = str(decision.get("domain") or "").strip().lower()
        if not domain or domain in entries:
            continue
        if domain == "arcgis.com" or domain.endswith(".arcgis.com"):
            # Item-scoped authority never becomes a domain entry:
            # enrolling a shared Esri host would authorize every
            # tenant on it. Item scope binds at selection time only.
            continue
        entries[domain] = RegistryEntry(
            hostname=domain,
            source_class=SourceClass.AUTHORITATIVE,
            outlet_name=str(decision.get("publisher_name") or domain),
            publisher=str(decision.get("publisher_name") or domain),
            record_types=[],
            added_by=f"gabriel autonomous enrollment {ENROLLMENT_POLICY_VERSION}",
            added_on=str(decision.get("decided_at") or "")[:10],
            review_due=str(decision.get("review_due") or ""),
            note=str(decision.get("scope_note") or ""),
        )
    if len(entries) != len(PRODUCTION_REGISTRY.entries):
        registry = SourceRegistry(entries=entries)
    _REGISTRY_CACHE.update({"at": now, "registry": registry})
    return registry


def clear_registry_cache() -> None:
    _REGISTRY_CACHE.update({"at": None, "registry": None})
