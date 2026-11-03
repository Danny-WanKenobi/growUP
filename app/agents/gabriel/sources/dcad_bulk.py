"""DCAD bulk official-data discovery provider (Gabriel precedence P1).

Dallas Central Appraisal District publishes the authoritative Dallas
County appraisal data as free bulk downloads (GIS Data Products:
parcel geometry shapefiles; Data Products: appraisal-roll account
files, joined on account number). DCAD's robots guidance disallows
crawling per-account pages, so bulk files are the lawful discovery
path; the single-account page adapter in ``dcad.py`` remains an
evidence tool, not a discovery crawler.

This provider ingests a documented CSV exchange produced from those
official downloads:

* ``dcad_accounts.csv`` — one row per appraisal account with columns
  ``account, situs_address, city, zip_code, land_use,
  improvement_description, improvement_area_sqft, year_built,
  latitude, longitude, appraised_value, value_year_label``.
* ``dcad_parcels.csv`` (optional) — ``account, latitude, longitude``
  parcel centroids used when an account row lacks coordinates.

Converting DCAD shapefiles/appraisal files into this exchange is an
offline preprocessing step over lawfully downloaded archives; this
module is the canonical ingestion point and mints no claim the
records do not support. Appraisal land use is a category *hint*
(a claim for the shared verification policy), never proof of current
use, vacancy, or availability, and appraised values are not prices.

Configuration: ``DCAD_BULK_DATA_DIR`` names the directory holding the
exchange files. Unconfigured or missing data fails closed.
"""

from __future__ import annotations

import csv
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    register_source,
)
from app.agents.gabriel.sources.dcad import DcadConfigError, dcad_account_url

logger = logging.getLogger(__name__)

DCAD_BULK_ENV = "DCAD_BULK_DATA_DIR"
ACCOUNTS_FILE = "dcad_accounts.csv"
PARCELS_FILE = "dcad_parcels.csv"
DATASET = "dcad_official_bulk_exchange"
DOCS_URL = "https://www.dallascad.org/GISDataProducts.aspx"

_REQUIRED_COLUMNS = {"account", "situs_address", "land_use"}
_WAREHOUSE_TOKENS = ("warehouse", "storage", "industrial", "manufacturing")
_COMMERCIAL_TOKENS = (
    "office", "retail", "commercial", "mixed", "medical", "restaurant",
    "hotel", "shopping", "bank",
)


def map_land_use(land_use: str, improvement_description: str, num_stories: str = "") -> str:
    """Map appraisal land-use text to a canonical category hint.

    Appraisal classifications describe the appraisal district's use
    class, not verified current use; hints feed the shared
    verification policy and default to ``other`` when unclear.
    """
    text = f"{land_use} {improvement_description}".lower()
    stories = _float_or_none(num_stories)
    # Same discovery threshold as OSM; only a preliminary category hint.
    # The paired primary improvement, not an unrelated structure, supplies height.
    if stories is not None and stories >= 8 and any(
        token in text for token in (*_WAREHOUSE_TOKENS, *_COMMERCIAL_TOKENS)
    ):
        return "high_rise"
    if any(token in text for token in _WAREHOUSE_TOKENS):
        return "warehouse_industrial"
    if any(token in text for token in _COMMERCIAL_TOKENS):
        return "commercial_property"
    return "other"


def _float_or_none(value: str | None) -> float | None:
    try:
        number = float(str(value).strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _load_rows(data_dir: Path) -> list[dict[str, str]]:
    accounts_path = data_dir / ACCOUNTS_FILE
    if not accounts_path.is_file():
        raise SourceError("dcad_bulk_data_missing")
    with accounts_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not _REQUIRED_COLUMNS.issubset(
            {name.strip() for name in reader.fieldnames}
        ):
            raise SourceError("dcad_bulk_unexpected_columns")
        rows = [row for row in reader]
    parcels_path = data_dir / PARCELS_FILE
    if parcels_path.is_file():
        centroids: dict[str, tuple[float, float]] = {}
        with parcels_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                lat = _float_or_none(row.get("latitude"))
                lon = _float_or_none(row.get("longitude"))
                account = str(row.get("account", "") or "").strip()
                if account and lat is not None and lon is not None:
                    centroids[account] = (lat, lon)
        for row in rows:
            account = str(row.get("account", "") or "").strip()
            if account in centroids and (
                _float_or_none(row.get("latitude")) is None
                or _float_or_none(row.get("longitude")) is None
            ):
                lat, lon = centroids[account]
                row["latitude"], row["longitude"] = str(lat), str(lon)
    return rows


def parse_exchange(data_dir: Path, retrieved_at: str) -> list[RawListing]:
    """Convert staged DCAD exchange rows to observed raw listings."""
    return _listings_from_rows(_load_rows(data_dir),retrieved_at)


def _listings_from_rows(rows, retrieved_at):
    listings: list[RawListing] = []
    seen: set[str] = set()
    for row in rows:
        account = str(row.get("account", "") or "").strip()
        address = str(row.get("situs_address", "") or "").strip()
        if not account or not address or account in seen:
            continue
        seen.add(account)
        land_use = str(row.get("land_use", "") or "").strip()
        improvement = str(row.get("improvement_description", "") or "").strip()
        try:
            element_url = dcad_account_url(account)
        except DcadConfigError:
            # Bulk accounts include alphanumeric unit ids that have
            # no account-page URL; discovery must not fail on them.
            element_url = None
        listings.append(
            RawListing(
                source="dcad_bulk",
                external_id=f"dcad:{account}",
                element_url=element_url,
                name=None,
                street_address=address,
                city=str(row.get("city", "") or "").strip() or None,
                state="TX",
                zip_code=str(row.get("zip_code", "") or "").strip() or None,
                category_hint=map_land_use(land_use, improvement, str(row.get("num_stories") or "")),
                latitude=_float_or_none(row.get("latitude")),
                longitude=_float_or_none(row.get("longitude")),
                retrieved_at=retrieved_at,
                tags={
                    "dataset": DATASET,
                    "exchange_file": ACCOUNTS_FILE,
                    "account": account,
                    "land_use": land_use,
                    "improvement_description": improvement,
                    "num_stories": str(row.get("num_stories") or ""),
                    "improvement_count": str(row.get("improvement_count") or ""),
                    "improvements": str(row.get("improvements") or ""),
                    "improvement_area_sqft": str(
                        row.get("improvement_area_sqft", "") or ""
                    ).strip(),
                    "year_built": str(row.get("year_built", "") or "").strip(),
                    "appraised_value": str(
                        row.get("appraised_value", "") or ""
                    ).strip(),
                    "value_year_label": str(
                        row.get("value_year_label", "") or ""
                    ).strip(),
                },
            )
        )
    return listings


class DcadBulkSource:
    """Official DCAD bulk discovery over a staged lawfully-downloaded exchange."""

    name = "dcad_bulk"
    description = (
        "Dallas Central Appraisal District bulk appraisal/parcel data "
        "(official P1 discovery; land use is a claim, not verified use)."
    )
    docs_url = DOCS_URL
    requires_credentials = False

    def search_page(self, bbox, categories, limit, timeout_seconds, cursor=None, *, whole_resource=False):
        from app.agents.gabriel.sources.base import DiscoveryPage
        import hashlib, json
        if type(limit) is not int or not 1 <= limit <= 25:
            raise SourceError("DCAD page limit must be an integer in 1..25")
        root = Path(os.environ.get(DCAD_BULK_ENV, ""))
        if not os.environ.get(DCAD_BULK_ENV, "").strip() or not (root / ACCOUNTS_FILE).is_file():
            raise SourceError("dcad_bulk_not_configured")
        # Snapshot identity includes both exchange inputs. A changed snapshot
        # requires an explicit new scan; never silently reuse its old offset.
        identity = [(str((root / name).resolve()), (root / name).stat().st_size,
                     (root / name).stat().st_mtime_ns)
                    for name in (ACCOUNTS_FILE, PARCELS_FILE) if (root / name).exists()]
        snapshot = hashlib.sha256(json.dumps([identity, None if whole_resource else bbox.as_tuple(), sorted(categories)]).encode()).hexdigest()
        offset = 0
        if cursor:
            try:
                token = json.loads(cursor)
                offset = token["offset"]
                if token["snapshot"] != snapshot or type(offset) is not int or offset < 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise SourceError("DCAD cursor/snapshot changed; explicit new scan required")
        if whole_resource:
            if cursor and token.get("mode") != "csv-cookie":
                raise SourceError("DCAD cursor mode changed; explicit new scan required")
            # readline iteration preserves tell()/seek() even for quoted
            # multiline CSV rows. Only one bounded page is held in memory.
            with (root / ACCOUNTS_FILE).open(newline="",encoding="utf-8-sig") as handle:
                header_reader=csv.reader(iter(handle.readline,""))
                headers=next(header_reader,[])
                if not _REQUIRED_COLUMNS.issubset(set(headers)):
                    raise SourceError("dcad_bulk_unexpected_columns")
                if cursor:
                    handle.seek(offset)
                reader=csv.DictReader(iter(handle.readline,""),fieldnames=headers)
                raw=[]
                for _ in range(limit):
                    row=next(reader,None)
                    if row is None:
                        break
                    raw.append(row)
                following=handle.tell()
                done=handle.read(1)==""
            items=_listings_from_rows(raw,datetime.now(timezone.utc).isoformat(timespec="seconds"))
            next_cursor=None if done else json.dumps({"offset":following,"snapshot":snapshot,"mode":"csv-cookie"})
            return DiscoveryPage(items,next_cursor,done,"entire staged DCAD CSV snapshot in file order")
        cached=getattr(self,"_scan_cache",None)
        if cached is not None and cached[0] == snapshot:
            rows=cached[1]
        else:
            rows = parse_exchange(root, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            south, west, north, east = bbox.as_tuple()
            rows = sorted((r for r in rows if r.category_hint in set(categories) and
                          (whole_resource or (r.latitude is not None and r.longitude is not None
                           and south <= r.latitude <= north and west <= r.longitude <= east))),key=lambda r:r.external_id)
            self._scan_cache=(snapshot,rows)
        items = rows[offset:offset+limit]
        following = offset + len(items)
        done = following >= len(rows)
        return DiscoveryPage(items, None if done else json.dumps({"offset":following,"snapshot":snapshot}),
                             done, "entire staged DCAD exchange snapshot" if whole_resource else "approved DCAD window only")

    def search(
        self,
        bbox: BoundingBox,
        categories: list[str],
        limit: int,
        timeout_seconds: float,
    ) -> list[RawListing]:
        data_dir_raw = os.environ.get(DCAD_BULK_ENV, "").strip()
        if not data_dir_raw:
            raise SourceError("dcad_bulk_not_configured")
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        listings = parse_exchange(Path(data_dir_raw), retrieved_at)
        south, west, north, east = bbox.as_tuple()
        wanted = set(categories)
        inside = [
            item
            for item in listings
            if item.latitude is not None
            and item.longitude is not None
            and south <= item.latitude <= north
            and west <= item.longitude <= east
            and item.category_hint in wanted
        ]
        if len(inside) < len(listings):
            logger.info(
                "dcad_bulk: %d of %d staged accounts inside bbox/categories",
                len(inside), len(listings),
            )
        return inside[:limit]


register_source(DcadBulkSource())
