"""Generic open-data collection adapters (Socrata / ArcGIS / CKAN).

A generic adapter collects *only* the exact resource retained in a
server-constructed selection (``selected_resource``: URL, platform,
publisher, jurisdiction, field mapping, observed terms/freshness,
policy/resource digest). It never reads ``GENERIC_*`` environment
locators and cannot be bootstrapped: without a binding it fails
closed. Changing URL, publisher, jurisdiction, digest, category
request or assignment on replay refuses before transmission.

Transport goes through the shared ``http_policy`` helper (public
HTTPS, no private/DNS-non-global endpoints, no redirects,
``trust_env=False``, streamed byte cap before buffering, bounded
time/rows, no retries). Collection itself needs a durable idempotent
claim (existing workflow ``research_artifact`` records, no DDL): the
claim commits before transmission, a completed claim replays with
zero network, and a claimed-but-unfinished attempt stays consumed
(uncertain, never auto-retried/refunded).

Rows are strict: missing/non-finite coordinates or coordinates
outside the requested bbox are excluded (never returned as a
fallback). Category comes only from observed use/building/stories
through the existing OSM normalization; unknown stays ``other``
(unresolved) and is filtered out when canonical categories are
requested. State/county/vacancy/ownership/occupancy/zoning are never
invented; observed dataset fields are retained as provenance.
"""
from __future__ import annotations

import csv
import base64
import hashlib
import io
import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    SourceTimeout,
    register_source,
)
from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes

MAX_BODY_BYTES = 1_000_000
MAX_CSV_ROWS = 5000

_ID = ("account", "parcel_id", "parcelid", "apn", "pin", "id", "objectid", "fid", "property_id",
       "taxparcelidentificationnumber", "tax_parcel_identification_number",
       "parcelidentificationnumber", "parcel_number", "parcelnumber", "parcel_no",
       "propertyid", "taxparcel", "folio", "reid", "propid", "prop_id", "geoid")
_ADDRESS = ("situs_address", "situsconcat", "address", "street_address", "full_address", "addr",
            "property_address", "location_address")
_CITY = ("city", "municipality", "town", "situscity", "situs_city")
_STATE = ("state", "st")
_ZIP = ("zip_code", "zip", "zipcode", "postal_code", "situszip", "situs_zip")
_LAT = ("latitude", "lat", "y")
_LON = ("longitude", "lon", "lng", "x")
_USE = ("land_use", "landuse", "use_code", "property_class", "classification",
        "building_class", "bldg_class_desc", "description", "use",
        "primaryspaceuse", "primary_space_use", "space_use", "spaceuse",
        "predominantspaceuse", "largestpropertyusetype", "epapropertytype")
_BUILDING = ("building", "building_type", "structure_type", "buildingtype")
_STORIES = ("num_stories", "stories", "building_levels", "levels", "floors")
_HEIGHT = ("height_m",)  # Generic unitless height has no safe default unit.
_NAME = ("name", "property_name", "building_name")

Fetch = Callable[[str, dict, float], bytes]


def _default_fetch(url: str, params: dict, timeout: float) -> bytes:
    try:
        return fetch_bytes(url, params=params, timeout=timeout,
                           max_bytes=MAX_BODY_BYTES, name="generic resource")
    except HttpPolicyError as exc:
        if "transport failed" in str(exc) and "Timeout" in str(exc):
            raise SourceTimeout("generic_open_data_timeout") from exc
        raise SourceError(f"generic_open_data_refused: {exc}") from exc


def _pick(row: dict, aliases: tuple[str, ...], mapping: dict, key: str) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    mapped = str((mapping or {}).get(key) or "").strip().lower()
    if mapped and lowered.get(mapped) not in (None, ""):
        return str(lowered[mapped]).strip()
    for alias in aliases:
        value = lowered.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def category_from_observed(row: dict, mapping: dict | None = None) -> str:
    """Canonical category from observed use/building/stories, else other."""
    from app.agents.gabriel.sources.overpass import map_category

    mapping = mapping or {}
    text = " ".join(_pick(row, aliases, mapping, key) for aliases, key in
                    ((_USE, "use"), (_BUILDING, "building"))).lower()
    building = ""
    for token, tag in (("warehouse", "warehouse"), ("storage", "warehouse"),
                       ("industrial", "industrial"), ("skyscraper", "skyscraper"),
                       ("office", "office"), ("retail", "retail"),
                       ("hotel", "commercial"), ("nonresidential", "commercial"),
                       ("commercial", "commercial")):
        if token in text:
            building = tag
            break
    tags: dict[str, Any] = {"building": building}
    stories = _pick(row, _STORIES, mapping, "stories")
    height = _pick(row, _HEIGHT, mapping, "height_m")
    if stories:
        tags["building:levels"] = stories
    if height:
        tags["height"] = height
    return map_category(tags)


def _coord(row: dict, aliases: tuple[str, ...], mapping: dict, key: str) -> float | None:
    raw = _pick(row, aliases, mapping, key)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _ring_centroid(ring: list) -> tuple[float, float] | None:
    """Shoelace centroid of one GeoJSON ring; vertex mean if degenerate."""
    points: list[tuple[float, float]] = []
    for coord in ring or []:
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            try:
                points.append((float(coord[0]), float(coord[1])))
            except (TypeError, ValueError):
                continue
    if len(points) < 3:
        return points[0] if points else None
    if points[0] == points[-1]:
        points = points[:-1]
    area2 = 0.0
    cx = cy = 0.0
    for i, (x, y) in enumerate(points):
        nx, ny = points[(i + 1) % len(points)]
        cross = x * ny - nx * y
        area2 += cross
        cx += (x + nx) * cross
        cy += (y + ny) * cross
    if abs(area2) < 1e-18:
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))
    return (cx / (3 * area2), cy / (3 * area2))


def _geometry_point(geometry: Any) -> tuple[float, float] | None:
    """Representative (lon, lat) for Point/Polygon/MultiPolygon geometry.

    Parcel layers are polygons by nature; a listing's coordinates are
    the polygon centroid -- a representative point for the parcel,
    never a fabricated address or a surveyed boundary point.
    """
    if not isinstance(geometry, dict):
        return None
    gtype = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if gtype == "Point" and len(coords) == 2:
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            return None
        return (lon, lat) if math.isfinite(lon) and math.isfinite(lat) else None
    if gtype == "Polygon" and coords:
        return _ring_centroid(coords[0])
    if gtype == "MultiPolygon" and coords:
        best = None
        for polygon in coords:
            if polygon:
                centroid = _ring_centroid(polygon[0])
                if centroid is not None:
                    best = centroid
                    break
        return best
    return None


def _category_code_hint(row: dict, binding: dict) -> str | None:
    """Resource-recorded appraisal-code mapping, when present.

    The selection resource may carry ``category_codes`` ({field,
    map}) recorded during publisher verification from the layer's
    own observed code distribution. Only a code present in that
    recorded map classifies; anything else falls through to the
    generic observed-use classification, unchanged.
    """
    spec = binding.get("category_codes") if isinstance(binding, dict) else None
    if not isinstance(spec, dict):
        return None
    field = str(spec.get("field") or "")
    mapping = spec.get("map") or {}
    if not field or not isinstance(mapping, dict) or not mapping:
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    value = lowered.get(field.lower())
    if value in (None, ""):
        return None
    return mapping.get(str(value).strip().upper())


def _to_listings(rows: list[tuple[dict, Any]], *, source: str,
                binding: dict) -> tuple[list[RawListing], int]:
    mapping = dict(binding.get("field_mapping") or {})
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    listings: list[RawListing] = []
    seen: set[str] = set()
    excluded = 0
    for row, geometry in rows:
        external = _pick(row, _ID, mapping, "external_id")
        if not external or external in seen:
            excluded += 1
            continue
        seen.add(external)
        lat = _coord(row, _LAT, mapping, "latitude")
        lon = _coord(row, _LON, mapping, "longitude")
        if lat is None or lon is None:
            point = _geometry_point(geometry)
            if point is not None:
                lon, lat = point
        observed_category = category_from_observed(row, mapping)
        code_category = _category_code_hint(row, binding)
        # A broad appraisal use code does not negate observed building
        # height/stories. Preserve the more specific high-rise class.
        category = (observed_category if observed_category == "high_rise"
                    else code_category or observed_category)
        listings.append(RawListing(
            source=source,
            external_id=f"{source}:{external}",
            element_url=None,
            name=_pick(row, _NAME, mapping, "name") or None,
            street_address=_pick(row, _ADDRESS, mapping, "street_address") or None,
            city=_pick(row, _CITY, mapping, "city") or None,
            state=_pick(row, _STATE, mapping, "state") or None,
            zip_code=_pick(row, _ZIP, mapping, "zip_code") or None,
            category_hint=category,
            latitude=lat, longitude=lon,
            retrieved_at=retrieved_at,
            tags={
                "dataset_url": str(binding.get("resource_url") or ""),
                "platform": str(binding.get("platform") or ""),
                "observed_use": _pick(row, _USE, mapping, "use"),
                "selection_digest": str(binding.get("selection_digest") or ""),
                "resource_digest": str(binding.get("resource_digest") or ""),
                "field_mapping": mapping,
                "platform_record": dict(row),
                **({"geometry": geometry} if geometry is not None else {}),
            },
        ))
    return listings, excluded


def _inside_strict(listings: list[RawListing], bbox: BoundingBox,
                   categories: list[str], limit: int) -> list[RawListing]:
    south, west, north, east = bbox.as_tuple()
    wanted = set(categories or [])
    out = []
    for item in listings:
        if item.latitude is None or item.longitude is None:
            continue
        if not (south <= item.latitude <= north and west <= item.longitude <= east):
            continue
        if wanted and item.category_hint not in wanted:
            continue
        out.append(item)
        if len(out) >= max(1, limit):
            break
    return out


class WorkflowCollectionStore:
    """Durable collection claim on existing workflow records (no DDL)."""

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        from app.properties_db import get_connection
        return get_connection()

    @staticmethod
    def _key(assignment: str, fingerprint: str) -> str:
        # The assignment is stable. Changed parameters must conflict, not
        # mint another request key under the original approval.
        return f"generic-collection:{assignment}"

    def claim(self, run_id: str, assignment: str, fingerprint: str,
              payload: dict) -> dict[str, Any]:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import (
            get_record_in_transaction, insert_record_if_absent_in_transaction,
            get_run_in_transaction, add_usage_in_transaction)
        try:
            run_uuid = UUID(str(run_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise SourceError("generic collection claim needs a durable run identity") from exc
        key = self._key(assignment, fingerprint)
        conn = self._open()
        try:
            with conn:
                run = get_run_in_transaction(conn, run_uuid, for_update=True)
                if run is None:
                    raise SourceError("generic collection finding run is missing")
                selected = get_record_in_transaction(conn, run_uuid, KIND_RESEARCH_ARTIFACT, "selection", 1)
                from app.agents.gabriel.sources.source_selection import WorkflowSelectionStore
                if selected is None:
                    raise SourceError("generic collection finding selection is missing")
                decision = WorkflowSelectionStore._verified_decision(conn, run, selected)
                if (decision.get("assignment_key") != assignment
                        or decision.get("resource_digest") != payload.get("resource_digest")
                        or decision.get("selected_resource") != payload.get("resource")):
                    raise SourceError("generic collection does not bind its retained finding selection")
                prior = get_record_in_transaction(conn, run_uuid, KIND_RESEARCH_ARTIFACT, key, 1)
                if prior is not None and (prior.digest != digest_payload(prior.payload)
                                          or prior.payload.get("fingerprint") != fingerprint):
                    raise SourceError("generic collection replay scope/digest mismatch")
                inserted = insert_record_if_absent_in_transaction(
                    conn, run_uuid, kind=KIND_RESEARCH_ARTIFACT, record_key=key,
                    version=1, payload=payload, digest=digest_payload(payload))
                if inserted is not None:
                    request_cap = payload.get("requests_reserved", 1)
                    if (isinstance(request_cap, bool) or not isinstance(request_cap, int)
                            or not 1 <= request_cap <= 4):
                        raise SourceError("generic collection request reservation is invalid")
                    add_usage_in_transaction(
                        conn, run_uuid, "generic_collection_requests_reserved", request_cap)
                    conn.commit()
                    return {"status": "winner"}
                done = get_record_in_transaction(
                    conn, run_uuid, KIND_RESEARCH_ARTIFACT, key, 2)
                conn.commit()
                if done is not None:
                    if done.digest != digest_payload(done.payload) or str((done.payload or {}).get("fingerprint", "")) != fingerprint:
                        raise SourceError("generic collection replay fingerprint mismatch")
                    return {"status": "completed", "outcome": dict(done.payload or {})}
                return {"status": "uncertain"}
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def complete(self, run_id: str, assignment: str, fingerprint: str,
                 outcome: dict) -> None:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import put_record_in_transaction
        run_uuid = UUID(str(run_id))
        key = self._key(assignment, fingerprint)
        conn = self._open()
        try:
            with conn:
                put_record_in_transaction(
                    conn, run_uuid, kind=KIND_RESEARCH_ARTIFACT, record_key=key,
                    version=2, payload=outcome, digest=digest_payload(outcome))
                conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass


def binding_from_selection(selection: dict, adapter: str) -> dict[str, Any]:
    """Extract the retained resource binding; refuse any mismatch."""
    from app.agents.gabriel.sources.source_selection import (
        SourceSelectionError, enforce_selection)

    try:
        enforce_selection(source=adapter,
                          jurisdiction=str((selection.get("mission") or {}).get("jurisdiction") or ""),
                          selection=selection)
    except SourceSelectionError as exc:
        raise SourceError(str(exc)) from exc
    res = dict(selection.get("selected_resource") or {})
    res["selection_digest"] = str(selection.get("digest") or "")
    res["resource_digest"] = str(selection.get("resource_digest") or "")
    res["assignment_key"] = str(selection.get("assignment_key") or "")
    res["claim_run_id"] = str(selection.get("run_id") or "")
    return res


class _GenericBase:
    platform = ""

    def __init__(self, fetch: Fetch | None = None, binding: dict | None = None,
                 claim_store: Any = None) -> None:
        self._fetch = fetch or _default_fetch
        self._binding = dict(binding) if binding else None
        self._claim_store = claim_store

    def bind(self, binding: dict) -> None:
        self._binding = dict(binding)

    def _fetch_body(self, url: str, params: dict, timeout: float) -> bytes:
        if len(self._response_pages) >= self._request_cap:
            raise SourceError("generic collection request cap exhausted")
        body = self._fetch(url, params, timeout)
        if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
            raise SourceError("generic resource body must be bounded bytes")
        self._response_body = body
        self._response_pages.append({
            "url": url, "params": dict(params),
            "body_b64": base64.b64encode(body).decode("ascii"),
            "digest": hashlib.sha256(body).hexdigest(),
        })
        return body

    def _require_binding(self) -> dict:
        if not self._binding or not str(self._binding.get("resource_url") or "").strip():
            raise SourceError(f"{self.name}_not_configured")
        if str(self._binding.get("adapter") or self.name) != self.name:
            raise SourceError(f"{self.name}_binding_adapter_mismatch")
        return self._binding

    def _collect(self, bbox: BoundingBox, categories: list[str], limit: int,
                 timeout_seconds: float, fetch_rows: Callable[[dict], list]) -> list[RawListing]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise SourceError("generic collection row limit must be an integer in 1..25")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30):
            raise SourceError("generic collection timeout must be finite in (0,30]")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                   for v in bbox.as_tuple()):
            raise SourceError("generic collection requires finite coordinates")
        south, west, north, east = bbox.as_tuple()
        if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
            raise SourceError("generic collection bounding box is invalid")
        if any(c not in ("high_rise", "commercial_property", "warehouse_industrial") for c in categories):
            raise SourceError("generic collection category is unsupported")
        binding = self._require_binding()
        request_cap = binding.get("max_pages", 1) if self.platform == "arcgis" else 1
        if (isinstance(request_cap, bool) or not isinstance(request_cap, int)
                or not 1 <= request_cap <= 4):
            raise SourceError("generic ArcGIS max_pages must be an integer in 1..4")
        self._request_cap = request_cap
        self._response_pages: list[dict[str, Any]] = []
        resource = {key: value for key, value in binding.items()
                    if key not in {"selection_digest", "resource_digest", "assignment_key", "claim_run_id"}}
        fingerprint = hashlib.sha256(json.dumps({
            "resource_digest": binding.get("resource_digest"),
            "resource": resource, "timeout_seconds": timeout_seconds,
            "bbox": list(bbox.as_tuple()), "categories": list(categories),
            "limit": int(limit)}, sort_keys=True).encode()).hexdigest()
        assignment = str(binding.get("assignment_key") or "")
        store = self._claim_store
        if store is None and assignment and binding.get("claim_run_id"):
            store = WorkflowCollectionStore()
        if store is not None:
            if not assignment:
                raise SourceError("generic collection claim needs an assignment_key")
            claim = store.claim(str(binding.get("claim_run_id") or ""), assignment,
                                fingerprint, {"state": "claimed", "fingerprint": fingerprint,
                                              "resource_digest": binding.get("resource_digest"),
                                              "resource": resource,
                                              "requests_reserved": request_cap,
                                              "resource_url": binding.get("resource_url")})
            if claim.get("status") == "completed":
                outcome = claim.get("outcome") or {}
                raw_rows = outcome.get("rows", [])
                digest = hashlib.sha256(json.dumps(raw_rows, sort_keys=True, default=str).encode()).hexdigest()
                if (outcome.get("fingerprint") != fingerprint or outcome.get("resource_digest") != binding.get("resource_digest")
                        or outcome.get("response_digest") != digest):
                    raise SourceError("generic collection replay scope/evidence digest mismatch")
                try:
                    raw = base64.b64decode(outcome.get("raw_response_b64", ""), validate=True)
                except (ValueError, TypeError) as exc:
                    raise SourceError("generic collection retained response is malformed") from exc
                if hashlib.sha256(raw).hexdigest() != outcome.get("raw_response_digest"):
                    raise SourceError("generic collection retained response digest mismatch")
                pages = outcome.get("response_pages")
                if pages is not None:
                    if not isinstance(pages, list) or not 1 <= len(pages) <= request_cap:
                        raise SourceError("generic collection retained page count mismatch")
                    for page in pages:
                        if (not isinstance(page, dict) or not isinstance(page.get("url"), str)
                                or not isinstance(page.get("params"), dict)):
                            raise SourceError("generic collection retained page is malformed")
                        try:
                            page_body = base64.b64decode(page.get("body_b64", ""), validate=True)
                        except (ValueError, TypeError) as exc:
                            raise SourceError("generic collection retained page is malformed") from exc
                        if (len(page_body) > MAX_BODY_BYTES
                                or hashlib.sha256(page_body).hexdigest() != page.get("digest")):
                            raise SourceError("generic collection retained page digest mismatch")
                    if pages[-1].get("body_b64") != outcome.get("raw_response_b64"):
                        raise SourceError("generic collection final retained page mismatch")
                rows = [RawListing(**r) for r in raw_rows]
                return _inside_strict(rows, bbox, categories, limit)
            if claim.get("status") != "winner":
                raise SourceError("generic collection attempt already claimed and uncertain; not retried")
            listings = fetch_rows(binding)
            store.complete(str(binding.get("claim_run_id") or ""), assignment, fingerprint, {
                "state": "completed", "fingerprint": fingerprint,
                "resource_digest": binding.get("resource_digest"),
                "raw_response_b64": base64.b64encode(self._response_body).decode("ascii"),
                "raw_response_digest": hashlib.sha256(self._response_body).hexdigest(),
                "response_pages": self._response_pages,
                "requests_reserved": request_cap,
                "requests_made": len(self._response_pages),
                "response_digest": hashlib.sha256(
                    json.dumps([asdict(x) for x in listings], sort_keys=True, default=str).encode()).hexdigest(),
                "rows": [asdict(x) for x in listings]})
            return listings
        if self._fetch is _default_fetch:
            raise SourceError("real generic collection requires a durable assignment claim")
        return fetch_rows(binding)  # explicitly injected fixture transport only


class GenericSocrataSource(_GenericBase):
    name = "generic_socrata"
    platform = "socrata"
    description = "Generic Socrata SODA dataset collection (retained resource only)."
    docs_url = "https://dev.socrata.com/"
    requires_credentials = False

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        def _rows(binding: dict) -> list[RawListing]:
            body = self._fetch_body(str(binding["resource_url"]),
                               {"$limit": str(max(1, limit))}, timeout_seconds)
            try:
                rows = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise SourceError("generic_socrata_invalid_json") from exc
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise SourceError("generic_socrata_unexpected_response")
            listings, _ = _to_listings([(r, None) for r in rows],
                                       source=self.name, binding=binding)
            return _inside_strict(listings, bbox, categories, limit)

        return self._collect(bbox, categories, limit, timeout_seconds, _rows)


class GenericArcGISSource(_GenericBase):
    name = "generic_arcgis"
    platform = "arcgis"
    description = "Generic ArcGIS FeatureServer layer collection (retained resource only)."
    docs_url = "https://developers.arcgis.com/rest/"
    requires_credentials = False

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        def _rows(binding: dict) -> list[RawListing]:
            south, west, north, east = bbox.as_tuple()
            url = str(binding["resource_url"]).rstrip("/")
            if not url.endswith("/query"):
                url += "/query"
            # The default remains one request. More pages require an
            # explicit retained resource bound (1..4), reserved in the
            # durable claim before any transmission.
            page_size = max(1, limit)
            collected: list[RawListing] = []
            seen_ids: set[str] = set()
            for page in range(binding.get("max_pages", 1)):
                params = {
                    "where": "1=1", "outFields": "*",
                    "geometry": f"{west},{south},{east},{north}",
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326", "outSR": "4326", "f": "geojson",
                    "resultRecordCount": str(page_size),
                    "resultOffset": str(page * page_size),
                }
                body = self._fetch_body(url, params, timeout_seconds)
                try:
                    payload = json.loads(body.decode("utf-8"))
                    pairs = [(dict(f.get("properties") or {}), f.get("geometry"))
                             for f in payload.get("features", []) if isinstance(f, dict)]
                except (ValueError, UnicodeDecodeError, AttributeError) as exc:
                    raise SourceError("generic_arcgis_invalid_response") from exc
                listings, _ = _to_listings(pairs, source=self.name, binding=binding)
                for listing in _inside_strict(listings, bbox, categories, limit):
                    if listing.external_id not in seen_ids:
                        seen_ids.add(listing.external_id)
                        collected.append(listing)
                if len(collected) >= limit or len(pairs) < page_size:
                    break
            return collected[:limit]

        return self._collect(bbox, categories, limit, timeout_seconds, _rows)


class GenericCKANSource(_GenericBase):
    name = "generic_ckan"
    platform = "ckan"
    description = "Generic CKAN CSV resource collection (retained resource only)."
    docs_url = "https://docs.ckan.org/"
    requires_credentials = False

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        def _rows(binding: dict) -> list[RawListing]:
            body = self._fetch_body(str(binding["resource_url"]), {}, timeout_seconds)
            try:
                reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
                rows = []
                for i, row in enumerate(reader):
                    if i >= MAX_CSV_ROWS:
                        break
                    rows.append((dict(row), None))
            except (UnicodeDecodeError, csv.Error) as exc:
                raise SourceError("generic_ckan_invalid_csv") from exc
            listings, _ = _to_listings(rows, source=self.name, binding=binding)
            return _inside_strict(listings, bbox, categories, limit)

        return self._collect(bbox, categories, limit, timeout_seconds, _rows)


_CLASSES = {c.name: c for c in (GenericSocrataSource, GenericArcGISSource, GenericCKANSource)}


def source_for_selection(selection: dict, adapter: str, *,
                         fetch: Fetch | None = None, claim_store: Any = None) -> _GenericBase:
    """Build a fresh bound collector for one retained selection."""
    cls = _CLASSES.get(adapter)
    if cls is None:
        raise SourceError(f"not a generic adapter: {adapter!r}")
    return cls(fetch=fetch, binding=binding_from_selection(selection, adapter),
               claim_store=claim_store)


register_source(GenericSocrataSource())
register_source(GenericArcGISSource())
register_source(GenericCKANSource())
