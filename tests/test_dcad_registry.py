"""DCAD registry enrollment: fact-level verification, never property
qualification. Offline fixtures are test material, not live evidence.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.agents.gabriel.source_registry import (
    PRODUCTION_REGISTRY,
    SourceClass,
)
from app.agents.gabriel.sources.dcad import (
    build_dcad_assessment,
    dcad_account_url,
)
from app.agents.gabriel.verification import (
    IdentityAttestation,
    VerificationRequest,
    VoiceInput,
    verify_category,
)
from app.agents.raphael.service import evaluate_dossier

ACCOUNT = "00000700090000000"
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "dcad_account_valid.html"
_OFFICE_ROW = (
    "<tr><td><b>Desc:</b>&nbsp; OFFICE BUILDING</td>"
    "<td><b>Total Area:</b>&nbsp; 1,440 sqft</td>"
    "<td><b>Year Built:</b>&nbsp; 1968</td></tr>"
)


@pytest.fixture()
def _dcad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("DCAD_ALLOWED_ACCOUNTS", ACCOUNT)


def _collection(body: str) -> dict[str, Any]:
    return {
        "collector": "dcad-commercial-account-html",
        "collector_revision": "dcad-1",
        "account": ACCOUNT,
        "url": dcad_account_url(ACCOUNT),
        "retrieved_at": "2026-11-02T00:00:00+00:00",
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "body_bytes": len(body.encode("utf-8")),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "body_text": body,
        "principal": "test-operator",
        "collection_provenance": "adapter_collected",
    }


def _assess(body: str, claim: str) -> dict[str, Any]:
    return build_dcad_assessment(
        collection=_collection(body),
        collection_digest=_collection(body)["body_sha256"],
        property_record={"street_address": "2323 CHALK HILL RD"},
        dossier_key="addr:2323 chalk hill rd|dallas|tx|75212",
        claim_scope=claim,
    )


def test_production_registry_enrolls_dcad_narrowly() -> None:
    entry = PRODUCTION_REGISTRY.lookup("https://www.dallascad.org/x")
    assert entry is not None
    assert entry.source_class is SourceClass.AUTHORITATIVE
    assert entry.record_types == ["commercial_account"]
    assert "NOT authority for occupancy" in entry.note
    assert "vacancy" in entry.note


def test_single_class_record_verifies_that_fact_only(_dcad_env) -> None:
    body = FIXTURE.read_text(encoding="utf-8").replace(_OFFICE_ROW, "")
    assessment = _assess(body, "warehouse_industrial")
    verdict = assessment["dcad_only_verification"]
    assert assessment["recorded_uses"] == ["warehouse"]
    assert verdict["outcome"] == "verified"
    assert verdict["pathway"] == "authority"
    assert any("category claim only" in limit
               for limit in verdict["limitations"])


def test_mixed_class_record_still_never_verifies(_dcad_env) -> None:
    assessment = _assess(FIXTURE.read_text(encoding="utf-8"),
                         "warehouse_industrial")
    verdict = assessment["dcad_only_verification"]
    assert assessment["recorded_uses"] == ["office", "warehouse"]
    assert verdict["outcome"] == "not_verified"
    assert "material_contradiction" in verdict["reasons"]


def test_unsupported_claims_never_verify() -> None:
    voice = VoiceInput(
        url=dcad_account_url(ACCOUNT),
        provenance="adapter_collected",
        record_type="commercial_account",
        defining_facts={"use": "warehouse", "use_status": "recorded"},
    )
    for claim in ("vacant", "available_for_lease", "farm_suitable"):
        verdict = verify_category(VerificationRequest(
            claim=claim,
            voices=[voice],
            identity=IdentityAttestation(group_key="k", aligned=True),
            registry=PRODUCTION_REGISTRY,
        ))
        assert verdict.outcome.value == "not_verified"


def test_verified_fact_does_not_qualify_the_property(_dcad_env) -> None:
    body = FIXTURE.read_text(encoding="utf-8").replace(_OFFICE_ROW, "")
    verdict = _assess(body, "warehouse_industrial")["dcad_only_verification"]
    assert verdict["outcome"] == "verified"
    blocked = evaluate_dossier({
        "dossier": {"identity_disputes": {}, "conflicts": ["mixed-use"],
                    "category_attribution": {"status": "observed"}},
        "candidate_verification": verdict,
    })
    assert blocked["outcome"] == "needs_evidence"
    disputed = evaluate_dossier({
        "dossier": {"identity_disputes": {"suite": ["a", "b"]},
                    "conflicts": [],
                    "category_attribution": {"status": "observed"}},
        "candidate_verification": verdict,
    })
    assert disputed["outcome"] == "needs_evidence"
