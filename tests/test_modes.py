"""DEMO_MODE parsing and upstream mode-invariance acceptance.

DEMO_MODE governs Michael-stage and later external behavior only.
Gabriel, Raphael, and Onesimus must behave identically in both modes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modes import demo_mode_enabled


def test_demo_mode_defaults_to_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert demo_mode_enabled() is True
    monkeypatch.setenv("DEMO_MODE", "")
    assert demo_mode_enabled() is True
    monkeypatch.setenv("DEMO_MODE", "yes")
    assert demo_mode_enabled() is True


def test_demo_mode_no_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "no")
    assert demo_mode_enabled() is False
    monkeypatch.setenv("DEMO_MODE", " No ")
    assert demo_mode_enabled() is False


def test_demo_mode_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("1", "0", "true", "false", "demo", "YESPLEASE", "nope"):
        monkeypatch.setenv("DEMO_MODE", raw)
        with pytest.raises(ValueError, match="DEMO_MODE"):
            demo_mode_enabled()


def _upstream_fixture_results() -> dict[str, object]:
    """Run one pure Gabriel/Raphael/Onesimus evaluation each."""
    from app.agents.gabriel.attribution import (
        CategoryObservation,
        evaluate_category_evidence,
    )
    from app.agents.onesimus.crops import crop_profile
    from app.agents.onesimus.service import ClassifiedInput, evaluate_scenario
    from app.agents.raphael.service import evaluate_dossier

    gabriel = evaluate_category_evidence(
        [
            CategoryObservation(
                value="warehouse_industrial",
                origin="listing_text",
                source_url="https://broker.example/listings/1",
            ),
            CategoryObservation(
                value="warehouse_industrial",
                origin="provider",
                source_url="https://exchange.example/p/2",
            ),
        ]
    )
    raphael = evaluate_dossier({"dossier": {"key": "k"}, "candidate_verification": None})
    crop = crop_profile()
    site = {
        "area_m2": ClassifiedInput(
            name="area_m2", value=100.0, unit="m2",
            provenance="fixture", classification="estimated",
        )
    }
    onesimus = evaluate_scenario("bench", crop, site)
    return {
        "gabriel": gabriel.model_dump(mode="json"),
        "raphael": raphael,
        "onesimus": onesimus,
    }


def test_upstream_results_identical_in_both_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "yes")
    before = _upstream_fixture_results()
    monkeypatch.setenv("DEMO_MODE", "no")
    after = _upstream_fixture_results()
    assert before == after


def test_upstream_modules_never_reference_demo_mode() -> None:
    """Static guard: Gabriel/Raphael/Onesimus cannot branch on DEMO_MODE."""
    root = Path(__file__).resolve().parent.parent / "app" / "agents"
    checked = 0
    for path in root.rglob("*.py"):
        if path.parent.name not in ("gabriel", "raphael", "onesimus"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "app.modes" not in text, path
        assert "DEMO_MODE" not in text, path
        checked += 1
    assert checked > 0
