"""B5-2A.2b-0: commercial-property category migration and downtown attribute.

Retires ``downtown_commercial`` as a canonical category in favor of
``commercial_property`` and introduces the evidence-gated ``is_downtown``
property attribute (default False; positive determination deferred).
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.gabriel import attribution, research
from app.agents.gabriel.attribution import (
    CategoryEvidenceStatus,
    CategoryObservation,
    evaluate_category_evidence,
    normalize_category_value,
)
from app.agents.gabriel.extractor import extract_properties, mentions_downtown
from app.agents.gabriel.geography import CITY_PRIORITY_ORDER, COUNTY_PRIORITY_ORDER
from app.agents.gabriel.planner import (
    PLANNED_CATEGORIES,
    PLANNER_VERSION,
    plan_initial_searches,
)
from app.agents.gabriel.schemas import BoundingBoxInput, DiscoveryRunRequest
from app.agents.gabriel.sources.overpass import map_category
from app.properties import Property, PropertyCategory, PropertyCreate
from app.properties_db import _property_insert_params

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

MIGRATION_011 = Path(__file__).resolve().parent.parent / "migrations" / "011_commercial_property_category.sql"


def _bbox() -> BoundingBoxInput:
    return BoundingBoxInput(south=32.77, west=-96.81, north=32.79, east=-96.785)


# --- Canonical enum and legacy normalization (items 1-3) ---


def test_canonical_categories_exclude_retired_member() -> None:
    values = {member.value for member in PropertyCategory}
    assert PropertyCategory.COMMERCIAL_PROPERTY.value == "commercial_property"
    assert "downtown_commercial" not in values
    assert not hasattr(PropertyCategory, "DOWNTOWN_COMMERCIAL")


def test_legacy_category_input_normalizes_to_canonical() -> None:
    created = PropertyCreate(name="Legacy Row", category="downtown_commercial")  # type: ignore[arg-type]
    assert created.category is PropertyCategory.COMMERCIAL_PROPERTY


def test_legacy_alias_accepts_variant_spellings() -> None:
    for spelling in ("Downtown Commercial", "downtown-commercial", " DOWNTOWN_COMMERCIAL "):
        created = PropertyCreate(name="Legacy Row", category=spelling)  # type: ignore[arg-type]
        assert created.category is PropertyCategory.COMMERCIAL_PROPERTY


# --- Discovery API and planner compatibility (items 4-7) ---


def test_discovery_rejects_retired_category() -> None:
    with pytest.raises(ValidationError, match="unknown discovery category"):
        DiscoveryRunRequest(bbox=_bbox(), categories=["downtown_commercial"])


def test_discovery_accepts_canonical_categories() -> None:
    request = DiscoveryRunRequest(
        bbox=_bbox(), categories=["high_rise", "commercial_property", "warehouse_industrial"]
    )
    assert request.categories == ["high_rise", "commercial_property", "warehouse_industrial"]


def test_planner_emits_new_canonical_triples() -> None:
    assert PLANNER_VERSION == "004B-2.1"
    assert list(PLANNED_CATEGORIES) == [
        PropertyCategory.HIGH_RISE,
        PropertyCategory.COMMERCIAL_PROPERTY,
        PropertyCategory.WAREHOUSE_INDUSTRIAL,
    ]
    plan = plan_initial_searches()
    assert len(plan) == 36
    for index in range(0, 36, 3):
        triple = plan[index:index + 3]
        assert [a.category for a in triple] == list(PLANNED_CATEGORIES)
        assert len({a.county for a in triple}) == 1
        assert all(a.planner_version == "004B-2.1" for a in triple)


def test_stale_planner_version_rejected() -> None:
    assignments = plan_initial_searches()
    stale = [a.model_copy(update={"planner_version": "004B-2.0"}) for a in assignments]
    with pytest.raises(research.ResearchError, match="unsupported planner version"):
        research.select_assignments(stale, 0)


def test_commercial_query_template_preserved() -> None:
    commercial = [a for a in plan_initial_searches() if a.category is PropertyCategory.COMMERCIAL_PROPERTY]
    assert commercial
    assert all("commercial office building" in a.query for a in commercial)


# --- OSM and listing-text classification (items 8-13) ---


@pytest.mark.parametrize("tag", ["commercial", "retail", "office"])
def test_osm_commercial_tags_map_to_canonical(tag: str) -> None:
    assert map_category({"building": tag}) == "commercial_property"


def test_osm_generic_building_remains_other() -> None:
    assert map_category({"building": "yes"}) == "other"


def test_osm_high_rise_and_warehouse_preserved() -> None:
    assert map_category({"building": "office", "building:levels": "12"}) == "high_rise"
    assert map_category({"building": "warehouse"}) == "warehouse_industrial"


def _single_category(title: str, snippet: str):  # type: ignore[no-untyped-def]
    props = extract_properties(title, snippet)
    assert len(props) == 1
    return props[0].category


def test_listing_office_text_classifies_commercial() -> None:
    category = _single_category(
        "Commerce Plaza", "123 Main Street, Dallas, TX. Class A office space for lease."
    )
    assert category is PropertyCategory.COMMERCIAL_PROPERTY


def test_downtown_only_text_does_not_classify_commercial() -> None:
    category = _single_category(
        "Commerce Plaza", "123 Main Street, Dallas, TX. Near downtown with downtown views."
    )
    assert category is None


def test_listing_high_rise_and_warehouse_preserved() -> None:
    assert (
        _single_category("Tower Block", "44 Elm Street, Dallas, TX. 20-story skyscraper.")
        is PropertyCategory.HIGH_RISE
    )
    assert (
        _single_category("Logistics Hub", "9 Freight Road, Dallas, TX. Distribution warehouse.")
        is PropertyCategory.WAREHOUSE_INDUSTRIAL
    )


# --- Attribution evaluation (items 4, 28) ---


def test_attribution_normalizes_legacy_reads() -> None:
    assert normalize_category_value("commercial_property") == "commercial_property"
    assert normalize_category_value("downtown_commercial") == "commercial_property"
    assert normalize_category_value("strip_mall") is None


def test_single_commercial_observation_is_not_verified() -> None:
    result = evaluate_category_evidence(
        [CategoryObservation(value="commercial_property", origin="listing_text", source_url="https://a.example/x")],
        search_category="commercial_property",
    )
    assert result.status is CategoryEvidenceStatus.OBSERVED
    assert result.verified is None
    assert result.observed == "commercial_property"


def test_search_target_alone_proves_no_category() -> None:
    result = evaluate_category_evidence([], search_category="commercial_property")
    assert result.status is CategoryEvidenceStatus.UNKNOWN
    assert result.verified is None


# --- Downtown attribute (items 14-20) ---


def _stored_row(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(timezone.utc)
    row = {
        "id": uuid4(),
        "name": "Commerce Plaza",
        "category": "commercial_property",
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


def test_is_downtown_defaults_false_on_read_model() -> None:
    assert Property(**_stored_row()).is_downtown is False


def test_is_downtown_is_not_caller_settable() -> None:
    assert "is_downtown" not in PropertyCreate.model_fields
    created = PropertyCreate(name="Plaza", category="commercial_property", is_downtown=True)  # type: ignore[call-arg]
    assert not hasattr(created, "is_downtown")
    assert "is_downtown" not in created.model_dump()


def test_property_insert_pins_is_downtown_false() -> None:
    params = _property_insert_params({"name": "Plaza", "category": "commercial_property", "is_downtown": True})
    assert params["is_downtown"] is False
    params = _property_insert_params({"name": "Plaza", "category": "commercial_property"})
    assert params["is_downtown"] is False


def test_legacy_stored_row_reads_with_false_downtown() -> None:
    record = Property(**_stored_row(category="downtown_commercial"))
    assert record.category is PropertyCategory.COMMERCIAL_PROPERTY
    assert record.is_downtown is False  # legacy category never proves downtown location


def test_downtown_mention_signal_does_not_set_attribute() -> None:
    assert mentions_downtown("Minutes from downtown with downtown views.") is True
    record = Property(**_stored_row())
    assert record.is_downtown is False
    assert "downtown" not in Property.model_fields


# --- Pilot scope preservation (items 23-27) ---


def test_pilot_geography_and_quota_arithmetic() -> None:
    assert len(COUNTY_PRIORITY_ORDER) == 12
    assert all(len(cities) == 5 for cities in CITY_PRIORITY_ORDER.values())
    assert len(CITY_PRIORITY_ORDER) == 12
    assert len(PLANNED_CATEGORIES) == 3
    assert len(plan_initial_searches()) == 36  # 12 counties x 3 categories
    assert 36 * 4 == 144  # four dossiers per county/category cell


# --- Migration safety (item 22) ---


def test_migration_011_is_additive_and_non_rewriting() -> None:
    text = MIGRATION_011.read_text(encoding="utf-8")
    assert "commercial_property" in text
    assert "'downtown_commercial'" in text  # historical compatibility retained
    assert "is_downtown" in text
    upper = text.upper()
    assert "UPDATE " not in upper
    assert "DELETE " not in upper


@needs_live_pg
def test_live_check_permits_canonical_and_legacy_with_false_default() -> None:
    from app.properties_db import get_connection

    canonical_id = uuid4()
    legacy_id = uuid4()
    # Rollback isolation: nothing committed, so no cleanup DELETE is needed.
    # The connection is managed manually because exiting a Psycopg connection
    # context with an open transaction would commit it.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO properties (id, name, category, data_status) "
                "VALUES (%(id)s, 'Catmig Canonical', 'commercial_property', 'unknown')",
                {"id": canonical_id},
            )
            cur.execute(
                "INSERT INTO properties (id, name, category, data_status) "
                "VALUES (%(id)s, 'Catmig Legacy', 'downtown_commercial', 'unknown')",
                {"id": legacy_id},
            )
            cur.execute(
                "SELECT category, is_downtown FROM properties WHERE id = %(id)s",
                {"id": canonical_id},
            )
            row = cur.fetchone()
            assert row["category"] == "commercial_property"
            assert row["is_downtown"] is False
            cur.execute(
                "SELECT category, is_downtown FROM properties WHERE id = %(id)s",
                {"id": legacy_id},
            )
            legacy = cur.fetchone()
            assert legacy["category"] == "downtown_commercial"
            assert legacy["is_downtown"] is False
    finally:
        conn.rollback()
        conn.close()
