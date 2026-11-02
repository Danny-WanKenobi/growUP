"""Offline converter tests: synthetic DBF/SHP/CSV fixtures (stdlib only)."""
from __future__ import annotations

import csv
import struct

from app.agents.gabriel.sources.dcad_convert import (
    _FOOT_M,
    convert,
    lcc_forward,
    lcc_inverse,
    read_dbf,
    shp_centroids,
)


def test_lcc_origin_and_roundtrip():
    x, y = lcc_forward(31.66666666666667, -98.5)
    assert abs(x - 1_968_500.0 * _FOOT_M) < 1.0
    assert abs(y - 6_561_666.666666666 * _FOOT_M) < 1.0
    lat, lon = lcc_inverse(*lcc_forward(32.777, -96.797))
    assert abs(lat - 32.777) < 1e-6
    assert abs(lon + 96.797) < 1e-6


def _dbf_bytes(rows):
    fields = [("Acct", 20)]
    header_len = 32 + 32 * len(fields) + 1
    record_len = 1 + sum(length for _n, length in fields)
    out = bytearray(32)
    out[0] = 0x03
    struct.pack_into("<IHH", out, 4, len(rows), header_len, record_len)
    for name, length in fields:
        desc = bytearray(32)
        desc[0:len(name)] = name.encode()
        desc[11] = ord("C")
        desc[16] = length
        out += desc
    out += b"\x0d"
    for row in rows:
        record = bytearray(b" ")
        record += row["Acct"].ljust(20).encode()[:20]
        out += record
    return bytes(out)


def _shp_bytes(rings):
    records = bytearray()
    for number, ring in enumerate(rings, start=1):
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        content = struct.pack("<i4d2i", 5, min(xs), min(ys), max(xs), max(ys),
                              1, len(ring))
        content += struct.pack("<i", 0)
        for x, y in ring:
            content += struct.pack("<2d", x, y)
        records += struct.pack(">2i", number, len(content) // 2) + content
    header = bytearray(100)
    struct.pack_into(">i", header, 0, 9994)
    struct.pack_into(">i", header, 24, (100 + len(records)) // 2)
    struct.pack_into("<2i", header, 28, 1000, 5)
    return bytes(header) + bytes(records)


def _square(cx, cy, half=100.0):
    return [(cx - half, cy - half), (cx + half, cy - half),
            (cx + half, cy + half), (cx - half, cy + half),
            (cx - half, cy - half)]


def test_dbf_and_shp_readers(tmp_path):
    (tmp_path / "x.dbf").write_bytes(
        _dbf_bytes([{"Acct": "00000700090000000"}, {"Acct": "123"}]))
    rows = list(read_dbf(tmp_path / "x.dbf"))
    assert [r["Acct"] for r in rows] == ["00000700090000000", "123"]
    (tmp_path / "x.shp").write_bytes(
        _shp_bytes([_square(1000.0, 2000.0), _square(5000.0, 6000.0)]))
    centroids = list(shp_centroids(tmp_path / "x.shp"))
    assert centroids[0] == (1000.0, 2000.0)
    assert centroids[1] == (5000.0, 6000.0)


def test_convert_end_to_end_preserves_leading_zeros(tmp_path):
    extracted, out = tmp_path / "in", tmp_path / "out"
    extracted.mkdir()
    account = "00000700090000000"
    (extracted / "PARCEL_GEOM.dbf").write_bytes(_dbf_bytes([{"Acct": account}]))
    cx_m, cy_m = lcc_forward(32.78, -96.80)
    ring = _square(cx_m / _FOOT_M, cy_m / _FOOT_M)
    (extracted / "PARCEL_GEOM.shp").write_bytes(_shp_bytes([ring]))
    (extracted / "ACCOUNT_INFO.CSV").write_text(
        "ACCOUNT_NUM,STREET_NUM,STREET_HALF_NUM,FULL_STREET_NAME,"
        "PROPERTY_CITY,PROPERTY_ZIPCODE,APPRAISAL_YR\n"
        f"{account},2323,,CHALK HILL RD,DALLAS,75212,2026\n"
        f"{account},2323,,CHALK HILL RD,DALLAS,75212,2026\n"
        ",9,,NOWHERE ST,DALLAS,75201,2026\n",
        encoding="utf-8")
    (extracted / "ACCOUNT_APPRL_YEAR.CSV").write_text(
        "ACCOUNT_NUM,APPRAISAL_YR,TOT_VAL,DIVISION_CD,SPTD_CODE\n"
        f"{account},2026,670000,COM,F1\n",
        encoding="utf-8")
    (extracted / "COM_DETAIL.CSV").write_text(
        "ACCOUNT_NUM,BLDG_CLASS_DESC,GROSS_BLDG_AREA,YEAR_BUILT\n"
        f"{account},STORAGE WAREHOUSE,19440,1968\n"
        f"{account},OFFICE BUILDING,1440,1968\n",
        encoding="utf-8")
    stats = convert(extracted, out)
    assert stats["accounts"] == 1
    assert stats["accounts_duplicate"] == 1
    assert stats["accounts_rejected"] == 1
    accounts = list(csv.DictReader((out / "dcad_accounts.csv").open()))
    assert accounts[0]["account"] == account
    assert accounts[0]["situs_address"] == "2323 CHALK HILL RD"
    assert accounts[0]["improvement_description"] == "STORAGE WAREHOUSE"
    assert accounts[0]["value_year_label"] == "2026 Certified"
    parcels = list(csv.DictReader((out / "dcad_parcels.csv").open()))
    assert parcels[0]["account"] == account
    assert abs(float(parcels[0]["latitude"]) - 32.78) < 1e-4
    assert abs(float(parcels[0]["longitude"]) + 96.80) < 1e-4
