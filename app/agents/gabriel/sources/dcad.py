"""Controlled DCAD commercial-account evidence collection (bounded).

One approved source only: a single Dallas Central Appraisal District
commercial-account page (``AcctDetailCom.aspx?ID=<account>``) fetched
over pinned HTTPS with fixed limits. This module is the sole minter
of DCAD collection provenance: the collector builds the request URL
server-side from owner-allowlisted accounts (callers supply no URL),
transmits exactly once outside any business transaction, and retains
the raw bytes with their SHA-256. There is no upload path and no
caller flag that can mint or impersonate this provenance. A digest
proves byte integrity, not truth or source authenticity.

Parsing reads the actual labeled HTML structure with the standard
library only: the account title, the confirmed address label, and
the confirmed improvements heading with the nested improvement
rows beneath it. Each entry keeps its inline description, Total
Area, and Year Built associated in one row; raw values and
locators are retained alongside normalized values, and missing
fields stay unknown. Source period labels ("Current 2027",
"Certified ... 2026") are preserved literally and never converted
into effective dates, use assertions, occupancy, or financial inputs.

Admission binds one persisted collection to one operational
property: the returned account must echo in the account title,
and the source street address must agree exactly (normalized)
with the property record before alignment is supported. Alignment covers
the property account / street address only -- never a building or
suite. Improvement descriptions are descriptive scope only and are
never mapped to present operating use. The shared verification
policy remains the sole category engine; DCAD evidence carries no
defining use facts, so it cannot verify a category by itself.

Owner curation: collection is disabled unless
DCAD_COLLECTION_ENABLED=1 and the account is listed in
DCAD_ALLOWED_ACCOUNTS. Production registry enrollment stays
owner-controlled and is untouched here. Live retrieval is
owner-executed after separate approval; offline fixtures (labeled
test material, not retained live evidence) drive implementation
tests.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.gabriel.dedup import normalize_text
from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY, SourceRegistry
from app.agents.gabriel.verification import (
    IdentityAttestation,
    VerificationRequest,
    VoiceInput,
    verify_category,
)

DCAD_HOST = "www.dallascad.org"
DCAD_PATH = "/AcctDetailCom.aspx"
DCAD_COLLECTOR = "dcad-commercial-account-html"
DCAD_COLLECTOR_REVISION = "dcad-1"
DCAD_ASSESSMENT_VERSION = "dcad-assess-1"
DCAD_RECORD_TYPE = "commercial_account"
DCAD_PROVENANCE = "adapter_collected"
DCAD_TIMEOUT_SECONDS = 12.0
DCAD_MAX_BODY_BYTES = 256 * 1024
DCAD_ENABLED_ENV = "DCAD_COLLECTION_ENABLED"
DCAD_ACCOUNTS_ENV = "DCAD_ALLOWED_ACCOUNTS"
DCAD_USER_AGENT = "growUP-DCAD-collector/1"

# Confirmed live-page element ids (observed source markup).
DCAD_ACCOUNT_TITLE_ID = "lblPageTitle"
DCAD_ADDR_LABEL_ID = "PropAddr1_lblPropAddr"
DCAD_IMP_HEADING_ID = "lblMainImp"

# Actual DCAD section-header structure (observed source markup):
# sections open with a span carrying this class. Ordinary spans
# (FieldTitle/FieldValue, address labels) and nested inline labels
# never qualify, and h1-h4 tags are not section signals.
_SECTION_HEADER_CLASS = "DtlSectionHdr"

_ACCOUNT_RE = re.compile(r"^[0-9]{8,24}$")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_WS_RE = re.compile(r"\s+")

# Inline labeled-field vocabulary for improvement entries (narrow,
# start-anchored, case-insensitive; matched against cleaned cell
# text). "Total Area" must not match "Foundation (Area)", "Net
# Lease Area", or "Land Area", and "Desc" must not match
# "Description".
_DESC_LABEL_RE = re.compile(r"^desc\s*:(.*)$", re.IGNORECASE)
_TOTAL_AREA_LABEL_RE = re.compile(r"^total\s+area\s*:(.*)$", re.IGNORECASE)
_YEAR_BUILT_LABEL_RE = re.compile(r"^year\s+built\s*:(.*)$", re.IGNORECASE)

# Description keyword buckets for scope transparency only. Buckets
# never become use facts; they only show whether descriptions span
# more than the claim scope.
_DESC_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("warehouse", ("warehouse", "storage", "distribution", "industrial")),
    ("office", ("office",)),
    ("retail", ("retail", "commercial", "store", "restaurant")),
)
_CLAIM_BUCKET = {
    "warehouse_industrial": "warehouse",
    "commercial_property": None,  # broad claim; any single bucket is in-scope
    "high_rise": None,
}


class DcadError(ValueError):
    """DCAD collection, parsing, or admission refused (safe to report)."""


class DcadConfigError(DcadError):
    """Collection not enabled or account not allowlisted."""


class DcadFetchError(DcadError):
    """Transport, status, redirect, size, or type refusal (nothing kept)."""


class DcadParseError(DcadError):
    """Missing, mismatched, or ambiguous record structure (fail closed)."""


def collection_enabled() -> bool:
    """True only when the owner explicitly enables DCAD collection."""
    return os.getenv(DCAD_ENABLED_ENV, "").strip() == "1"


def allowed_accounts() -> frozenset[str]:
    """Owner-allowlisted DCAD account ids (empty means none)."""
    raw = os.getenv(DCAD_ACCOUNTS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def dcad_account_url(account: str) -> str:
    """Server-constructed page URL for one validated account id."""
    cleaned = str(account or "").strip()
    if not _ACCOUNT_RE.match(cleaned):
        raise DcadConfigError("account id must be 8-24 digits")
    return f"https://{DCAD_HOST}{DCAD_PATH}?ID={cleaned}"


def require_collection_allowed(account: str) -> str:
    """Fail closed unless collection is enabled and the account is listed."""
    cleaned = str(account or "").strip()
    if not collection_enabled():
        raise DcadConfigError("DCAD collection is not enabled")
    if not _ACCOUNT_RE.match(cleaned):
        raise DcadConfigError("account id must be 8-24 digits")
    if cleaned not in allowed_accounts():
        raise DcadConfigError("account id is not allowlisted")
    return cleaned


def check_public_destination(host: str) -> None:
    """Refuse unless every resolved address is global public unicast.

    An exact hostname string is not IP pinning: a hostile or
    poisoned resolver could answer with loopback, private,
    link-local, or otherwise non-public addresses. This resolves
    the pinned host and rejects anything not allocated for public
    networks, as well as resolution failures. Residual limitation:
    resolve-then-connect leaves a TOCTOU gap (documented, not
    silently assumed away); TLS server authentication still applies
    to the connection itself.
    """
    try:
        resolved = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DcadFetchError("destination does not resolve") from exc
    if not resolved:
        raise DcadFetchError("destination does not resolve")
    for family, _socktype, _proto, _canon, sockaddr in resolved:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise DcadFetchError("destination address is not usable") from exc
        if address.is_multicast or not address.is_global:
            raise DcadFetchError("destination is not a public address")


class DcadCollection(BaseModel):
    """One controlled retrieval, minted only by :func:`collect_account`."""

    collector: str = DCAD_COLLECTOR
    collector_revision: str = DCAD_COLLECTOR_REVISION
    account: str
    url: str
    retrieved_at: str
    status_code: int
    content_type: str
    body_bytes: int
    body_sha256: str
    body_text: str
    principal: str
    collection_provenance: Literal["adapter_collected"] = "adapter_collected"


def collect_account(
    account: str, *, principal: str, timeout_seconds: float = DCAD_TIMEOUT_SECONDS
) -> DcadCollection:
    """Fetch one approved account page with fixed fail-closed limits.

    Exactly one HTTPS GET: pinned host/path/account URL, resolved
    addresses verified public beforehand, 12-second timeout, 256
    KiB cap, no redirects (any 3xx refused), no retries, no
    secondary fetches, no environment proxy (explicit direct
    connection). Certificate validation stays enabled (never
    disabled). Any refusal raises with nothing retained. Runs
    outside any business or custody transaction; the caller
    persists the result.
    """
    import httpx

    cleaned = require_collection_allowed(account)
    if not str(principal or "").strip():
        raise DcadConfigError("collection requires an authenticated principal")
    if not timeout_seconds or timeout_seconds <= 0 or timeout_seconds > DCAD_TIMEOUT_SECONDS:
        raise DcadConfigError(f"timeout must be within (0, {DCAD_TIMEOUT_SECONDS}]s")
    url = dcad_account_url(cleaned)
    check_public_destination(DCAD_HOST)
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with httpx.stream(
            "GET",
            url,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "text/html", "User-Agent": DCAD_USER_AGENT},
        ) as response:
            status = response.status_code
            if 300 <= status < 400:
                raise DcadFetchError("redirect refused")
            if status != 200:
                raise DcadFetchError(f"unexpected status {status}")
            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type.lower():
                raise DcadFetchError("unexpected content type")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > DCAD_MAX_BODY_BYTES:
                    raise DcadFetchError("response too large")
                chunks.append(chunk)
            body = b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise DcadFetchError("collection timed out") from exc
    except httpx.HTTPError as exc:
        raise DcadFetchError("collection unreachable") from exc
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DcadFetchError("response is not UTF-8 HTML") from exc
    return DcadCollection(
        account=cleaned,
        url=url,
        retrieved_at=retrieved_at,
        status_code=status,
        content_type=content_type,
        body_bytes=len(body),
        body_sha256=hashlib.sha256(body).hexdigest(),
        body_text=text,
        principal=str(principal).strip(),
    )


# --- Parser (pure, standard library) ------------------------------------------


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


class _PageModel(HTMLParser):
    """Ordered labeled texts plus table structure (no scripts/styles)."""

    _TEXT_TAGS = frozenset(
        {"span", "label", "div", "td", "th", "h1", "h2", "h3", "h4", "p", "a", "li", "b"}
    )
    _CONTAINER_TAGS = frozenset({"div", "p", "li"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[tuple[str, str, str]] = []  # (locator, tag, text) in order
        self.labeled: dict[str, list[str]] = {}
        self.tables: list[dict[str, Any]] = []  # {rows} in order
        self.heading_pos: int = -1
        self.section_positions: list[tuple[str, int]] = []  # (tag, start) in order
        self._table_stack: list[dict[str, Any]] = []
        self._row_stack: list[dict[str, Any]] = []  # {cells, start, nested}
        self._frames: list[dict[str, Any]] = []
        self._skip = 0
        self._counter = 0
        self._pos = 0

    def _locator(self, tag: str, element_id: str) -> str:
        self._counter += 1
        if element_id:
            return f"#{element_id}"
        return f"{tag}[{self._counter}]"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._pos += 1
        if tag in ("script", "style"):
            self._skip += 1
            return
        mapping = {key: (value or "") for key, value in attrs}
        if mapping.get("id") == DCAD_IMP_HEADING_ID and self.heading_pos < 0:
            self.heading_pos = self._pos
        if tag == "span" and _SECTION_HEADER_CLASS in mapping.get("class", "").split():
            self.section_positions.append((tag, self._pos))
        if tag == "table":
            # Any open row now contains a nested table, so its cells
            # carry aggregated descendant text and can never serve as
            # an entry row themselves; the inner rows parse separately.
            for open_row in self._row_stack:
                open_row["nested"] = True
            table: dict[str, Any] = {"rows": []}
            self._table_stack.append(table)
            self.tables.append(table)
            return
        if tag == "tr":
            if self._table_stack:
                row: dict[str, Any] = {
                    "cells": [],
                    "start": self._pos,
                    "nested": False,
                }
                self._table_stack[-1]["rows"].append(row)
                self._row_stack.append(row)
            return
        if tag not in self._TEXT_TAGS:
            return
        frame = {
            "tag": tag,
            "id": mapping.get("id", ""),
            "locator": self._locator(tag, mapping.get("id", "")),
            "text": "",
        }
        if tag in ("td", "th") and self._row_stack:
            cell = {
                "text": "",
                "header": tag == "th",
                "locator": frame["locator"],
            }
            self._row_stack[-1]["cells"].append(cell)
            frame["cell"] = cell
        self._frames.append(frame)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "table":
            if self._table_stack:
                self._table_stack.pop()
            return
        if tag == "tr":
            if self._row_stack:
                self._row_stack.pop()
            return
        if not self._frames or self._frames[-1]["tag"] != tag:
            return
        frame = self._frames.pop()
        raw = str(frame["text"])
        text = _clean(raw)
        if "cell" in frame:
            frame["cell"]["text"] = text
        if self._frames:
            self._frames[-1]["text"] += " " + raw
        if not text:
            return
        self.texts.append((str(frame["locator"]), str(frame["tag"]), text))
        if frame["id"]:
            self.labeled.setdefault(str(frame["id"]), []).append(text)

    def handle_data(self, data: str) -> None:
        if self._skip or not data.strip() or not self._frames:
            return
        self._frames[-1]["text"] += data


def _model_page(body_text: str) -> _PageModel:
    model = _PageModel()
    model.feed(body_text)
    model.close()
    return model


def _exactly_one_labeled(model: _PageModel, element_id: str, what: str) -> tuple[str, str]:
    """Return (text, locator) for a required unique labeled element."""
    hits = model.labeled.get(element_id, [])
    if not hits:
        raise DcadParseError(f"{what} is missing (#{element_id})")
    if len(hits) > 1:
        raise DcadParseError(f"{what} is ambiguous (#{element_id} repeats)")
    text = _clean(hits[0])
    if not text:
        raise DcadParseError(f"{what} is empty (#{element_id})")
    return text, f"#{element_id}"


def _parse_area(raw: str) -> int | None:
    cleaned = _clean(raw).replace(",", "").replace(" ", "")
    cleaned = re.sub(r"(sq\.?\s?ft\.?|sf|sqft)$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or not re.fullmatch(r"\d{1,9}", cleaned):
        return None
    return int(cleaned)


def _parse_year(raw: str) -> int | None:
    cleaned = _clean(raw)
    if re.fullmatch(r"(19|20)\d{2}", cleaned):
        return int(cleaned)
    return None


def _labeled_values(
    cells: list[dict[str, Any]], label: re.Pattern[str]
) -> list[tuple[str, str]]:
    """(value, locator) for each cell carrying one inline label."""
    hits: list[tuple[str, str]] = []
    for cell in cells:
        match = label.match(_clean(str(cell.get("text", ""))))
        if match is not None:
            hits.append(
                (_clean(match.group(1)), str(cell.get("locator", "td")))
            )
    return hits


def _entry_from_row(cells: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, bool]:
    """Build one improvement entry from a clean section row, if it is one.

    Returns (entry, skipped): rows without a Desc label are not
    entries (detail rows such as foundation/lease/depreciation, or
    header rows); rows with an empty or repeated Desc label, or
    with a repeated Total Area or Year Built label, are skipped
    rather than guessed at. Missing or unparseable Total Area and
    Year Built stay unknown with the raw value retained; values
    are never borrowed from another row or label.
    """
    descriptions = _labeled_values(cells, _DESC_LABEL_RE)
    if not descriptions:
        return None, False
    if len(descriptions) > 1:
        return None, True
    description, locator = descriptions[0]
    if not description:
        return None, True
    areas = _labeled_values(cells, _TOTAL_AREA_LABEL_RE)
    if len(areas) > 1:
        return None, True
    years = _labeled_values(cells, _YEAR_BUILT_LABEL_RE)
    if len(years) > 1:
        return None, True
    area_raw = areas[0][0] if areas else ""
    year_raw = years[0][0] if years else ""
    return (
        {
            "description_raw": description,
            "area_raw": area_raw,
            "area_sqft": _parse_area(area_raw),
            "year_built_raw": year_raw,
            "year_built": _parse_year(year_raw),
            "locator": locator,
        },
        False,
    )


def parse_account_page(body_text: str, *, expected_account: str) -> dict[str, Any]:
    """Parse one account page into labeled statements (pure, fail-closed).

    Requires the requested account as a standalone token inside
    the unique account title (an occurrence in a nearby link,
    unlabeled text, or any other element does not bind identity),
    the confirmed address label exactly once, and the confirmed
    improvements heading exactly once. Improvement entries are
    the rows inside that section (after the heading, before the
    next DCAD section header) whose cells carry the inline Desc label; each
    entry keeps its description, Total Area, and Year Built from
    that same row. Rows containing a nested table are never
    entries themselves, so ancestor aggregation cannot duplicate
    them; unrelated matching rows outside the section are
    ignored. Foundation, lease, and land areas never match the
    Total Area label. Empty or repeated Desc labels and repeated
    Total Area / Year Built labels skip the row (counted);
    missing or unparseable areas and years stay unknown (None)
    with the raw value retained. Period labels are literal
    heading/label texts, never dates.

    The account title, heading, inline labeled-row structure,
    and span.DtlSectionHdr section boundary follow observed
    source markup; surrounding page layout beyond those anchors
    remains representative, so a structural mismatch fails
    closed here rather than guessing.
    """
    if not str(body_text or ""):
        raise DcadParseError("empty response body")
    model = _model_page(str(body_text))
    title_text, account_locator = _exactly_one_labeled(
        model, DCAD_ACCOUNT_TITLE_ID, "account title"
    )
    token = re.compile(r"(?<![0-9])" + re.escape(expected_account) + r"(?![0-9])")
    if not token.search(title_text):
        raise DcadParseError(
            "requested account is not bound to the account title "
            f"(#{DCAD_ACCOUNT_TITLE_ID})"
        )
    address_raw, address_locator = _exactly_one_labeled(
        model, DCAD_ADDR_LABEL_ID, "source address"
    )
    heading_text, heading_locator = _exactly_one_labeled(
        model, DCAD_IMP_HEADING_ID, "improvements heading"
    )
    if model.heading_pos < 0:
        raise DcadParseError("improvements heading has no document position")
    section_end = min(
        (pos for _tag, pos in model.section_positions if pos > model.heading_pos),
        default=None,
    )
    improvements: list[dict[str, Any]] = []
    skipped_rows = 0
    for table in model.tables:
        for row in table["rows"]:
            if row["start"] <= model.heading_pos:
                continue
            if section_end is not None and row["start"] >= section_end:
                continue
            if row.get("nested"):
                continue
            entry, skipped = _entry_from_row(row["cells"])
            if skipped:
                skipped_rows += 1
            elif entry is not None:
                improvements.append(entry)
    if not improvements:
        raise DcadParseError("improvement entries are missing or unrecognized in their section")
    period_labels: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    candidates = [(heading_locator, "span", heading_text)] + [
        (locator, tag, text)
        for locator, tag, text in model.texts
        if tag not in _PageModel._CONTAINER_TAGS
    ]
    for locator, _tag, text in candidates:
        if locator in seen_labels:
            continue
        current = re.search(r"current\s+20\d{2}", text, re.IGNORECASE)
        certified = "certified" in text.lower() and _YEAR_RE.search(text)
        heading_year = locator == heading_locator and _YEAR_RE.search(text)
        if current or certified or heading_year:
            period_labels.append({"label": text, "locator": locator})
            seen_labels.add(locator)
    return {
        "account": expected_account,
        "account_locator": account_locator,
        "address_raw": address_raw,
        "address_locator": address_locator,
        "improvements": improvements,
        "skipped_rows": skipped_rows,
        "period_labels": period_labels,
    }


# --- Admission (pure) ---------------------------------------------------------


def _description_buckets(description: str) -> list[str]:
    lowered = str(description or "").lower()
    return sorted(
        name for name, keywords in _DESC_BUCKETS if any(word in lowered for word in keywords)
    )


def _admission_scope_note(claim_scope: str, buckets: set[str]) -> str:
    if not claim_scope:
        return (
            "No recognized claim scope: improvement descriptions are "
            "descriptive only and establish no category."
        )
    expected = _CLAIM_BUCKET.get(claim_scope)
    if expected is None:
        return (
            f"Claim scope is {claim_scope}; improvement descriptions "
            f"(buckets: {sorted(buckets) or ['none']}) are descriptive "
            "only and establish no use, occupancy, or category."
        )
    if buckets == {expected}:
        return (
            f"Improvement descriptions fall in the {expected} bucket; "
            "descriptions are descriptive only and establish no present "
            "operating use, occupancy, or exclusive-use claim."
        )
    return (
        f"Claim scope is {claim_scope} but improvement descriptions span "
        f"buckets {sorted(buckets) or ['none']}; descriptions are "
        "descriptive only -- scope conflict preserved, nothing selected "
        "to favor verification."
    )


def build_dcad_voice(collection_url: str) -> VoiceInput:
    """One authenticated DCAD voice: no defining facts, no publisher.

    The record type names the source format honestly; with no use
    facts attached, this voice alone can never satisfy a category
    pathway. Provenance ``adapter_collected`` here is minted only
    through :func:`build_dcad_assessment` over a validated collection.
    """
    return VoiceInput(
        url=collection_url,
        provenance="adapter_collected",
        record_type=DCAD_RECORD_TYPE,
        defining_facts={},
    )


def cumulative_attestation(
    *,
    dossier_key: str,
    brave_voice_urls: list[str],
    dcad_voice_url: str,
    dcad_aligned: bool,
    dcad_basis: str,
    dcad_scope: str,
    basis: str,
    disputes: int,
) -> tuple[IdentityAttestation, list[dict[str, Any]]]:
    """Request attestation where only supported evidence counts aligned.

    Account/address agreement never inherits onto retained Brave
    voices: each voice carries its own alignment, and the request
    is aligned only when EVERY voice is. Retained Brave voices are
    grouped-only (Phase 2A) and therefore never aligned, so a
    cumulative request stays unaligned while recording exactly
    why. No verified outcome may depend on unsupported inherited
    alignment. Pure and deterministic.
    """
    from app.agents.gabriel.producers import phase2a_default_note

    brave_basis = phase2a_default_note(basis, disputes)
    voice_alignment: list[dict[str, Any]] = [
        {"url": url, "aligned": False, "basis": brave_basis}
        for url in brave_voice_urls
    ]
    voice_alignment.append(
        {
            "url": dcad_voice_url,
            "aligned": bool(dcad_aligned),
            "basis": f"{dcad_basis}. {dcad_scope}".strip(),
        }
    )
    aligned = bool(voice_alignment) and all(
        entry["aligned"] for entry in voice_alignment
    )
    if aligned:
        note = (
            f"Every voice aligns to {dossier_key}: DCAD account/street "
            f"agreement ({dcad_basis}). {dcad_scope}"
        )
    else:
        note = (
            f"Request-level alignment unsupported for {dossier_key}: "
            f"{len(brave_voice_urls)} retained Brave voice(s) carry "
            f"grouping only ({brave_basis}); DCAD voice carries "
            f"account/street agreement ({dcad_basis}). {dcad_scope}"
        )
    return (
        IdentityAttestation(group_key=dossier_key, aligned=aligned, note=note),
        voice_alignment,
    )


def build_dcad_assessment(
    *,
    collection: dict[str, Any],
    collection_digest: str,
    property_record: dict[str, Any],
    dossier_key: str,
    claim_scope: str,
    registry: SourceRegistry | None = None,
) -> dict[str, Any]:
    """Admit one persisted DCAD collection against one property (pure).

    Re-validates the collection end to end: provenance minted by the
    collector, collector/revision constants, digest over the retained
    bytes, exact server-constructed URL, and current owner
    allowlisting. Caller-supplied URLs, flags, uploads, or city/ZIP
    fills cannot reach this path -- there are no such parameters.

    Alignment is supported only on exact normalized street-address
    agreement, scoped to the property account / street address (never
    a building or suite). The DCAD-only policy verdict is computed
    for transparency; with no defining use facts it cannot verify.
    Deterministic: no timestamps, so identical retries replay safely.
    """
    if not isinstance(collection, dict):
        raise DcadParseError("collection must be a mapping")
    if collection.get("collection_provenance") != DCAD_PROVENANCE:
        raise DcadParseError("collection provenance is not collector-minted")
    if collection.get("collector") != DCAD_COLLECTOR:
        raise DcadParseError("unknown collector")
    if collection.get("collector_revision") != DCAD_COLLECTOR_REVISION:
        raise DcadParseError("unsupported collector revision")
    account = str(collection.get("account", "") or "")
    require_collection_allowed(account)
    body_text = collection.get("body_text", "")
    if not isinstance(body_text, str) or not body_text:
        raise DcadParseError("collection body is missing")
    digest = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    if digest != str(collection.get("body_sha256", "") or ""):
        raise DcadParseError("collection bytes do not match their digest")
    if str(collection.get("url", "") or "") != dcad_account_url(account):
        raise DcadParseError("collection URL does not match its account")
    if not str(collection.get("principal", "") or "").strip():
        raise DcadParseError("collection principal is missing")
    parsed = parse_account_page(body_text, expected_account=account)
    source_street = normalize_text(parsed["address_raw"])
    property_street = normalize_text((property_record or {}).get("street_address"))
    agreement = bool(source_street) and source_street == property_street
    if agreement:
        basis = (
            "exact normalized street-address agreement between the "
            "DCAD source address and the operational property record"
        )
    elif not property_street:
        basis = "property record carries no street address; no alignment claimed"
    elif not source_street:
        basis = "source address is empty after normalization; no alignment claimed"
    else:
        basis = (
            "source street address does not agree with the property "
            "record; no alignment claimed"
        )
    scope_limits = (
        "Property-account / street-address scope only: alignment never "
        "establishes a specific building, suite, use, occupancy, or "
        "current status."
    )
    attestation = IdentityAttestation(
        group_key=str(dossier_key or ""),
        aligned=bool(agreement),
        note=f"{basis}. {scope_limits}",
    )
    voice = build_dcad_voice(str(collection.get("url", "")))
    active = PRODUCTION_REGISTRY if registry is None else registry
    verdict = verify_category(
        VerificationRequest(
            claim=str(claim_scope or ""),
            voices=[voice],
            identity=attestation,
            registry=active,
        )
    ).model_dump(mode="json")
    improvements: list[dict[str, Any]] = []
    buckets: set[str] = set()
    for entry in parsed["improvements"]:
        entry_buckets = _description_buckets(entry["description_raw"])
        buckets.update(entry_buckets)
        improvements.append({**entry, "buckets": entry_buckets})
    return {
        "assessment_version": DCAD_ASSESSMENT_VERSION,
        "collector": DCAD_COLLECTOR,
        "collector_revision": DCAD_COLLECTOR_REVISION,
        "property_id": str((property_record or {}).get("id", "") or ""),
        "dossier_key": str(dossier_key or ""),
        "account": account,
        "collection_digest": str(collection_digest or ""),
        "claim_scope": str(claim_scope or ""),
        "address": {
            "source_raw": parsed["address_raw"],
            "locator": parsed["address_locator"],
            "agreement": bool(agreement),
        },
        "improvements": improvements,
        "skipped_rows": parsed["skipped_rows"],
        "period_labels": parsed["period_labels"],
        "alignment": {
            "aligned": bool(agreement),
            "basis": basis,
            "scope": scope_limits,
        },
        "scope_note": _admission_scope_note(str(claim_scope or ""), buckets),
        "dcad_only_verification": verdict,
    }


__all__ = [
    "DCAD_ACCOUNTS_ENV",
    "DCAD_ASSESSMENT_VERSION",
    "DCAD_COLLECTOR",
    "DCAD_COLLECTOR_REVISION",
    "DCAD_ENABLED_ENV",
    "DCAD_HOST",
    "DCAD_MAX_BODY_BYTES",
    "DCAD_PATH",
    "DCAD_PROVENANCE",
    "DCAD_RECORD_TYPE",
    "DCAD_TIMEOUT_SECONDS",
    "DcadCollection",
    "DcadConfigError",
    "DcadError",
    "DcadFetchError",
    "DcadParseError",
    "allowed_accounts",
    "build_dcad_assessment",
    "build_dcad_voice",
    "collect_account",
    "collection_enabled",
    "cumulative_attestation",
    "dcad_account_url",
    "parse_account_page",
    "require_collection_allowed",
]
