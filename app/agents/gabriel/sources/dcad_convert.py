"""Convert official DCAD bulk archives into the canonical exchange.

Reads the two owner-approved DCAD downloads in their real published
layouts (verified 2026-11-02 against the acquired archives):

* ``PARCEL_GEOM`` shapefile set — 697,788 parcel polygons with a DBF
  carrying ``Acct`` (account id, text, leading zeros significant),
  in NAD83 StatePlane Texas North Central (FIPS 4202, US survey
  feet; Lambert Conformal Conic). Centroids are computed from the
  polygon geometry and inverse-projected to WGS84 here; no GIS
  dependency is introduced.
* ``ACCOUNT_INFO.CSV`` — account/situs records (``ACCOUNT_NUM``,
  ``STREET_NUM``/``FULL_STREET_NAME``, ``PROPERTY_CITY``,
  ``PROPERTY_ZIPCODE``, ``GIS_PARCEL_ID``, ``DIVISION_CD``).
* ``ACCOUNT_APPRL_YEAR.CSV`` — appraisal values and classifications
  (``TOT_VAL``, ``APPRAISAL_YR``, ``SPTD_CODE``, ``DIVISION_CD``).
* ``COM_DETAIL.CSV`` — commercial improvements, one-to-many per
  account (``BLDG_CLASS_DESC``, ``GROSS_BLDG_AREA``, ``YEAR_BUILT``,
  ``NUM_STORIES``).

Outputs the exchange consumed by :mod:`dcad_bulk`:
``dcad_accounts.csv`` + ``dcad_parcels.csv``. Account identifiers
stay strings end to end. Malformed records are counted and skipped,
never repaired by fabrication. Appraisal classifications remain
claims for the shared verification policy.

Run: ``python -m app.agents.gabriel.sources.dcad_convert IN OUT``
"""

from __future__ import annotations

import csv
import math
import struct
import sys
from pathlib import Path
from typing import Iterator

# --- Lambert Conformal Conic (2SP) for EPSG:2275 ---------------------------
# NAD83 / Texas North Central (ftUS), per the archive's PARCEL_GEOM.prj.
_A = 6_378_137.0
_F = 1.0 / 298.257222101
_E = math.sqrt(_F * (2.0 - _F))
_FOOT_M = 0.3048006096012192
_PHI1 = math.radians(32.13333333333333)
_PHI2 = math.radians(33.96666666666667)
_PHI0 = math.radians(31.66666666666667)
_LAM0 = math.radians(-98.5)
_FE_M = 1_968_500.0 * _FOOT_M
_FN_M = 6_561_666.666666666 * _FOOT_M


def _m(phi: float) -> float:
    s = math.sin(phi)
    return math.cos(phi) / math.sqrt(1.0 - _E * _E * s * s)


def _t(phi: float) -> float:
    s = math.sin(phi)
    return math.tan(math.pi / 4.0 - phi / 2.0) / (
        (1.0 - _E * s) / (1.0 + _E * s)
    ) ** (_E / 2.0)


_N = (
    math.log(_m(_PHI1) / _m(_PHI2))
    / math.log(_t(_PHI1) / _t(_PHI2))
)
_FC = _m(_PHI1) / (_N * _t(_PHI1) ** _N)
_RHO0 = _A * _FC * _t(_PHI0) ** _N


def lcc_forward(lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """WGS84 degrees -> StatePlane meters (for tests/diagnostics)."""
    phi, lam = math.radians(lat_deg), math.radians(lon_deg)
    rho = _A * _FC * _t(phi) ** _N
    theta = _N * (lam - _LAM0)
    return (_FE_M + rho * math.sin(theta), _FN_M + _RHO0 - rho * math.cos(theta))


def lcc_inverse(x_m: float, y_m: float) -> tuple[float, float]:
    """StatePlane meters -> (latitude, longitude) in WGS84 degrees."""
    dx, dy = x_m - _FE_M, _RHO0 - (y_m - _FN_M)
    rho = math.hypot(dx, dy) * (1 if _N > 0 else -1)
    theta = math.atan2(dx, dy)
    t = (rho / (_A * _FC)) ** (1.0 / _N)
    lam = _LAM0 + theta / _N
    phi = math.pi / 2.0 - 2.0 * math.atan(t)
    for _ in range(10):
        s = math.sin(phi)
        phi_next = math.pi / 2.0 - 2.0 * math.atan(
            t * ((1.0 - _E * s) / (1.0 + _E * s)) ** (_E / 2.0)
        )
        if abs(phi_next - phi) < 1e-12:
            phi = phi_next
            break
        phi = phi_next
    return (math.degrees(phi), math.degrees(lam))


# --- Minimal DBF / SHP readers (standard library) ---------------------------


def read_dbf(path: Path) -> Iterator[dict[str, str]]:
    """Yield DBF records as stripped string dicts (dBase III layout)."""
    with path.open("rb") as handle:
        data = handle.read()
    num_records, header_len, record_len = struct.unpack_from("<IHH", data, 4)
    fields: list[tuple[str, int]] = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\x00")[0].decode("ascii")
        fields.append((name, data[pos + 16]))
        pos += 32
    for index in range(num_records):
        start = header_len + index * record_len
        record = data[start:start + record_len]
        if len(record) < record_len or record[0:1] == b"*":
            continue
        row: dict[str, str] = {}
        offset = 1
        for name, length in fields:
            row[name] = record[offset:offset + length].decode(
                "ascii", errors="replace").strip()
            offset += length
        yield row


def _ring_centroid(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Signed area and centroid of one ring (shoelace)."""
    area2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-9:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
    return (cx / (3.0 * area2), cy / (3.0 * area2), area2 / 2.0)


def shp_centroids(path: Path) -> Iterator[tuple[float, float] | None]:
    """Yield one (x, y) centroid per SHP record, in record order.

    Coordinates are in the shapefile's projected units (feet here).
    Null/unsupported shapes yield None. Polygon centroid combines
    signed ring areas, so holes subtract correctly.
    """
    with path.open("rb") as handle:
        data = handle.read()
    offset = 100
    total = len(data)
    while offset + 8 <= total:
        _rec_no, content_words = struct.unpack_from(">2i", data, offset)
        content = data[offset + 8: offset + 8 + content_words * 2]
        offset += 8 + content_words * 2
        if len(content) < 4:
            yield None
            continue
        shape_type = struct.unpack_from("<i", content, 0)[0]
        if shape_type == 1:  # Point
            x, y = struct.unpack_from("<2d", content, 4)
            yield (x, y)
            continue
        if shape_type != 5:  # not Polygon
            yield None
            continue
        num_parts, num_points = struct.unpack_from("<2i", content, 36)
        parts = struct.unpack_from(f"<{num_parts}i", content, 44)
        points_at = 44 + 4 * num_parts
        flat = struct.unpack_from(f"<{2 * num_points}d", content, points_at)
        points = [(flat[2 * i], flat[2 * i + 1]) for i in range(num_points)]
        bounds = list(parts) + [num_points]
        cx = cy = area_sum = 0.0
        for start, end in zip(bounds, bounds[1:]):
            ring = points[start:end]
            if len(ring) < 3:
                continue
            rx, ry, area = _ring_centroid(ring)
            cx += rx * area
            cy += ry * area
            area_sum += area
        if abs(area_sum) < 1e-9:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            yield ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
        else:
            yield (cx / area_sum, cy / area_sum)


# --- Conversion ---------------------------------------------------------------


def _num(value: str | None) -> float:
    try:
        return float(str(value or "").strip() or 0)
    except ValueError:
        return 0.0


def convert(extracted_dir: Path, out_dir: Path) -> dict[str, int]:
    """Convert extracted official files into the canonical exchange."""
    extracted_dir, out_dir = Path(extracted_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {"parcels": 0, "parcels_rejected": 0, "accounts": 0,
             "accounts_rejected": 0, "accounts_duplicate": 0,
             "commercial_details": 0, "joined_coordinates": 0}

    accounts_dbf = [
        row.get("Acct", "") for row in read_dbf(extracted_dir / "PARCEL_GEOM.dbf")
    ]
    with (out_dir / "dcad_parcels.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["account", "latitude", "longitude"])
        seen_parcels: set[str] = set()
        for account, centroid in zip(
            accounts_dbf, shp_centroids(extracted_dir / "PARCEL_GEOM.shp")
        ):
            account = (account or "").strip()
            if not account or centroid is None or account in seen_parcels:
                stats["parcels_rejected"] += 1
                continue
            seen_parcels.add(account)
            lat, lon = lcc_inverse(
                centroid[0] * _FOOT_M, centroid[1] * _FOOT_M)
            if not (31.5 < lat < 33.5 and -98.0 < lon < -96.0):
                stats["parcels_rejected"] += 1
                continue
            writer.writerow([account, f"{lat:.7f}", f"{lon:.7f}"])
            stats["parcels"] += 1

    appraisal: dict[str, dict[str, str]] = {}
    with (extracted_dir / "ACCOUNT_APPRL_YEAR.CSV").open(
        newline="", encoding="utf-8-sig", errors="replace"
    ) as handle:
        for row in csv.DictReader(handle):
            account = (row.get("ACCOUNT_NUM") or "").strip()
            if account and account not in appraisal:
                appraisal[account] = row

    commercial: dict[str, dict[str, str]] = {}
    with (extracted_dir / "COM_DETAIL.CSV").open(
        newline="", encoding="utf-8-sig", errors="replace"
    ) as handle:
        for row in csv.DictReader(handle):
            account = (row.get("ACCOUNT_NUM") or "").strip()
            if not account:
                continue
            stats["commercial_details"] += 1
            current = commercial.get(account)
            if current is None or _num(row.get("GROSS_BLDG_AREA")) > _num(
                current.get("GROSS_BLDG_AREA")
            ):
                commercial[account] = row

    with (extracted_dir / "ACCOUNT_INFO.CSV").open(
        newline="", encoding="utf-8-sig", errors="replace"
    ) as handle, (out_dir / "dcad_accounts.csv").open(
        "w", newline="", encoding="utf-8"
    ) as out:
        writer = csv.writer(out)
        writer.writerow([
            "account", "situs_address", "city", "zip_code", "land_use",
            "improvement_description", "improvement_area_sqft", "year_built",
            "latitude", "longitude", "appraised_value", "value_year_label",
        ])
        seen: set[str] = set()
        for row in csv.DictReader(handle):
            account = (row.get("ACCOUNT_NUM") or "").strip()
            street = " ".join(
                part for part in (
                    (row.get("STREET_NUM") or "").strip(),
                    (row.get("STREET_HALF_NUM") or "").strip(),
                    (row.get("FULL_STREET_NAME") or "").strip(),
                ) if part
            )
            if not account or not street:
                stats["accounts_rejected"] += 1
                continue
            if account in seen:
                stats["accounts_duplicate"] += 1
                continue
            seen.add(account)
            appr = appraisal.get(account, {})
            detail = commercial.get(account, {})
            land_use = " ".join(
                part for part in (
                    (appr.get("DIVISION_CD") or "").strip(),
                    (appr.get("SPTD_CODE") or "").strip(),
                    (detail.get("BLDG_CLASS_DESC") or "").strip(),
                ) if part
            )
            year = (appr.get("APPRAISAL_YR") or "").strip()
            writer.writerow([
                account,
                street,
                (row.get("PROPERTY_CITY") or "").strip(),
                (row.get("PROPERTY_ZIPCODE") or "").strip(),
                land_use,
                (detail.get("BLDG_CLASS_DESC") or "").strip(),
                (detail.get("GROSS_BLDG_AREA") or "").strip(),
                (detail.get("YEAR_BUILT") or "").strip(),
                "",
                "",
                (appr.get("TOT_VAL") or "").strip(),
                f"{year} Certified" if year else "",
            ])
            stats["accounts"] += 1
    stats["joined_coordinates"] = stats["parcels"]
    return stats


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: dcad_convert EXTRACTED_DIR OUT_DIR")
        return 2
    stats = convert(Path(argv[1]), Path(argv[2]))
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
