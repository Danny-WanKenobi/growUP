"""Lead-to-source conversion for Gabriel.

A SearXNG result is a *lead* -- a page title and URL claiming
nothing about licensing, freshness, publisher authority, or even
that a dataset exists. This module converts a lead into a concrete
dataset resource (or records exactly why it cannot), using at most
two bounded requests under the shared HTTP policy:

- A Socrata resource URL (``/resource/<id>.json``), API view URL
  (``/api/views/<id>``), or dataset page (``/d/<id>``) resolves
  directly to that Socrata resource.
- An ArcGIS layer URL (``/rest/services/.../(FeatureServer|
  MapServer)/<n>``) resolves directly to that layer.
- An ArcGIS Hub item page or ``arcgis.com`` item link resolves
  through the publisher-maintained sharing item record, whose
  ``url`` field names the actual layer.
- Any other page is fetched once and its outbound links scanned:
  the first link naming a downloadable dataset file or a service
  layer becomes a ``bulk_candidate`` (recorded, never collected --
  no generic bulk adapter exists yet); otherwise the lead is
  ``unresolved`` with the reason recorded.

Conversion never enrolls or selects anything by itself. A resolved
Socrata/ArcGIS resource is handed to the existing publisher-direct
verification (``autonomous_enrollment.verify_and_select``), which
applies the full deterministic enrollment policy before any durable
selection exists. Unresolvable leads stay leads.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

_SOCRATA_ID = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")
_LAYER = re.compile(r"/rest/services/(.+)/(FeatureServer|MapServer)(?:/(\d+))?/?$", re.I)
_ITEM_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_DOWNLOAD = re.compile(r"\.(zip|csv|geojson|json|gpkg|shp)(\?|$)", re.I)


class LeadConversionError(ValueError):
    """A lead could not be resolved under the bounded policy."""


def _fetch(url: str, fetch: Callable[..., bytes] | None, timeout: float) -> bytes:
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes
    if fetch is not None:
        return fetch(url, {})
    try:
        return fetch_bytes(url, timeout=timeout, max_bytes=1_000_000,
                           name="lead resolution")
    except HttpPolicyError as exc:
        raise LeadConversionError(f"lead resolution refused: {exc}") from exc


def resolve_lead(lead: dict[str, Any], *, fetch: Callable[..., bytes] | None = None,
                 timeout: float = 15.0) -> dict[str, Any]:
    """Resolve one lead to a dataset resource, a bulk candidate, or unresolved.

    Returns a dict with ``outcome`` in {``resource``, ``bulk_candidate``,
    ``unresolved``}; ``resource`` outcomes carry the exact binding the
    generic adapters consume (platform, resource_url, dataset_id,
    publisher_domain). Unknowns stay unknown -- nothing is inferred
    from the lead's title beyond URL structure.
    """
    url = str(lead.get("lead_url") or lead.get("url") or "").strip()
    name = str(lead.get("name") or "")
    if not url:
        return {"outcome": "unresolved", "reason": "lead carries no URL", "lead": name}
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return {"outcome": "unresolved", "reason": "lead URL is not public https",
                "lead": name, "lead_url": url}
    path = parsed.path or ""

    # Direct Socrata resource / view / dataset-page URLs.
    if "/resource/" in path:
        dataset_id = path.rsplit("/resource/", 1)[-1].split(".")[0]
        if _SOCRATA_ID.match(dataset_id):
            return {"outcome": "resource", "lead": name, "lead_url": url,
                    "resource": {"platform": "socrata",
                                 "resource_url": f"https://{host}/resource/{dataset_id}.json",
                                 "dataset_id": dataset_id, "publisher_domain": host,
                                 "publisher_name": ""}}
    if "/api/views/" in path:
        dataset_id = path.rsplit("/api/views/", 1)[-1].strip("/")
        if _SOCRATA_ID.match(dataset_id):
            return {"outcome": "resource", "lead": name, "lead_url": url,
                    "resource": {"platform": "socrata",
                                 "resource_url": f"https://{host}/resource/{dataset_id}.json",
                                 "dataset_id": dataset_id, "publisher_domain": host,
                                 "publisher_name": ""}}
    if "/d/" in path:
        dataset_id = path.rsplit("/d/", 1)[-1].strip("/").split("/")[0]
        if _SOCRATA_ID.match(dataset_id):
            return {"outcome": "resource", "lead": name, "lead_url": url,
                    "resource": {"platform": "socrata",
                                 "resource_url": f"https://{host}/resource/{dataset_id}.json",
                                 "dataset_id": dataset_id, "publisher_domain": host,
                                 "publisher_name": ""}}

    # Direct ArcGIS layer URLs.
    layer_match = _LAYER.search(path)
    if layer_match:
        layer_url = f"https://{host}{path.rstrip('/')}"
        if layer_match.group(3) is None:
            layer_url += "/0"
        return {"outcome": "resource", "lead": name, "lead_url": url,
                "resource": {"platform": "arcgis", "resource_url": layer_url,
                             "dataset_id": "", "publisher_domain": host,
                             "publisher_name": ""}}

    # ArcGIS Hub / arcgis.com item pages: resolve via the item record.
    item_id = ""
    if "hub.arcgis.com" in host and "/datasets/" in path:
        item_id = path.rsplit("/datasets/", 1)[-1].strip("/").split("/")[0].split("-")[-1]
    query_id = (parse_qs(parsed.query).get("id") or [""])[0]
    if query_id and _ITEM_ID.match(query_id):
        item_id = query_id
    if item_id and _ITEM_ID.match(item_id):
        body = _fetch(
            f"https://www.arcgis.com/sharing/rest/content/items/{item_id}?f=pjson",
            fetch, timeout)
        try:
            item = json.loads(body or b"{}")
        except ValueError as exc:
            raise LeadConversionError("hub item record is not valid JSON") from exc
        layer_url = str(item.get("url") or "").strip()
        if layer_url and _LAYER.search(urlparse(layer_url).path or ""):
            return {"outcome": "resource", "lead": name, "lead_url": url,
                    "resource": {"platform": "arcgis",
                                 "resource_url": layer_url.rstrip("/"),
                                 "dataset_id": item_id,
                                 "publisher_domain":
                                     (urlparse(layer_url).hostname or "").lower(),
                                 "publisher_name": str(item.get("owner") or "")}}
        return {"outcome": "unresolved", "lead": name, "lead_url": url,
                "reason": "hub item record names no service layer"}

    # Any other page: one bounded fetch, scan outbound links for a
    # dataset file or service layer. The page itself is never treated
    # as data (no scraping); only explicit download/service links
    # count, and they convert to a recorded bulk candidate only.
    body = _fetch(url, fetch, timeout)
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    for href in re.findall(r'href="([^"]+)"', text):
        absolute = href if "://" in href else f"https://{host}{href if href.startswith('/') else '/' + href}"
        if _LAYER.search(urlparse(absolute).path or ""):
            return {"outcome": "resource", "lead": name, "lead_url": url,
                    "resource": {"platform": "arcgis",
                                 "resource_url": absolute.rstrip("/"),
                                 "dataset_id": "",
                                 "publisher_domain":
                                     (urlparse(absolute).hostname or "").lower(),
                                 "publisher_name": ""}}
        if _DOWNLOAD.search(absolute):
            return {"outcome": "bulk_candidate", "lead": name, "lead_url": url,
                    "download_url": absolute,
                    "reason": "page links a downloadable dataset file; "
                              "no generic bulk adapter exists yet"}
    return {"outcome": "unresolved", "lead": name, "lead_url": url,
            "reason": "no dataset resource or download link observed on the lead page"}


def convert_leads(
    ranked: list[dict[str, Any]],
    *,
    jurisdiction: str,
    assignment_key: str,
    max_leads: int = 4,
    fetch: Callable[..., bytes] | None = None,
    selection_store: Any = None,
    directory_urls: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Resolve ranked leads and verify any dataset resources found.

    Bounded: at most ``max_leads`` leads, each at most two resolution
    requests, then the existing publisher-direct verification for
    resolved resources. Returns ``selection``/``enrollment`` on the
    first fully verified source, plus the per-lead attempts ledger.
    Nothing here lowers any bar: an unverified or unenrolled
    resource converts to a recorded attempt, never a selection.
    """
    from app.agents.gabriel.sources.autonomous_enrollment import (
        EnrollmentError, verify_and_select,
    )
    attempts: list[dict[str, Any]] = []
    for entry in ranked[:max_leads]:
        lead_url = entry.get("lead_url") or entry.get("resource_url")
        if not lead_url:
            continue
        try:
            resolved = resolve_lead(
                {"name": entry.get("name"), "lead_url": lead_url}, fetch=fetch)
        except LeadConversionError as exc:
            attempts.append({"lead": entry.get("name"), "outcome": "unresolved",
                             "reason": str(exc)})
            continue
        record: dict[str, Any] = {
            "lead": entry.get("name"), "outcome": resolved["outcome"]}
        if resolved["outcome"] == "bulk_candidate":
            record["download_url"] = resolved.get("download_url")
            record["reason"] = resolved.get("reason")
            attempts.append(record)
            continue
        if resolved["outcome"] != "resource":
            record["reason"] = resolved.get("reason")
            attempts.append(record)
            continue
        resource = resolved["resource"]
        ranked_entry = {
            "name": entry.get("name"),
            "publisher_domain": resource["publisher_domain"],
            "publisher_name": resource.get("publisher_name", ""),
            "platform": resource["platform"],
            "adapter": "generic_arcgis" if resource["platform"] == "arcgis"
                       else "generic_socrata",
            "dataset_id": resource.get("dataset_id", ""),
            "resource_url": resource["resource_url"],
        }
        suffix = (resource["publisher_domain"] + ":" +
                  (resource.get("dataset_id") or
                   resource["resource_url"].rsplit("/", 1)[-1]))
        try:
            verified = verify_and_select(
                ranked_entry, jurisdiction=jurisdiction,
                selection_store=selection_store,
                assignment_key=f"{assignment_key}:lead:{suffix}",
                fetch=fetch, directory_urls=directory_urls)
        except EnrollmentError as exc:
            record.update({"outcome": "needs_more_evidence",
                           "missing": [str(exc)],
                           "resource_url": resource["resource_url"]})
            attempts.append(record)
            continue
        enrollment = verified.get("enrollment") or {}
        record.update({
            "outcome": "selected" if verified.get("selection") else
                       enrollment.get("status", "needs_more_evidence"),
            "missing": enrollment.get("missing"),
            "resource_url": resource["resource_url"],
            "domain": resource["publisher_domain"],
        })
        attempts.append(record)
        if verified.get("selection") is not None:
            return {"selection": verified["selection"],
                    "enrollment": enrollment, "attempts": attempts}
    return {"selection": None, "enrollment": None, "attempts": attempts}
