"""Offline test material only: admission, complete shared flow, immutable history."""
import copy
import json
from uuid import uuid4

import pytest

from app.workflow import digest_payload
from app.workflow_db import WorkflowError, put_record_in_transaction
from tests.test_official_records import _seed, _eval, _Cursor, _Conn
from tests.test_dcad_admission import _body, _collection, _OFFICE_ROW, ACCOUNT


class Cursor(_Cursor):
    def execute(self, sql, params=None):
        p = dict(params or {})
        if sql.startswith("SELECT * FROM workflow_runs WHERE idempotency_key"):
            self._result = [dict(r) for r in self._db["runs"] if r["idempotency_key"] == p["key"]]
        elif sql.startswith("INSERT INTO workflow_runs"):
            row = {"run_id": p["run_id"], "idempotency_key": p["key"], "stage": p["stage"],
                   "status": "open", "correlation_id": p["correlation"],
                   "limits": json.loads(p["limits"]), "usage": {}, "errors": []}
            self._db["runs"].append(row)
            self._result = [dict(row)]
        else:
            super().execute(sql, params)


class Conn(_Conn):
    def cursor(self):
        return Cursor(self._db)


def seed(monkeypatch, mixed=False):
    db, _, parent, prop = _seed(monkeypatch)
    conn = Conn(db)
    _eval(conn, parent, prop)
    collection_run = uuid4()
    account = "00000100324000000"
    body = _body().replace(ACCOUNT, account).replace("2323 CHALK HILL RD", "801 JACKSON ST")
    if not mixed:
        body = body.replace(_OFFICE_ROW, "")
    from app.agents.gabriel.sources.dcad import dcad_account_url
    collection = _collection(body, account=account, url=dcad_account_url(account))
    claim = {"account": account, "principal": collection["principal"],
             "collector_revision": "dcad-1", "state": "started"}
    for kind, key, payload in (("dcad_collection", "stored-page", collection),
                               ("dcad_claim", f"dcad:{account}:claim", claim)):
        put_record_in_transaction(conn, collection_run, kind=kind, record_key=key,
                                  version=1, payload=payload, digest=digest_payload(payload))
    assessment = {"property_id":str(prop), "dossier_key":"main", "account":account,
                  "collection_digest":digest_payload(collection)}
    put_record_in_transaction(conn, collection_run, kind="dcad_assessment", record_key="original-assessment",
                              version=1, payload=assessment, digest=digest_payload(assessment))
    monkeypatch.setenv("CUSTODY_AMENDERS", "owner")
    monkeypatch.setenv("DCAD_COLLECTION_ENABLED", "0")
    monkeypatch.delenv("DCAD_ALLOWED_ACCOUNTS", raising=False)
    args = dict(parent_run_id=str(parent), dossier_key="main", collection_run_id=str(collection_run))
    return db, conn, args


def test_preview_reuses_authenticated_bytes_without_acquisition(monkeypatch):
    from app.retained_assessment import preview_in_transaction
    db, conn, args = seed(monkeypatch)
    before = copy.deepcopy(db)
    preview = preview_in_transaction(conn, **args)
    assert preview["decision"]["outcome"] == "authorized"
    assert preview["provider_requests"] == 0
    assert preview["dossier"]["version"] == 1
    assert preview["inspection_preview"]["facts"]["actual_vacancy"]["classification"] == "unknown"
    assert db == before


def test_commit_same_dossier_through_michael_and_replay(monkeypatch):
    from app.retained_assessment import preview_in_transaction, commit_in_transaction
    from app.custody_db import verify_chain_in_transaction, current_custody_in_transaction
    from app.custody import dossier_id_for_property
    db, conn, args = seed(monkeypatch)
    original_records = copy.deepcopy(db["records"])
    original_versions = copy.deepcopy(db["versions"])
    preview = preview_in_transaction(conn, **args)
    result = commit_in_transaction(conn, principal="owner", reason="offline reassessment",
                                   preview_digest=preview["preview_digest"], **args)
    assert result["decision"] == "authorized" and result["current_holder"] == "michael"
    assert "package" not in result
    assert not any(a["complete"] for a in result["financial_assessments"].values())
    assert db["records"][:len(original_records)] == original_records
    assert db["versions"][:len(original_versions)] == original_versions
    newest = db["versions"][-1]
    reports = newest["payload"]["dossier"]["reports"]
    assert set(reports) == {"raphael", "onesimus", "michael"}
    assert newest["payload"]["dossier"]["report_history"]
    did = dossier_id_for_property(args_property(db))
    assert verify_chain_in_transaction(conn, did) >= 5
    assert current_custody_in_transaction(conn, did)["holder"] == "michael"
    after = copy.deepcopy(db)
    assert commit_in_transaction(conn, principal="owner", reason="offline reassessment",
                                 preview_digest=preview["preview_digest"], **args) == result
    assert db == after


def args_property(db):
    return db["property"]["id"]


@pytest.mark.parametrize("mutation", ["claim", "bytes", "identity", "preview", "permission", "property_binding"])
def test_refuses_bad_bindings_before_write(monkeypatch, mutation):
    from app.retained_assessment import preview_in_transaction, commit_in_transaction
    from app.custody import CustodyAuthError
    db, conn, args = seed(monkeypatch)
    preview = preview_in_transaction(conn, **args)
    if mutation in ("claim", "bytes"):
        row = next(r for r in db["records"] if r["kind"] == ("dcad_claim" if mutation == "claim" else "dcad_collection"))
        row["payload"]["principal" if mutation == "claim" else "body_text"] = "tampered"
        row["digest"] = digest_payload(row["payload"])
    elif mutation == "property_binding":
        row = next(r for r in db["records"] if r["kind"]=="dcad_assessment")
        row["payload"]["property_id"] = str(uuid4())
        row["digest"] = digest_payload(row["payload"])
    elif mutation == "identity":
        db["property"]["street_address"] = "99 Other St"
    elif mutation == "preview":
        args["site_inputs"] = {"usable_area_m2": {"value": 20, "unit": "m2", "classification": "assumed"}}
    else:
        monkeypatch.setenv("CUSTODY_AMENDERS", "somebody-else")
    before = copy.deepcopy(db)
    with pytest.raises((WorkflowError, ValueError, CustodyAuthError)):
        commit_in_transaction(conn, principal="owner", reason="offline reassessment",
                              preview_digest=preview["preview_digest"], **args)
    assert db == before


def test_mixed_record_remains_needs_evidence(monkeypatch):
    from app.retained_assessment import preview_in_transaction
    db, conn, args = seed(monkeypatch, mixed=True)
    preview = preview_in_transaction(conn, **args)
    assert preview["decision"]["outcome"] != "authorized"
    assert preview["assessment"]["dcad_only_verification"]["outcome"] != "verified"


def test_second_reassessment_retains_original_artifact_lineage(monkeypatch):
    from app.retained_assessment import preview_in_transaction, commit_in_transaction
    db, conn, args = seed(monkeypatch)
    first = preview_in_transaction(conn, **args)
    outcome = commit_in_transaction(conn, principal="owner", reason="first",
                                    preview_digest=first["preview_digest"], **args)
    args["parent_run_id"] = outcome["run_id"]
    second = preview_in_transaction(conn, **args)
    assert second["dossier"]["retained_evidence_lineage"] == first["dossier"]["retained_evidence_lineage"]
    assert second["decision"]["outcome"] == "authorized"
    again = commit_in_transaction(conn, principal="owner", reason="second",
                                  preview_digest=second["preview_digest"], **args)
    assert again["current_holder"] == "michael"


def test_financial_inputs_cannot_override_inspected_geometry(monkeypatch):
    from app.retained_assessment import preview_in_transaction
    db, conn, args = seed(monkeypatch)
    with pytest.raises(WorkflowError, match="unknown financial input"):
        preview_in_transaction(conn, **args, financial_inputs={"room_width_m": {"value": 900}})


def test_complete_input_bundle_reaches_finance_without_outreach(monkeypatch):
    from app.retained_assessment import preview_in_transaction, commit_in_transaction
    from app.michael import FINANCIAL_NAMES
    from tests.test_funnel_acceptance import _site_inputs
    db, conn, args = seed(monkeypatch)
    supplied = _site_inputs()
    args["site_inputs"] = {k:v for k,v in supplied.items() if k not in FINANCIAL_NAMES}
    args["financial_inputs"] = {k:v for k,v in supplied.items() if k in FINANCIAL_NAMES}
    preview = preview_in_transaction(conn, **args)
    outcome = commit_in_transaction(conn, principal="owner", reason="conditional inputs",
                                    preview_digest=preview["preview_digest"], **args)
    assert all(a["complete"] for a in outcome["financial_assessments"].values())
    assert outcome["current_holder"] == "michael" and "package" not in outcome
