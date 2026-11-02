"""Controlled Census geocoder county evidence collection (bounded).

One approved source only: the documented single-address geographies
endpoint (``geographies/address?street=&city=&state=&zip=``) fetched
over HTTPS with fixed limits. This module is the sole minter of
Census collection provenance: the collector builds the request URL
server-side from the stored operational address (callers supply no
URL or host), transmits exactly once outside any business
transaction, and retains the raw bytes with their SHA-256. There is
no upload path and no caller flag that can mint or impersonate this
provenance. A digest proves byte integrity, not truth or source
authenticity.

County admission is county-only and narrowly scoped: a unique
supported address match establishes the county of the matched
address location for discovery territory and pilot accounting only.
It never establishes parcel boundaries, buildings or suites,
ownership, occupancy, vacancy, distress, current use, category
verification, zoning, or agricultural permission. Address-range
interpolation does not guarantee the correct parcel, and known
parcel/address disputes stay unresolved.

Owner curation: collection is disabled unless
CENSUS_COLLECTION_ENABLED=1. Benchmark/vintage are the documented
``Public_AR_Current`` / ``Current_Current`` names; their returned
identity is preserved per retrieval, and a mutable "Current"
dataset is never described as immutable across dates. Census is
never enrolled in the property-category verification registry,
and the shared verification policy is untouched here. Live
retrieval is owner-executed after separate approval; offline
fixtures (labeled test material, not retained live evidence)
drive implementation tests.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel

from app.agents.gabriel.attribution import (
    CountyAttribution,
    CountyClaim,
    CountyEvidenceStatus,
    TerritoryEligibility,
    normalize_county_claim,
)
from app.agents.gabriel.dedup import normalize_state, normalize_text
from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

CENSUS_HOST = "geocoding.geo.census.gov"
CENSUS_PATH = "/geocoder/geographies/address"
CENSUS_BENCHMARK = "Public_AR_Current"
CENSUS_VINTAGE = "Current_Current"
CENSUS_COLLECTOR = "census-geocoder-geographies-address"
CENSUS_COLLECTOR_REVISION = "census-1"
CENSUS_COUNTY_VERSION = "census-county-1"
CENSUS_PROVENANCE = "adapter_collected"
CENSUS_CLAIM_AUTHORITY = "census_geocoder"
CENSUS_TIMEOUT_SECONDS = 12.0
CENSUS_MAX_BODY_BYTES = 256 * 1024
CENSUS_ENABLED_ENV = "CENSUS_COLLECTION_ENABLED"
CENSUS_USER_AGENT = "growUP-census-collector/1"
CENSUS_COUNTY_SCOPE = (
    "County of the matched Census address location - discovery territory only."
)
TEXAS_STATE_FIPS = "48"
_MAX_COMPONENT_CHARS = 200

_HOUSE_NUMBER_RE = re.compile(r"^\s*(\d+)\b(.*)$")
_DIGITS_RE = re.compile(r"\D")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# Texas county reference: GEOID -> county name, verified 2026-11-01 against
# https://www2.census.gov/geo/docs/reference/codes/files/national_county.txt
# (static reference data; county FIPS are stable identifiers).
_TEXAS_COUNTIES_BY_GEOID: dict[str, str] = {
    "48001": "Anderson County",
    "48003": "Andrews County",
    "48005": "Angelina County",
    "48007": "Aransas County",
    "48009": "Archer County",
    "48011": "Armstrong County",
    "48013": "Atascosa County",
    "48015": "Austin County",
    "48017": "Bailey County",
    "48019": "Bandera County",
    "48021": "Bastrop County",
    "48023": "Baylor County",
    "48025": "Bee County",
    "48027": "Bell County",
    "48029": "Bexar County",
    "48031": "Blanco County",
    "48033": "Borden County",
    "48035": "Bosque County",
    "48037": "Bowie County",
    "48039": "Brazoria County",
    "48041": "Brazos County",
    "48043": "Brewster County",
    "48045": "Briscoe County",
    "48047": "Brooks County",
    "48049": "Brown County",
    "48051": "Burleson County",
    "48053": "Burnet County",
    "48055": "Caldwell County",
    "48057": "Calhoun County",
    "48059": "Callahan County",
    "48061": "Cameron County",
    "48063": "Camp County",
    "48065": "Carson County",
    "48067": "Cass County",
    "48069": "Castro County",
    "48071": "Chambers County",
    "48073": "Cherokee County",
    "48075": "Childress County",
    "48077": "Clay County",
    "48079": "Cochran County",
    "48081": "Coke County",
    "48083": "Coleman County",
    "48085": "Collin County",
    "48087": "Collingsworth County",
    "48089": "Colorado County",
    "48091": "Comal County",
    "48093": "Comanche County",
    "48095": "Concho County",
    "48097": "Cooke County",
    "48099": "Coryell County",
    "48101": "Cottle County",
    "48103": "Crane County",
    "48105": "Crockett County",
    "48107": "Crosby County",
    "48109": "Culberson County",
    "48111": "Dallam County",
    "48113": "Dallas County",
    "48115": "Dawson County",
    "48117": "Deaf Smith County",
    "48119": "Delta County",
    "48121": "Denton County",
    "48123": "DeWitt County",
    "48125": "Dickens County",
    "48127": "Dimmit County",
    "48129": "Donley County",
    "48131": "Duval County",
    "48133": "Eastland County",
    "48135": "Ector County",
    "48137": "Edwards County",
    "48139": "Ellis County",
    "48141": "El Paso County",
    "48143": "Erath County",
    "48145": "Falls County",
    "48147": "Fannin County",
    "48149": "Fayette County",
    "48151": "Fisher County",
    "48153": "Floyd County",
    "48155": "Foard County",
    "48157": "Fort Bend County",
    "48159": "Franklin County",
    "48161": "Freestone County",
    "48163": "Frio County",
    "48165": "Gaines County",
    "48167": "Galveston County",
    "48169": "Garza County",
    "48171": "Gillespie County",
    "48173": "Glasscock County",
    "48175": "Goliad County",
    "48177": "Gonzales County",
    "48179": "Gray County",
    "48181": "Grayson County",
    "48183": "Gregg County",
    "48185": "Grimes County",
    "48187": "Guadalupe County",
    "48189": "Hale County",
    "48191": "Hall County",
    "48193": "Hamilton County",
    "48195": "Hansford County",
    "48197": "Hardeman County",
    "48199": "Hardin County",
    "48201": "Harris County",
    "48203": "Harrison County",
    "48205": "Hartley County",
    "48207": "Haskell County",
    "48209": "Hays County",
    "48211": "Hemphill County",
    "48213": "Henderson County",
    "48215": "Hidalgo County",
    "48217": "Hill County",
    "48219": "Hockley County",
    "48221": "Hood County",
    "48223": "Hopkins County",
    "48225": "Houston County",
    "48227": "Howard County",
    "48229": "Hudspeth County",
    "48231": "Hunt County",
    "48233": "Hutchinson County",
    "48235": "Irion County",
    "48237": "Jack County",
    "48239": "Jackson County",
    "48241": "Jasper County",
    "48243": "Jeff Davis County",
    "48245": "Jefferson County",
    "48247": "Jim Hogg County",
    "48249": "Jim Wells County",
    "48251": "Johnson County",
    "48253": "Jones County",
    "48255": "Karnes County",
    "48257": "Kaufman County",
    "48259": "Kendall County",
    "48261": "Kenedy County",
    "48263": "Kent County",
    "48265": "Kerr County",
    "48267": "Kimble County",
    "48269": "King County",
    "48271": "Kinney County",
    "48273": "Kleberg County",
    "48275": "Knox County",
    "48277": "Lamar County",
    "48279": "Lamb County",
    "48281": "Lampasas County",
    "48283": "La Salle County",
    "48285": "Lavaca County",
    "48287": "Lee County",
    "48289": "Leon County",
    "48291": "Liberty County",
    "48293": "Limestone County",
    "48295": "Lipscomb County",
    "48297": "Live Oak County",
    "48299": "Llano County",
    "48301": "Loving County",
    "48303": "Lubbock County",
    "48305": "Lynn County",
    "48307": "McCulloch County",
    "48309": "McLennan County",
    "48311": "McMullen County",
    "48313": "Madison County",
    "48315": "Marion County",
    "48317": "Martin County",
    "48319": "Mason County",
    "48321": "Matagorda County",
    "48323": "Maverick County",
    "48325": "Medina County",
    "48327": "Menard County",
    "48329": "Midland County",
    "48331": "Milam County",
    "48333": "Mills County",
    "48335": "Mitchell County",
    "48337": "Montague County",
    "48339": "Montgomery County",
    "48341": "Moore County",
    "48343": "Morris County",
    "48345": "Motley County",
    "48347": "Nacogdoches County",
    "48349": "Navarro County",
    "48351": "Newton County",
    "48353": "Nolan County",
    "48355": "Nueces County",
    "48357": "Ochiltree County",
    "48359": "Oldham County",
    "48361": "Orange County",
    "48363": "Palo Pinto County",
    "48365": "Panola County",
    "48367": "Parker County",
    "48369": "Parmer County",
    "48371": "Pecos County",
    "48373": "Polk County",
    "48375": "Potter County",
    "48377": "Presidio County",
    "48379": "Rains County",
    "48381": "Randall County",
    "48383": "Reagan County",
    "48385": "Real County",
    "48387": "Red River County",
    "48389": "Reeves County",
    "48391": "Refugio County",
    "48393": "Roberts County",
    "48395": "Robertson County",
    "48397": "Rockwall County",
    "48399": "Runnels County",
    "48401": "Rusk County",
    "48403": "Sabine County",
    "48405": "San Augustine County",
    "48407": "San Jacinto County",
    "48409": "San Patricio County",
    "48411": "San Saba County",
    "48413": "Schleicher County",
    "48415": "Scurry County",
    "48417": "Shackelford County",
    "48419": "Shelby County",
    "48421": "Sherman County",
    "48423": "Smith County",
    "48425": "Somervell County",
    "48427": "Starr County",
    "48429": "Stephens County",
    "48431": "Sterling County",
    "48433": "Stonewall County",
    "48435": "Sutton County",
    "48437": "Swisher County",
    "48439": "Tarrant County",
    "48441": "Taylor County",
    "48443": "Terrell County",
    "48445": "Terry County",
    "48447": "Throckmorton County",
    "48449": "Titus County",
    "48451": "Tom Green County",
    "48453": "Travis County",
    "48455": "Trinity County",
    "48457": "Tyler County",
    "48459": "Upshur County",
    "48461": "Upton County",
    "48463": "Uvalde County",
    "48465": "Val Verde County",
    "48467": "Van Zandt County",
    "48469": "Victoria County",
    "48471": "Walker County",
    "48473": "Waller County",
    "48475": "Ward County",
    "48477": "Washington County",
    "48479": "Webb County",
    "48481": "Wharton County",
    "48483": "Wheeler County",
    "48485": "Wichita County",
    "48487": "Wilbarger County",
    "48489": "Willacy County",
    "48491": "Williamson County",
    "48493": "Wilson County",
    "48495": "Winkler County",
    "48497": "Wise County",
    "48499": "Wood County",
    "48501": "Yoakum County",
    "48503": "Young County",
    "48505": "Zapata County",
    "48507": "Zavala County",
}


class CensusError(ValueError):
    """Census collection, parsing, or admission refused (safe to report)."""


class CensusConfigError(CensusError):
    """Collector misconfigured, disabled, or given unusable input."""


class CensusFetchError(CensusError):
    """Transport refused or failed; at most one attempt was made.

    ``transmitted`` records whether the attempt reached the
    provider: DNS/public-destination refusals never transmit,
    while timeouts, unreachable transports, refused redirects,
    unexpected statuses, content-type/size refusals, and decode
    failures all count as transmitted (conservative: a timeout
    may or may not have arrived, and is charged either way).
    """

    def __init__(self, message: str, *, transmitted: bool = False) -> None:
        super().__init__(message)
        self.transmitted = transmitted


class CensusParseError(CensusError):
    """Retained bytes are not the documented response structure."""


def collection_enabled() -> bool:
    """True only when the owner explicitly enables Census collection."""
    return os.getenv(CENSUS_ENABLED_ENV, "").strip() == "1"


def require_collection_allowed() -> None:
    """Fail closed unless Census collection is enabled."""
    if not collection_enabled():
        raise CensusConfigError("Census collection is not enabled")


def _clean_component(value: Any, name: str, *, required: bool) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        if required:
            raise CensusConfigError(f"submitted {name} is required")
        return ""
    if len(cleaned) > _MAX_COMPONENT_CHARS:
        raise CensusConfigError(f"submitted {name} is too long")
    return cleaned


def _require_texas(state: str) -> str:
    if normalize_state(state) != "tx":
        raise CensusConfigError("Census pilot collection requires a Texas state")
    return "TX"


def census_request_url(street: str, city: str, state: str, zip_code: str) -> str:
    """Server-constructed request URL for one stored address.

    Fixed approved host/path with the documented benchmark, vintage,
    and JSON format; only the address components vary, and they come
    from the stored operational record. Texas state is required:
    this adapter serves the twelve-county Texas pilot only.
    """
    clean_street = _clean_component(street, "street address", required=True)
    clean_city = _clean_component(city, "city", required=False)
    clean_state = _require_texas(_clean_component(state, "state", required=True))
    clean_zip = _clean_component(zip_code, "ZIP code", required=False)
    params = {
        "street": clean_street,
        "city": clean_city,
        "state": clean_state,
        "zip": clean_zip,
        "benchmark": CENSUS_BENCHMARK,
        "vintage": CENSUS_VINTAGE,
        "format": "json",
    }
    return f"https://{CENSUS_HOST}{CENSUS_PATH}?{urlencode(params)}"


class CensusCollection(BaseModel):
    """One controlled retrieval, minted only by :func:`collect_address`."""

    collector: str = CENSUS_COLLECTOR
    collector_revision: str = CENSUS_COLLECTOR_REVISION
    submitted_street: str
    submitted_city: str = ""
    submitted_state: str = ""
    submitted_zip: str = ""
    benchmark: str = CENSUS_BENCHMARK
    vintage: str = CENSUS_VINTAGE
    url: str
    retrieved_at: str
    status_code: int
    content_type: str
    body_bytes: int
    body_sha256: str
    body_text: str
    principal: str
    collection_provenance: Literal["adapter_collected"] = "adapter_collected"


def collect_address(
    street: str,
    city: str,
    state: str,
    zip_code: str,
    *,
    principal: str,
    timeout_seconds: float = CENSUS_TIMEOUT_SECONDS,
) -> CensusCollection:
    """Fetch one stored address with fixed fail-closed limits.

    Exactly one HTTPS GET: pinned host/path with a server-built
    query, resolved addresses verified public beforehand,
    12-second timeout, 256 KiB cap, no redirects (any 3xx
    refused), no retries, no secondary fetches, no environment
    proxy (explicit direct connection). Certificate validation
    stays enabled (never disabled). Any refusal raises with
    nothing retained. Runs outside any business or custody
    transaction; the caller persists the result.
    """
    import httpx

    from app.agents.gabriel.sources.dcad import (
        DcadFetchError,
        check_public_destination,
    )

    require_collection_allowed()
    if not str(principal or "").strip():
        raise CensusConfigError("collection requires an authenticated principal")
    if (
        not timeout_seconds
        or timeout_seconds <= 0
        or timeout_seconds > CENSUS_TIMEOUT_SECONDS
    ):
        raise CensusConfigError(
            f"timeout must be within (0, {CENSUS_TIMEOUT_SECONDS}]s"
        )
    url = census_request_url(street, city, state, zip_code)
    try:
        check_public_destination(CENSUS_HOST)
    except DcadFetchError as exc:
        raise CensusFetchError(str(exc), transmitted=False) from exc
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with httpx.stream(
            "GET",
            url,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": CENSUS_USER_AGENT},
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise CensusFetchError("redirect refused", transmitted=True)
            if status != 200:
                raise CensusFetchError(f"unexpected status {status}", transmitted=True)
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                raise CensusFetchError("unexpected content type", transmitted=True)
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > CENSUS_MAX_BODY_BYTES:
                    raise CensusFetchError("response too large", transmitted=True)
                chunks.append(chunk)
            body = b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise CensusFetchError("collection timed out", transmitted=True) from exc
    except httpx.HTTPError as exc:
        raise CensusFetchError("collection unreachable", transmitted=True) from exc
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CensusFetchError("response is not UTF-8", transmitted=True) from exc
    submitted = {
        "street": _clean_component(street, "street address", required=True),
        "city": _clean_component(city, "city", required=False),
        "state": _require_texas(_clean_component(state, "state", required=True)),
        "zip": _clean_component(zip_code, "ZIP code", required=False),
    }
    return CensusCollection(
        submitted_street=submitted["street"],
        submitted_city=submitted["city"],
        submitted_state=submitted["state"],
        submitted_zip=submitted["zip"],
        url=url,
        retrieved_at=retrieved_at,
        status_code=status,
        content_type=content_type,
        body_bytes=len(body),
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_text=text,
        principal=str(principal).strip(),
    )


def parse_geographies(body_text: str) -> dict[str, Any]:
    """Parse retained bytes into the documented response structure.

    Returns ``{"input": ..., "addressMatches": [...]}`` with raw
    values preserved. Anything else — unparseable JSON, a missing
    result object, or a non-list match set — is malformed and
    refused; match-level validation belongs to assessment.
    """
    import json

    try:
        payload = json.loads(body_text or "")
    except (ValueError, TypeError) as exc:
        raise CensusParseError("response is not JSON") from exc
    if not isinstance(payload, dict):
        raise CensusParseError("response is not a JSON object")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise CensusParseError("response has no result object")
    matches = result.get("addressMatches")
    if not isinstance(matches, list):
        raise CensusParseError("response has no address match list")
    for match in matches:
        if not isinstance(match, dict):
            raise CensusParseError("address match is not an object")
    raw_input = result.get("input")
    return {
        "input": dict(raw_input) if isinstance(raw_input, dict) else {},
        "addressMatches": matches,
    }


def _split_house_number(street: str) -> tuple[int, str] | None:
    match = _HOUSE_NUMBER_RE.match(street or "")
    if match is None:
        return None
    try:
        return int(match.group(1)), normalize_text(match.group(2))
    except ValueError:
        return None


def _zip5(value: Any) -> str:
    return _DIGITS_RE.sub("", str(value or ""))[:5]


def _county_name_from_geoid(geoid: str) -> str | None:
    return _TEXAS_COUNTIES_BY_GEOID.get(geoid)


def assess_county(
    collection: dict[str, Any],
    collection_digest: str,
    property_record: dict[str, Any],
) -> dict[str, Any]:
    """Assess one retained collection into a county-only result (pure).

    Requires exactly one supported address match with compatible
    normalized house number, street, and Texas state; explicit
    city/ZIP handling (mismatches refuse, absent sides record
    as not compared, never filled from targeting context); and
    exactly one county geography with valid consistent
    identifiers cross-checked between the returned name and the
    reference GEOID table. Outside-territory Texas results are
    admitted as outside, never rewritten. No match, ambiguity,
    malformed geography, or incompatibility refuses admission.
    Only collector-minted bytes are assessed: uploads and
    caller-supplied provenance are refused before parsing.
    """
    if not isinstance(collection, dict):
        raise CensusError("collection must be a mapping")
    if str(collection.get("collection_provenance", "") or "") != CENSUS_PROVENANCE:
        raise CensusError("collection lacks collector-authenticated provenance")
    if str(collection.get("collector", "") or "") != CENSUS_COLLECTOR:
        raise CensusError("collection is not a Census geographies retrieval")
    if (
        str(collection.get("collector_revision", "") or "")
        != CENSUS_COLLECTOR_REVISION
    ):
        raise CensusError("collection is not a supported collector revision")
    body_text = collection.get("body_text", "")
    if not isinstance(body_text, str) or not body_text:
        raise CensusError("collection retains no response body")
    actual_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    if actual_sha != str(collection.get("body_sha256", "") or ""):
        raise CensusError("retained body fails digest verification")
    if not isinstance(property_record, dict):
        raise CensusError("property record must be a mapping")
    property_id = str(property_record.get("id", "") or "")
    if not property_id:
        raise CensusError("property record has no id")
    submitted = {
        "street": str(collection.get("submitted_street", "") or ""),
        "city": str(collection.get("submitted_city", "") or ""),
        "state": str(collection.get("submitted_state", "") or ""),
        "zip": str(collection.get("submitted_zip", "") or ""),
    }
    if not submitted["street"] or normalize_state(submitted["state"]) != "tx":
        raise CensusError("submitted address is not a usable Texas street address")
    parsed = parse_geographies(body_text)
    matches = parsed["addressMatches"]
    if not matches:
        raise CensusError("no Census address match for the submitted address")
    if len(matches) > 1:
        raise CensusError(
            f"ambiguous Census address match ({len(matches)} matches)"
        )
    match = matches[0]
    matched_address = match.get("matchedAddress", "")
    if not isinstance(matched_address, str) or not matched_address.strip():
        raise CensusError("address match has no matched address")
    matched_street = matched_address.split(",")[0].strip()
    submitted_number = _split_house_number(submitted["street"])
    matched_number = _split_house_number(matched_street)
    if submitted_number is None or matched_number is None:
        raise CensusError("house number is not comparable")
    if submitted_number[0] != matched_number[0]:
        raise CensusError(
            "submitted house number does not match the Census match"
        )
    if not submitted_number[1] or submitted_number[1] != matched_number[1]:
        raise CensusError("submitted street does not match the Census match")
    components = match.get("addressComponents", {})
    if not isinstance(components, dict):
        raise CensusError("address match has no address components")
    if normalize_state(components.get("state", "")) != "tx":
        raise CensusError("Census match is not in Texas")
    comparisons: dict[str, Any] = {
        "house_number": {
            "submitted": submitted_number[0],
            "matched": matched_number[0],
            "compared": True,
        },
        "street": {
            "submitted": submitted["street"],
            "matched": matched_street,
            "compared": True,
        },
        "state": {
            "submitted": submitted["state"],
            "matched": str(components.get("state", "") or ""),
            "compared": True,
        },
    }
    for field, component_key in (("city", "city"), ("zip", "zip")):
        ours = submitted[field]
        theirs = str(components.get(component_key, "") or "")
        if field == "zip":
            ours_norm, theirs_norm = _zip5(ours), _zip5(theirs)
            compared = len(ours_norm) == 5 and len(theirs_norm) == 5
        else:
            ours_norm, theirs_norm = normalize_text(ours), normalize_text(theirs)
            compared = bool(ours_norm) and bool(theirs_norm)
        if compared and ours_norm != theirs_norm:
            raise CensusError(
                f"submitted {field} disagrees with the Census match "
                f"({ours!r} vs {theirs!r}); conflicting components are "
                "never filled from targeting context"
            )
        comparisons[field] = {
            "submitted": ours,
            "matched": theirs,
            "compared": compared,
        }
    coordinates = match.get("coordinates", {})
    if not isinstance(coordinates, dict):
        raise CensusError("address match has no coordinates")
    try:
        lon = float(coordinates.get("x", float("nan")))
        lat = float(coordinates.get("y", float("nan")))
    except (TypeError, ValueError) as exc:
        raise CensusError("address match coordinates are not numbers") from exc
    if lon != lon or lat != lat or lon in (float("inf"), float("-inf")) or lat in (
        float("inf"),
        float("-inf"),
    ):
        raise CensusError("address match coordinates are not finite")
    geographies = match.get("geographies", {})
    if not isinstance(geographies, dict):
        raise CensusError("address match has no geographies")
    counties = geographies.get("Counties", [])
    if not isinstance(counties, list) or len(counties) != 1:
        raise CensusError("Census match does not carry exactly one county result")
    county = counties[0]
    if not isinstance(county, dict):
        raise CensusError("county result is not an object")
    geoid = str(county.get("GEOID", "") or "")
    state_fips = str(county.get("STATE", "") or "")
    county_fips = str(county.get("COUNTY", "") or "")
    name = str(county.get("NAME", "") or "")
    if (
        not geoid
        or not state_fips
        or not county_fips
        or not name
        or geoid != f"{state_fips}{county_fips}"
        or len(geoid) != 5
        or not geoid.isdigit()
    ):
        raise CensusError("county identifiers are invalid or inconsistent")
    if state_fips != TEXAS_STATE_FIPS:
        raise CensusError("county geography is not in Texas")
    canonical = normalize_county_claim(name)
    if canonical is None:
        raise CensusError("county name does not normalize to a county")
    table_name = _county_name_from_geoid(geoid)
    if table_name is None:
        raise CensusError("county GEOID is not a known Texas county")
    if normalize_county_claim(table_name) != canonical:
        raise CensusError("county name and GEOID disagree")
    territory = (
        TerritoryEligibility.WITHIN_TERRITORY
        if canonical in COUNTY_PRIORITY_ORDER
        else TerritoryEligibility.OUTSIDE_TERRITORY
    )
    returned = parsed["input"]
    return {
        "assessment_version": CENSUS_COUNTY_VERSION,
        "collector": CENSUS_COLLECTOR,
        "collector_revision": CENSUS_COLLECTOR_REVISION,
        "collection_provenance": CENSUS_PROVENANCE,
        "property_id": property_id,
        "submitted": dict(submitted),
        "matched_address": matched_address.strip(),
        "address_components": {
            key: components.get(key, "") for key in sorted(components)
        },
        "coordinates": {"x": lon, "y": lat},
        "tiger_line": dict(match.get("tigerLine", {}) or {}),
        "county": {
            "name": name,
            "geoid": geoid,
            "state_fips": state_fips,
            "county_fips": county_fips,
            "canonical": canonical,
            "territory": territory.value,
        },
        "benchmark_requested": CENSUS_BENCHMARK,
        "vintage_requested": CENSUS_VINTAGE,
        "benchmark_returned": returned.get("benchmark", {}),
        "vintage_returned": returned.get("vintage", {}),
        "scope_label": CENSUS_COUNTY_SCOPE,
        "method": "census_unique_address_match",
        "comparisons": comparisons,
        "collection_url": str(collection.get("url", "") or ""),
        "collection_digest": str(collection_digest or ""),
        "retrieved_at": str(collection.get("retrieved_at", "") or ""),
        "principal": str(collection.get("principal", "") or ""),
    }


def has_collector_county_claim(county_attribution: Any) -> bool:
    """Whether a county attribution carries a Census collector claim.

    Dict-level shape check used by the credit gates: a backing
    claim with the collector authority whose source URL sits on
    the Census host. True authentication is the code path (only
    :func:`apply_county_to_dossier`, fed by verified collector
    records, mints these claims) plus stored-row integrity; this
    check refuses misshapen claims, never a database write.
    """
    from urllib.parse import urlparse

    if not isinstance(county_attribution, dict):
        return False
    claims = county_attribution.get("claims") or []
    if not isinstance(claims, list):
        return False
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if str(claim.get("authority", "") or "") != CENSUS_CLAIM_AUTHORITY:
            continue
        try:
            host = urlparse(str(claim.get("source_url", "") or "")).hostname or ""
        except ValueError:
            continue
        if host.lower() == CENSUS_HOST:
            return True
    return False


def apply_county_to_dossier(
    dossier: dict[str, Any],
    county_result: dict[str, Any],
    *,
    collection_digest: str,
    property_id: str,
    current_address: dict[str, Any],
) -> dict[str, Any]:
    """Apply an admitted county result to one dossier (pure, narrow).

    Verifies collector provenance, supported revisions, and
    property/collection/address bindings, then refuses when any
    retained county claim conflicts with the admitted county
    (claims stay retained; contradictions are explained, never
    overwritten). The dossier's own address must describe the
    collected address: evidence collected for address B never
    credits a dossier describing address A, even under the same
    property UUID, and unresolved dossier addresses refuse.
    Returns ``{"applied": True, "county_attribution": ...}`` or
    ``{"applied": False, "reason": ...}``; the dossier dict
    itself is never mutated. Only this function mints VERIFIED
    county attribution, and only from verified collector
    evidence — a caller-supplied "verified" status is never
    authority.
    """
    if not isinstance(dossier, dict) or not isinstance(county_result, dict):
        return {"applied": False, "reason": "dossier and county result are required"}
    if (
        str(county_result.get("collection_provenance", "") or "")
        != CENSUS_PROVENANCE
        or str(county_result.get("collector", "") or "") != CENSUS_COLLECTOR
        or str(county_result.get("collector_revision", "") or "")
        != CENSUS_COLLECTOR_REVISION
        or str(county_result.get("assessment_version", "") or "")
        != CENSUS_COUNTY_VERSION
    ):
        return {
            "applied": False,
            "reason": "county result lacks supported collector provenance",
        }
    if str(county_result.get("property_id", "") or "") != str(property_id or ""):
        return {"applied": False, "reason": "county result binds a different property"}
    if str(county_result.get("collection_digest", "") or "") != str(
        collection_digest or ""
    ):
        return {
            "applied": False,
            "reason": "county result does not match the retained collection",
        }
    submitted = county_result.get("submitted") or {}
    dossier_street = str(dossier.get("street_address", "") or "")
    dossier_city = str(dossier.get("city", "") or "")
    dossier_state = str(dossier.get("state", "") or "")
    dossier_number = _split_house_number(dossier_street)
    submitted_number = _split_house_number(str(submitted.get("street", "") or ""))
    if dossier_number is None:
        return {
            "applied": False,
            "reason": (
                "dossier street address is unresolved; county evidence "
                "cannot bind a dossier without a comparable address"
            ),
        }
    if submitted_number is None or dossier_number != submitted_number:
        return {
            "applied": False,
            "reason": (
                f"dossier street address ({dossier_street!r}) does not match "
                "the collected address; evidence collected for one address "
                "never credits a dossier describing another"
            ),
        }
    if not dossier_state.strip():
        return {
            "applied": False,
            "reason": (
                "dossier state is unresolved; county evidence cannot bind "
                "a dossier without a comparable address"
            ),
        }
    if normalize_state(dossier_state) != normalize_state(submitted.get("state", "")):
        return {
            "applied": False,
            "reason": (
                f"dossier state ({dossier_state!r}) does not match "
                "the collected address"
            ),
        }
    if dossier_city.strip() and str(submitted.get("city", "") or "").strip():
        if normalize_text(dossier_city) != normalize_text(submitted.get("city", "")):
            return {
                "applied": False,
                "reason": (
                    f"dossier city ({dossier_city!r}) does not match "
                    "the collected address"
                ),
            }
    current = current_address or {}
    for field in ("street", "city", "state", "zip"):
        if field == "state":
            old = normalize_state(submitted.get(field, ""))
            new = normalize_state(current.get(field, ""))
        elif field == "zip":
            old, new = _zip5(submitted.get(field, "")), _zip5(current.get(field, ""))
        else:
            old = normalize_text(submitted.get(field, ""))
            new = normalize_text(current.get(field, ""))
        if old != new:
            return {
                "applied": False,
                "reason": (
                    "property address changed since collection; "
                    "the county result no longer binds this record"
                ),
            }
    county = county_result.get("county") or {}
    canonical = county.get("canonical", "")
    if normalize_county_claim(canonical) != canonical:
        return {"applied": False, "reason": "admitted county is not canonical"}
    try:
        territory = TerritoryEligibility(str(county.get("territory", "") or ""))
    except ValueError:
        return {"applied": False, "reason": "admitted territory is invalid"}
    if territory is TerritoryEligibility.UNKNOWN:
        return {"applied": False, "reason": "admitted territory is unresolved"}
    existing = dossier.get("county_attribution") or {}
    prior_claims = existing.get("claims") or [] if isinstance(existing, dict) else []
    conflicts: list[str] = []
    for claim in prior_claims if isinstance(prior_claims, list) else []:
        if not isinstance(claim, dict):
            continue
        normalized = normalize_county_claim(
            str(claim.get("normalized", "") or claim.get("original", "") or "")
        )
        if normalized is not None and normalized != canonical:
            conflicts.append(normalized)
    if conflicts:
        return {
            "applied": False,
            "reason": (
                "retained county claims conflict with the admitted county "
                f"({canonical} vs {sorted(set(conflicts))}); "
                "all claims retained, contradiction unresolved"
            ),
        }
    claim = CountyClaim(
        original=str(county.get("name", "") or ""),
        normalized=canonical,
        source_url=str(county_result.get("collection_url", "") or ""),
        source_domain=CENSUS_HOST,
        excerpt=f"matched address: {county_result.get('matched_address', '')}",
        observed_at=str(county_result.get("retrieved_at", "") or "") or None,
        authority=CENSUS_CLAIM_AUTHORITY,
    )
    preserved: list[Any] = []
    for prior in prior_claims if isinstance(prior_claims, list) else []:
        if not isinstance(prior, dict):
            continue
        if (
            str(prior.get("authority", "") or "") == CENSUS_CLAIM_AUTHORITY
            and str(prior.get("source_url", "") or "") == claim.source_url
            and str(prior.get("normalized", "") or "") == canonical
        ):
            continue  # identical collector claim already retained: no duplicate
        preserved.append(dict(prior))
    preserved.append(claim)
    attribution = CountyAttribution(
        evidence_status=CountyEvidenceStatus.VERIFIED,
        territory=territory,
        value=canonical,
        claims=preserved,
        method=CENSUS_COUNTY_SCOPE,
        search_county=str(existing.get("search_county", "") or "")
        if isinstance(existing, dict)
        else "",
    )
    return {"applied": True, "county_attribution": attribution.model_dump(mode="json")}


def census_evidence_refs(
    *,
    admission_run_id: str,
    collection_record_key: str,
    county_record_key: str,
    collection_digest: str,
    county_digest: str,
    county_result: dict[str, Any],
) -> dict[str, Any]:
    """Build the persisted Census evidence locator block (pure).

    Binds an admission run id, both record keys, both stored
    digests, and the assessed county identity to one locator the
    dossier carries, so retrieval never reconstructs identities
    from URL text. Refuses malformed locators, unsupported
    collector revisions, and county results whose own
    collection binding disagrees. Digest truth itself is
    verified at load time against the stored rows.
    """
    from uuid import UUID as _UUID

    try:
        run_uuid = _UUID(str(admission_run_id or ""))
    except (ValueError, AttributeError, TypeError) as exc:
        raise CensusError("admission run id is not an operational UUID") from exc
    if not str(collection_record_key or "").strip():
        raise CensusError("collection record key is required")
    if not str(county_record_key or "").strip():
        raise CensusError("county record key is required")
    for name, value in (
        ("collection_digest", collection_digest),
        ("county_digest", county_digest),
    ):
        text = str(value or "")
        if not _DIGEST_RE.fullmatch(text):
            raise CensusError(f"{name} is not a SHA-256 hex digest")
    if not isinstance(county_result, dict):
        raise CensusError("county result is required")
    if (
        str(county_result.get("collection_provenance", "") or "")
        != CENSUS_PROVENANCE
        or str(county_result.get("collector", "") or "") != CENSUS_COLLECTOR
        or str(county_result.get("collector_revision", "") or "")
        != CENSUS_COLLECTOR_REVISION
        or str(county_result.get("assessment_version", "") or "")
        != CENSUS_COUNTY_VERSION
    ):
        raise CensusError("county result lacks supported collector provenance")
    if str(county_result.get("collection_digest", "") or "") != str(
        collection_digest or ""
    ):
        raise CensusError("county result does not match the retained collection")
    county = county_result.get("county") or {}
    canonical = county.get("canonical", "") if isinstance(county, dict) else ""
    if normalize_county_claim(canonical) != canonical:
        raise CensusError("admitted county is not canonical")
    return {
        "admission_run_id": str(run_uuid),
        "collection_record_key": str(collection_record_key),
        "county_record_key": str(county_record_key),
        "collection_digest": str(collection_digest),
        "county_digest": str(county_digest),
        "property_id": str(county_result.get("property_id", "") or ""),
        "county": canonical,
        "submitted": dict(county_result.get("submitted") or {}),
        "matched_address": str(county_result.get("matched_address", "") or ""),
        "scope": CENSUS_COUNTY_SCOPE,
    }


def census_admission_run_key(
    *,
    property_id: str,
    street: str,
    city: str,
    state: str,
    zip_code: str,
    revision: str,
    attempt: str,
) -> str:
    """Deterministic admission-run key binding property, address, attempt."""
    from app.workflow import digest_payload

    normalized = "|".join(
        [
            normalize_text(street),
            normalize_text(city),
            normalize_state(state),
            _zip5(zip_code),
        ]
    )
    return "census-" + digest_payload(
        {
            "property_id": str(property_id),
            "address": normalized,
            "revision": str(revision),
            "attempt": str(attempt),
        }
    )[:16]


def census_record_key(*, street: str, city: str, state: str, zip_code: str) -> str:
    """Stable record key for one collected address within its run."""
    from app.workflow import digest_payload

    normalized = "|".join(
        [
            normalize_text(street),
            normalize_text(city),
            normalize_state(state),
            _zip5(zip_code),
        ]
    )
    return "census:" + digest_payload({"address": normalized})[:16]


__all__ = [
    "CENSUS_BENCHMARK",
    "CENSUS_CLAIM_AUTHORITY",
    "CENSUS_COLLECTOR",
    "CENSUS_COLLECTOR_REVISION",
    "CENSUS_COUNTY_SCOPE",
    "CENSUS_COUNTY_VERSION",
    "CENSUS_HOST",
    "CENSUS_MAX_BODY_BYTES",
    "CENSUS_PATH",
    "CENSUS_PROVENANCE",
    "CENSUS_TIMEOUT_SECONDS",
    "CENSUS_VINTAGE",
    "TEXAS_STATE_FIPS",
    "CensusCollection",
    "CensusConfigError",
    "CensusError",
    "CensusFetchError",
    "CensusParseError",
    "apply_county_to_dossier",
    "assess_county",
    "census_admission_run_key",
    "census_evidence_refs",
    "census_record_key",
    "census_request_url",
    "collect_address",
    "collection_enabled",
    "has_collector_county_claim",
    "parse_geographies",
    "require_collection_allowed",
]
