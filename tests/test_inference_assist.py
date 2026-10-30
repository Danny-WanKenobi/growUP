"""Shared Baseten/Meta inference: budget, capabilities, Wondrue wiring.

All provider HTTP is mocked; all database access uses in-memory fakes
(budget ledger + workflow/package reads). No live calls, no real
database, no ambient configuration dependence.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.model_adapter import ModelError
from app.wondrue import WondrueError, handle_command

# Magic bytes plus padding: validation detects container format only and
# never claims to verify decodability.
_MINIMAL_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_MINIMAL_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


# --- Budget-ledger fake -------------------------------------------------------


class _BudgetCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self._result: list[Any] = []

    def __enter__(self) -> _BudgetCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def execute(self, sql: str, params: Any = None) -> None:
        p = dict(params or {})
        db = self._db
        with db["lock"]:
            state = db["allowance"]
            if sql.startswith("INSERT INTO inference_allowance"):
                if "DO UPDATE" in sql:
                    state["allowance"] = int(p["allowance"])
                    state["demo_reserve"] = int(p["reserve"])
                self._result = []
            elif sql.startswith("SELECT allowance_units, used_units"):
                self._result = [{
                    "allowance_units": state["allowance"],
                    "used_units": state["used"],
                    "demo_reserve_units": state["demo_reserve"],
                }]
            elif sql.startswith("SELECT used_units FROM inference_allowance"):
                self._result = [{"used_units": state["used"]}]
            elif sql.startswith("UPDATE inference_allowance SET used_units = used_units +") and "excess" in p:
                state["used"] += int(p["excess"])
                self._result = []
            elif sql.startswith("UPDATE inference_allowance SET used_units = GREATEST"):
                state["used"] = max(0, state["used"] - int(p["reserved"]))
                self._result = []
            elif sql.startswith("UPDATE inference_allowance SET used_units"):
                if state["used"] + int(p["units"]) <= int(p["ceiling"]):
                    state["used"] += int(p["units"])
                    self._result = [{"used_units": state["used"]}]
                else:
                    self._result = []
            elif sql.startswith("SELECT agent, capability, COUNT(*)"):
                grouped: dict[tuple[str, str], dict[str, int]] = {}
                for row in db["rows"].values():
                    if not row.get("live"):
                        continue
                    key = (str(row.get("agent", "")), str(row.get("capability", "")))
                    entry = grouped.setdefault(
                        key, {"calls": 0, "consumed": 0, "released": 0}
                    )
                    entry["calls"] += 1
                    if row.get("outcome") == "released":
                        entry["released"] += int(row.get("reserved_units", 0) or 0)
                    else:
                        entry["consumed"] += int(row.get("reserved_units", 0) or 0)
                        entry["consumed"] += int(row.get("excess_units", 0) or 0)
                self._result = [
                    {"agent": a, "capability": c, "calls": v["calls"],
                     "consumed": v["consumed"], "released": v["released"]}
                    for (a, c), v in sorted(grouped.items())
                ]
            elif sql.startswith("INSERT INTO inference_usage"):
                if p.get("idempotency_key"):
                    for row in db["rows"].values():
                        if row.get("idempotency_key") == p["idempotency_key"]:
                            raise Exception(
                                'duplicate key value violates unique constraint "inference_usage_idempotency_key_key"'
                            )
                db["rows"][str(p["request_id"])] = {
                    "request_id": str(p["request_id"]),
                    "idempotency_key": p.get("idempotency_key"),
                    "principal": p.get("principal", ""),
                    "agent": p.get("agent", ""),
                    "capability": p.get("capability", ""),
                    "dossier_id": p.get("dossier_id"),
                    "dossier_version": p.get("dossier_version"),
                    "run_id": p.get("run_id"),
                    "package_entry": p.get("package_entry"),
                    "provider": p.get("provider", ""),
                    "model": p.get("model", ""),
                    "prompt_version": p.get("prompt_version", ""),
                    "schema_version": p.get("schema_version", ""),
                    "request_digest": p.get("digest", ""),
                    "requested_max_tokens": p.get("max_tokens", 0),
                    "bound_input_units": p.get("bound", 0),
                    "reserved_units": p.get("reserved", 0),
                    "excess_units": 0,
                    "outcome": "reserved",
                    "live": p.get("live", True),
                    "replay_of": p.get("replay_of"),
                    "reported_input_tokens": None,
                    "reported_output_tokens": None,
                    "reported_total_tokens": None,
                    "estimated_cost_usd": None,
                    "provider_request_id": "",
                    "error": "",
                    "result": None,
                }
                self._result = []
            elif sql.startswith("SELECT request_id, principal, agent"):
                found = next(
                    (row for row in db["rows"].values()
                     if row.get("idempotency_key") == p["key"]),
                    None,
                )
                self._result = [dict(found)] if found else []
            elif sql.startswith("UPDATE inference_usage SET outcome = 'released'"):
                row = db["rows"].get(str(p["request_id"]))
                assert row is not None, "release of unknown request"
                if row["outcome"] != "reserved":
                    self._result = []
                else:
                    row["outcome"] = "released"
                    self._result = [{"reserved_units": row["reserved_units"]}]
            elif sql.startswith("UPDATE inference_usage SET outcome"):
                row = db["rows"].get(str(p["request_id"]))
                assert row is not None, "reconcile of unknown request"
                if row["outcome"] != "reserved":
                    self._result = []  # idempotent: already reconciled
                else:
                    row["outcome"] = p["outcome"]
                    row["reported_input_tokens"] = p["in_tok"]
                    row["reported_output_tokens"] = p["out_tok"]
                    row["reported_total_tokens"] = p["total"]
                    row["estimated_cost_usd"] = p["cost"]
                    row["provider_request_id"] = p["provider_ref"]
                    row["error"] = p["error"]
                    row["result"] = json.loads(p["result"]) if p["result"] else None
                    self._result = [{"reserved_units": row["reserved_units"]}]
            elif sql.startswith("UPDATE inference_usage SET excess_units"):
                row = db["rows"].get(str(p["request_id"]))
                assert row is not None, "excess of unknown request"
                row["excess_units"] = int(p["excess"])
                self._result = []
            else:  # pragma: no cover - fake must cover every budget statement
                raise AssertionError(f"unhandled SQL: {sql[:80]}")


class _BudgetConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self) -> _BudgetCursor:
        return _BudgetCursor(self._db)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _new_budget_db() -> dict[str, Any]:
    return {
        "allowance": {"allowance": 0, "used": 0, "demo_reserve": 0},
        "rows": {},
        "lock": threading.Lock(),
    }


# --- Workflow/package fake ----------------------------------------------------


class _FlowCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self._result: list[Any] = []

    def __enter__(self) -> _FlowCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def execute(self, sql: str, params: Any = None) -> None:
        p = dict(params or {})
        db = self._db
        if sql.startswith("SELECT * FROM workflow_runs WHERE run_id"):
            row = next(
                (r for r in db["runs"] if str(r["run_id"]) == str(p["rid"])), None
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("INSERT INTO workflow_records"):
            key = (str(p["run"]), p["kind"], p["key"], p["version"])
            db["ops"].append(f"insert-record:{p['kind']}")
            if key in db["record_keys"]:
                self._result = []
            else:
                db["record_keys"].add(key)
                row = {
                    "record_id": p["rid"], "run_id": p["run"], "kind": p["kind"],
                    "record_key": p["key"], "version": p["version"],
                    "payload": json.loads(p["payload"]), "digest": p["digest"],
                }
                db["records"].append(row)
                self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM workflow_records WHERE run_id"):
            rows = [r for r in db["records"] if str(r["run_id"]) == str(p["run"])]
            if "AND kind = %(kind)s" in sql:
                rows = [r for r in rows if r["kind"] == p["kind"]]
            if "AND record_key = %(key)s" in sql:
                rows = [r for r in rows if r["record_key"] == p["key"]]
            if "AND version = %(version)s" in sql:
                rows = [r for r in rows if r["version"] == p["version"]]
            if "ORDER BY version DESC LIMIT 1" in sql:
                rows.sort(key=lambda r: r["version"], reverse=True)
                rows = rows[:1]
            elif "ORDER BY record_key, version" in sql:
                rows.sort(key=lambda r: (r["record_key"], r["version"]))
            elif "ORDER BY kind, record_key, version" in sql:
                rows.sort(key=lambda r: (r["kind"], r["record_key"], r["version"]))
            self._result = [dict(r) for r in rows]
        elif "FROM uriel_packages" in sql and "WHERE entry_id" in sql:
            row = next(
                (r for r in db["packages"] if str(r["entry_id"]) == str(p["eid"])),
                None,
            )
            if row is None:
                self._result = []
            else:
                found = dict(row)
                found["payload"] = json.dumps(row["payload"])
                self._result = [found]
        elif sql.startswith("SELECT page_no, mime, digest, source_ref FROM extraction_originals"):
            self._result = [
                {"page_no": page, "mime": row["mime"], "digest": row["digest"],
                 "source_ref": row["source"]}
                for (doc, page), row in sorted(db["originals"].items())
                if doc == p["doc"]
            ]
        elif sql.startswith("SELECT mime, digest, source_ref FROM extraction_originals"):
            row = db["originals"].get((p["doc"], int(p["page"])))
            self._result = [
                {"mime": row["mime"], "digest": row["digest"], "source_ref": row["source"]}
            ] if row else []
        elif sql.startswith("INSERT INTO extraction_originals"):
            key = (p["doc"], int(p["page"]))
            if key not in db["originals"]:
                db["originals"][key] = {
                    "mime": p["mime"], "digest": str(p["digest"]), "source": p["source"],
                }
                db["original_rows"].append(dict(p))
                self._result = [{"page_no": int(p["page"])}]
            else:
                self._result = []
        else:  # pragma: no cover - fake must cover every assistance statement
            raise AssertionError(f"unhandled SQL: {sql[:80]}")


class _FlowConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self.commits = 0
        self.closes = 0

    def cursor(self) -> _FlowCursor:
        return _FlowCursor(self._db)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closes += 1


def _seed_flow() -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = uuid4()
    prop = uuid4()
    db: dict[str, Any] = {
        "runs": [], "records": [], "record_keys": set(), "packages": [],
        "originals": {}, "original_rows": [], "ops": [],
    }
    db["runs"].append({
        "run_id": run_id, "idempotency_key": "assist-1",
        "stage": "raphael", "status": "complete_stopped",
        "correlation_id": "", "limits": {}, "usage": {}, "errors": [],
    })

    def _record(kind: str, key: str, payload: dict[str, Any]) -> None:
        db["record_keys"].add((str(run_id), kind, key, 1))
        db["records"].append({
            "record_id": uuid4(), "run_id": run_id, "kind": kind,
            "record_key": key, "version": 1, "payload": payload, "digest": "d",
        })

    _record("dossier_version", "main", {
        "property_id": str(prop),
        "dossier": {
            "category_attribution": {"status": "observed", "observed": "high_rise"},
            "county_attribution": {"evidence_status": "verified", "value": "Dallas County"},
        },
    })
    _record("raphael_decision", "digest-1", {
        "outcome": "needs_evidence", "basis": ["identity evidence missing"],
    })
    _record("scenario", "bench", {
        "name": "bench", "complete": True, "missing": [],
        "outputs": {"sites": 100.0},
        "financial_assessment": {
            "assessed": True,
            "assessment": {
                "complete": True, "verdict": "favorable",
                "verdict_reason": "operating net positive under stated assumptions",
                "drivers": ["labor cost 100.00 USD/yr"],
                "assumptions": ["No financing modeled."],
                "evidence_requests": [],
                "outputs": {
                    "capex_usd": "1000.00", "opex_usd_per_year": "500.00",
                    "revenue_usd_per_year": "900.00",
                    "operating_net_usd_per_year": "400.00",
                    "simple_payback_years": 2.5,
                    "sensitivity": {
                        "yield_plus_20pct_net_usd": "580.00",
                        "yield_minus_20pct_net_usd": "220.00",
                    },
                },
            },
        },
    })
    _record("research_artifact", "a1", {
        "provider": "brave", "assignment_id": "a1", "query": "Meridian Tower Dallas",
        "results": [
            {
                "url": "https://broker-a.example/x", "title": "Meridian Tower for sale",
                "snippet": "Meridian Tower offers 50000 square feet downtown Dallas",
                "domain": "broker-a.example", "assignment_id": "a1",
            },
            {
                "url": "https://broker-b.example/y", "title": "Meridian lease rates",
                "snippet": "Meridian Tower vacancy reported near twelve percent",
                "domain": "broker-b.example", "assignment_id": "a1",
            },
        ],
    })
    entry = uuid4()
    dossier = uuid4()
    db["packages"].append({
        "entry_id": entry, "dossier_id": dossier, "version": 1,
        "snapshot_id": uuid4(), "receipt_id": uuid4(),
        "simulated": False, "execution_state": "not_attempted",
        "package_state": "prepared",
        "payload": {
            "prepared_by": "owner",
            "lineage": {"dossier_id": str(dossier), "version": 1},
            "engineering": {"config": "bench", "sites": 100},
            "economics": {"verdict": "favorable"},
            "unknowns": ["roof load unverified"],
            "blockers": [],
        },
        "created_at": "t",
    })
    ids = {"run_id": run_id, "prop": prop, "entry": entry, "dossier": dossier}
    return db, ids


# --- Mocked provider ----------------------------------------------------------


class _Resp:
    def __init__(
        self,
        obj: Any,
        *,
        status_code: int = 200,
        usage: dict[str, Any] | None = None,
        finish: str = "stop",
        request_id: str = "req-live-1",
        raw: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        content = raw if raw is not None else json.dumps(obj)
        self._raw = json.dumps({
            "id": request_id,
            "choices": [{
                "finish_reason": finish,
                "message": {"content": content},
            }],
            "usage": dict(usage) if usage is not None else {
                "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
            },
        }).encode("utf-8")

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 16384):  # type: ignore[no-untyped-def]
        yield self._raw


def _canned() -> dict[str, Any]:
    return {
        "interpret_command": {"intent": "help", "args": {}},
        "propose_configs": {
            "options": [{"config": "bench"}, {"config": "rack3"}],
            "note": "canned",
        },
        "assist_explain": {
            "summary": "Run stopped at Raphael for missing identity evidence.",
            "points": ["Decision basis names the gap."],
            "unknowns": ["Ownership records."],
        },
        "analyze_evidence": {
            "conflicts": [],
            "gaps": [{
                "text": "No ownership source present.",
                "citations": [{"evidence_id": "a1#0", "excerpt": "Meridian Tower"}],
            }],
            "research_questions": [],
        },
        "explain_economics": {
            "summary": "Net is positive under stated assumptions.",
            "points": [{
                "text": "Operating net drives the verdict.",
                "figures": ["bench:operating_net_usd_per_year"],
            }],
        },
        "extract_document": {
            "claims": [{
                "page": "brochure:1", "kind": "dimension",
                "claim": "Hall spans 50000 square feet.",
            }],
        },
        "draft_outreach": {
            "kind": "intro_letter",
            "sections": [{"heading": "Hello", "body": "Draft introduction."}],
            "uncertainties": ["Roof load unverified."],
        },
    }


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    canned: dict[str, Any] | None = None,
    oplog: list[str] | None = None,
) -> dict[str, Any]:
    httpx = pytest.importorskip("httpx")
    seen: dict[str, Any] = {"calls": 0, "purposes": [], "bodies": []}
    canned = canned if canned is not None else _canned()

    def _stream(method: str, url: str, **kwargs: Any) -> _Resp:
        seen["calls"] += 1
        seen["url"] = url
        body = kwargs.get("json", {})
        seen["bodies"].append(body)
        if oplog is not None:
            oplog.append("provider-call")
        content = body["messages"][1]["content"]
        if isinstance(content, list):
            seen["images"] = len([p for p in content if p.get("type") == "image_url"])
            seen["image_labels"] = [
                p["text"] for p in content
                if p.get("type") == "text" and p.get("text", "").startswith("[image")
            ]
            content = next(
                p["text"] for p in content
                if p.get("type") == "text" and not p.get("text", "").startswith("[image")
            )
        data = json.loads(content)
        seen["purposes"].append(data["purpose"])
        seen["model"] = body.get("model")
        return _Resp(canned[data["purpose"]])

    monkeypatch.setattr(httpx, "stream", _stream)
    return seen


@pytest.fixture()
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-secret")
    monkeypatch.setenv("META_SPEND_APPROVED", "1")
    monkeypatch.setenv("META_API_URL", "https://inference.baseten.co/v1")
    monkeypatch.setenv("META_API_KEY", "test-key")
    monkeypatch.setenv("META_MODEL", "meta-models/muse-spark-open-1026")
    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "100000")
    monkeypatch.setenv("INFERENCE_USD_PER_1K_TOKENS", "0.002")
    monkeypatch.setenv("INFERENCE_TOKENS_PER_IMAGE", "2000")


def _wired(monkeypatch: pytest.MonkeyPatch, budget_db: dict[str, Any]) -> None:
    import app.inference as inference_module

    monkeypatch.setattr(
        inference_module, "get_connection", lambda: _BudgetConn(budget_db)
    )


def _override(intent: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"intent": intent, "args": args or {}}


# --- Acceptance: every capability shares one integration + budget ---


def test_all_capabilities_use_shared_budget_and_ledger(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    conn = _FlowConn(flow_db)
    run = str(ids["run_id"])

    def _run(intent: str, args: dict[str, Any]) -> dict[str, Any]:
        return handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True, intent_override=_override(intent, args),
            conn=conn, principal="wondrue-operator",
        )

    explained = _run("assist_explain", {"run_id": run})
    assert explained["data"]["model"]["mode"] == "live"
    assert "needs_evidence" in explained["data"]["brief"]["stop"]
    assert explained["data"]["commentary"]["summary"]
    analyzed = _run("analyze_evidence", {"run_id": run})
    assert analyzed["data"]["suggestions"]["gaps"][0]["citations"][0]["evidence_id"] == "a1#0"
    economics = _run("explain_economics", {"run_id": run})
    assert economics["data"]["commentary"]["points"][0]["figures"] == [
        "bench:operating_net_usd_per_year"
    ]
    assert economics["data"]["rendered"][0]["values"][0] == {
        "label": "bench:operating_net_usd_per_year",
        "value": "400.00",
        "provenance": "michael.assess",
    }
    drafted = _run(
        "draft_outreach",
        {"package_entry": str(ids["entry"]), "draft_kind": "intro_letter"},
    )
    assert drafted["data"]["delivery"].startswith("not sent")
    compared = _run("compare_scenarios", {})
    assert compared["model"]["mode"] == "live"
    assert len(compared["data"]["comparisons"]) == 2

    # Base URL resolves to the chat-completions endpoint; model is exact.
    assert seen["url"] == "https://inference.baseten.co/v1/chat/completions"
    assert seen["model"] == "meta-models/muse-spark-open-1026"
    assert seen["calls"] == 5
    assert budget_db["allowance"]["used"] > 0
    rows = list(budget_db["rows"].values())
    assert len(rows) == 5
    by_capability = {row["capability"]: row for row in rows}
    assert set(by_capability) == {
        "assist_explain", "analyze_evidence", "explain_economics",
        "draft_outreach", "propose",
    }
    assert by_capability["assist_explain"]["agent"] == "wondrue"
    assert by_capability["analyze_evidence"]["agent"] == "raphael"
    assert by_capability["explain_economics"]["agent"] == "michael"
    assert by_capability["draft_outreach"]["agent"] == "uriel"
    assert by_capability["propose"]["agent"] == "onesimus"
    for row in rows:
        assert row["principal"] == "wondrue-operator"
        assert row["provider"] == "meta"
        assert row["model"] == "meta-models/muse-spark-open-1026"
        assert row["prompt_version"] == "assist-prompts-1"
        assert row["schema_version"]
        assert row["request_digest"]
        assert row["outcome"] == "completed"
        assert row["live"] is True
        assert row["reported_total_tokens"] == 15
        assert row["estimated_cost_usd"] == "0.000030"
        assert row["provider_request_id"] == "req-live-1"
    assert str(by_capability["assist_explain"]["run_id"]) == run
    assert str(by_capability["draft_outreach"]["package_entry"]) == str(ids["entry"])
    assert str(by_capability["draft_outreach"]["dossier_id"]) == str(ids["dossier"])
    assert by_capability["draft_outreach"]["run_id"] is None  # no fabricated link
    assert by_capability["propose"]["run_id"] is None
    # Run-based assistance resolves the stored dossier identity/version.
    from app.custody import dossier_id_for_property

    expected_dossier = dossier_id_for_property(ids["prop"])
    for capability in ("assist_explain", "analyze_evidence", "explain_economics"):
        assert str(by_capability[capability]["dossier_id"]) == str(expected_dossier)
        assert by_capability[capability]["dossier_version"] == 1
    artifacts = [r for r in flow_db["records"] if r["kind"] == "wondrue_assistance"]
    assert len(artifacts) == 3  # run-scoped capabilities attach; drafts/proposals do not


# --- Acceptance: fail closed before any provider request ---


def test_missing_approval_budget_config_and_grant_fail_closed(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    flow_db, ids = _seed_flow()

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)

    def _attempt() -> None:
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("assist_explain", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )

    monkeypatch.delenv("META_SPEND_APPROVED")
    with pytest.raises(WondrueError, match="SPEND_APPROVED"):
        _attempt()
    monkeypatch.setenv("META_SPEND_APPROVED", "1")
    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "0")
    with pytest.raises(WondrueError, match="no allowance"):
        _attempt()
    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "100000")
    monkeypatch.delenv("INFERENCE_USD_PER_1K_TOKENS")
    with pytest.raises(WondrueError, match="pricing is not configured"):
        _attempt()
    monkeypatch.setenv("INFERENCE_USD_PER_1K_TOKENS", "0.002")
    with pytest.raises(WondrueError, match="blocked in read-only mode"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            use_live=True,
            intent_override=_override("assist_explain", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-reader",
        )
    # Live interpretation is refused at the boundary in read-only mode.
    with pytest.raises(WondrueError, match="never live calls"):
        handle_command(
            mode="read-only", max_mode="read-only", text="help me", target="demo",
            use_live=True, conn=_FlowConn(flow_db), principal="wondrue-reader",
        )
    assert budget_db["rows"] == {}
    assert budget_db["allowance"]["used"] == 0


def test_exhausted_allowance_refuses_before_provider(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    budget_db["allowance"] = {"allowance": 100000, "used": 100000, "demo_reserve": 0}
    _wired(monkeypatch, budget_db)
    flow_db, ids = _seed_flow()

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)
    with pytest.raises(WondrueError, match="exhausted"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("assist_explain", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )


def test_endpoint_resolution_and_pricing_math() -> None:
    from app.inference_budget import estimate_cost_usd
    from app.model_adapter import resolve_chat_endpoint

    assert resolve_chat_endpoint("https://inference.baseten.co/v1") == (
        "https://inference.baseten.co/v1/chat/completions"
    )
    assert resolve_chat_endpoint("https://inference.baseten.co/v1/chat/completions") == (
        "https://inference.baseten.co/v1/chat/completions"
    )
    with pytest.raises(ModelError):
        resolve_chat_endpoint("")
    with pytest.raises(ModelError):
        resolve_chat_endpoint("https://host/v1/chat/completions/extra")
    import os

    os.environ["INFERENCE_USD_PER_1K_TOKENS"] = "0.002"
    try:
        assert str(estimate_cost_usd(15)) == "0.000030"
        assert str(estimate_cost_usd(0)) == "0.000000"
    finally:
        del os.environ["INFERENCE_USD_PER_1K_TOKENS"]


# --- Acceptance: concurrent reservations + ambiguous outcomes ---


def test_concurrent_reservations_cannot_overspend(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference_budget import BudgetExhausted, reserve_in_transaction

    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "100")
    budget_db = _new_budget_db()
    outcomes: list[str] = []
    lock = threading.Lock()

    def _worker() -> None:
        try:
            reserve_in_transaction(_BudgetConn(budget_db), 20)
            result = "granted"
        except BudgetExhausted:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=_worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert outcomes.count("granted") == 5
    assert outcomes.count("refused") == 5
    assert budget_db["allowance"]["used"] == 100


def test_timeout_stays_unresolved_with_reservation_retained(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _slow(*args: Any, **kwargs: Any) -> Any:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx, "stream", _slow)
    with pytest.raises(ModelError, match="timed out"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    (row,) = list(budget_db["rows"].values())
    assert row["outcome"] == "unresolved"
    assert row["reported_total_tokens"] is None
    assert budget_db["allowance"]["used"] == row["reserved_units"] > 0


# --- Acceptance: attribution, replay labels, idempotency ---


def test_replay_labels_and_idempotent_reuse(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.model_adapter import validate_explanation

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)

    # Pure replays never touch the provider or the budget.
    replayed = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {}},
        use_live=False, mode="read-only", approved=True,
        validator=validate_explanation,
    )
    assert replayed["outcome"] == "replayed"
    assert replayed["model_result"].mode == "replay"
    assert seen["calls"] == 0
    assert budget_db["rows"] == {}

    # A live call followed by the same idempotency key replays stored output.
    first = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {"facts": {}}},
        principal="wondrue-operator", use_live=True, mode="full",
        approved=True, idempotency_key="assist-1",
        validator=validate_explanation,
    )
    assert first["outcome"] == "completed"
    used_after_first = budget_db["allowance"]["used"]
    second = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {"facts": {}}},
        principal="wondrue-operator", use_live=True, mode="full",
        approved=True, idempotency_key="assist-1",
        validator=validate_explanation,
    )
    assert second["outcome"] == "replayed"
    assert second["model_result"].mode == "replay"
    assert second["replay_of"] == first["request_id"]
    assert second["result"] == first["result"]
    assert seen["calls"] == 1
    assert budget_db["allowance"]["used"] == used_after_first
    replay_row = budget_db["rows"][second["request_id"]]
    assert replay_row["live"] is False
    assert replay_row["reserved_units"] == 0
    assert replay_row["reported_total_tokens"] is None  # zero new usage
    assert replay_row["replay_of"] == first["request_id"]
    assert second["model_result"].usage.get("historical") is True
    with pytest.raises(ModelError, match="different request"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {"other": True}},
            principal="wondrue-operator", use_live=True, mode="full",
            approved=True, idempotency_key="assist-1",
            validator=validate_explanation,
        )


# --- Acceptance: malformed/truncated/unsupported input fails safely ---


def test_truncated_and_garbled_responses_fail_with_spend_retained(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    replies = [
        _Resp({"summary": "partial", "points": [], "unknowns": []}, finish="length"),
        _Resp({}, raw="not json at all"),
    ]

    def _stream(method: str, url: str, **kwargs: Any) -> _Resp:
        return replies.pop(0)

    monkeypatch.setattr(httpx, "stream", _stream)
    with pytest.raises(ModelError, match="truncated"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    with pytest.raises(ModelError, match="not valid JSON"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    rows = list(budget_db["rows"].values())
    assert [row["outcome"] for row in rows] == ["failed", "failed"]
    assert budget_db["allowance"]["used"] == sum(row["reserved_units"] for row in rows)
    # Billing evidence travels with structured failures.
    assert rows[0]["reported_total_tokens"] == 15
    assert rows[1]["reported_total_tokens"] == 15
    assert rows[0]["provider_request_id"] == "req-live-1"


def test_usage_preserved_when_semantic_validation_fails(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    canned = _canned()
    canned["explain_economics"] = {
        "summary": "Invented figures.",
        "points": [{"text": "Bogus.", "figures": ["bench:made_up_metric"]}],
    }
    _provider(monkeypatch, canned)
    flow_db, ids = _seed_flow()
    with pytest.raises(WondrueError, match="unknown figure"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("explain_economics", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )
    (row,) = list(budget_db["rows"].values())
    assert row["outcome"] == "rejected"  # usage kept; never accepted/completed
    assert row["reported_total_tokens"] == 15
    assert [r["kind"] for r in flow_db["records"] if "assistance" in r["kind"]] == []


def test_unsupported_media_rejected_before_provider(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import assist as assist_module

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)
    flow_db, _ids = _seed_flow()
    ctx = {"principal": "wondrue-operator", "use_live": True, "mode": "full"}
    with pytest.raises(WondrueError, match="unsupported page format"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "d", "source_ref": "s",
            "pages": [{"page_no": 1, "mime": "application/pdf", "text": "x"}],
        }, ctx)
    with pytest.raises(WondrueError, match="size bound"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "d", "source_ref": "s",
            "pages": [{"page_no": 1, "mime": "text/plain", "text": "x" * 12001}],
        }, ctx)
    with pytest.raises(WondrueError, match="duplicate page_no"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "d", "source_ref": "s",
            "pages": [
                {"page_no": 1, "mime": "text/plain", "text": "a"},
                {"page_no": 1, "mime": "text/plain", "text": "b"},
            ],
        }, ctx)
    with pytest.raises(WondrueError, match="not a supported image format"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "d", "source_ref": "s",
            "pages": [{
                "page_no": 1, "mime": "image/png",
                "data_b64": base64.b64encode(b"not an image at all").decode(),
            }],
        }, ctx)
    with pytest.raises(WondrueError, match="not the claimed"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "d", "source_ref": "s",
            "pages": [{
                "page_no": 1, "mime": "image/jpeg",
                "data_b64": base64.b64encode(_MINIMAL_PNG).decode(),
            }],
        }, ctx)
    assert budget_db["rows"] == {}


# --- Acceptance: citations, catalogs, authorization boundaries ---


def test_invalid_citations_rejected(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    flow_db, ids = _seed_flow()

    def _attempt(analysis: dict[str, Any]) -> None:
        canned = _canned()
        canned["analyze_evidence"] = analysis
        _provider(monkeypatch, canned)
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("analyze_evidence", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )

    with pytest.raises(WondrueError, match="unknown evidence"):
        _attempt({"conflicts": [{
            "text": "Bogus.", "citations": [{"evidence_id": "zz#9", "excerpt": "x"}],
        }], "gaps": [], "research_questions": []})
    with pytest.raises(WondrueError, match="excerpt not found"):
        _attempt({"conflicts": [], "gaps": [{
            "text": "Bogus.",
            "citations": [{"evidence_id": "a1#0", "excerpt": "never appeared anywhere"}],
        }], "research_questions": []})


def test_out_of_catalog_proposals_rejected(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    canned = _canned()
    canned["propose_configs"] = {
        "options": [{"config": "aeroponic_tower", "overrides": {}}],
        "note": "not a Kratky catalog entry",
    }
    _provider(monkeypatch, canned)
    flow_db, _ids = _seed_flow()
    with pytest.raises(ModelError, match="catalog"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True, intent_override=_override("compare_scenarios", {}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )


def test_model_output_cannot_authorize_or_alter_results(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.wondrue import compare_with_proposal

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    conn = _FlowConn(flow_db)
    before = {(r["kind"], r["record_key"], r["version"]) for r in flow_db["records"]}
    for intent, args in (
        ("assist_explain", {"run_id": str(ids["run_id"])}),
        ("analyze_evidence", {"run_id": str(ids["run_id"])}),
        ("explain_economics", {"run_id": str(ids["run_id"])}),
        ("draft_outreach", {
            "package_entry": str(ids["entry"]), "draft_kind": "flyer",
        }),
    ):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True, intent_override=_override(intent, args),
            conn=conn, principal="wondrue-operator",
        )
    new_kinds = {
        r["kind"] for r in flow_db["records"]
        if (r["kind"], r["record_key"], r["version"]) not in before
    }
    assert new_kinds <= {"wondrue_assistance", "model_execution"}
    stored_decision = next(
        r for r in flow_db["records"]
        if r["kind"] == "raphael_decision" and (r["kind"], r["record_key"], r["version"]) in before
    )
    assert stored_decision["payload"]["outcome"] == "needs_evidence"
    # Deterministic math is independent of model prose.
    first = compare_with_proposal(_canned()["propose_configs"], None)
    second = compare_with_proposal(
        {"options": [{"config": "bench"}, {"config": "rack3"}],
         "note": "different words, same options"},
        None,
    )
    assert first["comparisons"] == second["comparisons"]


def test_draft_kinds_restricted_and_packages_untouched(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    with pytest.raises(WondrueError, match="unsupported draft kind"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("draft_outreach", {
                "package_entry": str(ids["entry"]), "draft_kind": "video_script",
            }),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )
    with pytest.raises(WondrueError, match="unsupported draft kind"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("draft_outreach", {
                "package_entry": str(ids["entry"]), "draft_kind": "website_copy",
            }),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )
    assert seen["calls"] == 0
    assert flow_db["packages"][0]["execution_state"] == "not_attempted"
    assert len(flow_db["packages"]) == 1


# --- Acceptance: thresholds, deferral, demo reserve ---


def test_thresholds_warn_defer_and_protect_reserve(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.inference_budget import reserve_in_transaction

    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "1000")
    monkeypatch.setenv("INFERENCE_DEMO_RESERVE", "100")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)

    snapshot = reserve_in_transaction(_BudgetConn(budget_db), 600)
    assert snapshot["fraction"] == 0.6
    assert "warning" in snapshot

    # Optional requests defer at/after 75% without spending.
    reserve_in_transaction(_BudgetConn(budget_db), 200)  # used now 800
    deferred = complete_capability(
        agent="uriel", capability="draft_outreach",
        purpose="draft_outreach", payload={"brief": {}},
        use_live=True, mode="full", approved=True, optional=True,
        validator=lambda result: result,
    )
    assert deferred["outcome"] == "deferred"
    assert deferred["budget"]["fraction"] == 0.8
    assert budget_db["allowance"]["used"] == 800

    # The demo reserve stays protected for ordinary requests.
    from app.inference_budget import BudgetExhausted

    with pytest.raises(BudgetExhausted):
        reserve_in_transaction(_BudgetConn(budget_db), 150)
    reserve_in_transaction(_BudgetConn(budget_db), 150, demo=True)
    assert budget_db["allowance"]["used"] == 950

    # Large batches at/after 90% need an ask-mode token.
    monkeypatch.setenv("INFERENCE_LARGE_BATCH_TOKENS", "10")
    with pytest.raises(BudgetExhausted, match="approval token"):
        reserve_in_transaction(_BudgetConn(budget_db), 20)
    reserve_in_transaction(
        _BudgetConn(budget_db), 20, demo=True, approval_via_token=True
    )
    assert budget_db["allowance"]["used"] == 970


# --- Acceptance: repeatable mocked demonstration through Wondrue ---


def test_mocked_demonstration_through_wondrue_routes(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.inference as inference_module
    import app.routes.wondrue as wondrue_routes
    from fastapi.testclient import TestClient

    from app.main import app

    # Ambient database configuration must not affect route tests.
    monkeypatch.setenv("DATABASE_HOST", "127.0.0.1")
    monkeypatch.setenv("DATABASE_PORT", "15432")
    monkeypatch.setenv("DATABASE_NAME", "guard_db")
    monkeypatch.setenv("DATABASE_USER", "guard")
    monkeypatch.setenv("DATABASE_PASSWORD", "guard-pw")
    budget_db = _new_budget_db()
    flow_db, ids = _seed_flow()
    flow_conn = _FlowConn(flow_db)
    monkeypatch.setattr(
        inference_module, "get_connection", lambda: _BudgetConn(budget_db)
    )
    monkeypatch.setattr(wondrue_routes, "get_connection", lambda: flow_conn)
    seen = _provider(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    operator = {"X-Wondrue-Key": "operator-secret"}
    reader = {"X-Wondrue-Key": "reader-secret"}
    run = str(ids["run_id"])

    def _command(intent: str, args: dict[str, Any], *, live: bool = True) -> dict[str, Any]:
        resp = client.post(
            "/wondrue/command",
            headers=operator,
            json={
                "mode": "full", "target": "real", "use_live": live,
                "intent": {"intent": intent, "args": args},
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    explained = _command("assist_explain", {"run_id": run})
    assert explained["data"]["model"]["mode"] == "live"
    analyzed = _command("analyze_evidence", {"run_id": run})
    assert analyzed["data"]["suggestions"]["gaps"]
    economics = _command("explain_economics", {"run_id": run})
    assert economics["data"]["commentary"]["points"]
    assert economics["data"]["rendered"][0]["values"]
    drafted = _command("draft_outreach", {
        "package_entry": str(ids["entry"]), "draft_kind": "brochure",
    })
    assert drafted["data"]["draft"]["sections"]
    assert drafted["data"]["delivery"].startswith("not sent")
    compared = _command("compare_scenarios", {})
    assert compared["model"]["mode"] == "live"

    # Natural-language interpretation routes through the same boundary.
    interpreted = client.post(
        "/wondrue/command",
        headers=operator,
        json={"mode": "full", "target": "demo", "use_live": True, "text": "help me"},
    )
    assert interpreted.status_code == 200
    assert interpreted.json()["model"]["mode"] == "live"

    # Document extraction over an explicitly supplied text page.
    extracted = client.post(
        "/wondrue/assist/extract",
        headers=operator,
        json={
            "mode": "full", "use_live": True,
            "doc_id": "brochure", "source_ref": "owner-supplied scan",
            "pages": [{
                "page_no": 1, "mime": "text/plain",
                "text": "Hall spans 50000 square feet.",
            }],
            "run_id": run,
        },
    )
    assert extracted.status_code == 200, extracted.text
    body = extracted.json()
    assert body["data"]["extraction"]["claims"][0]["page"] == "brochure:1"
    assert body["data"]["verification"].startswith("unverified")
    assert body["data"]["artifact"]["kind"] == "wondrue_assistance"

    # Read-only usage inspection shows spend by agent/capability.
    usage = client.post(
        "/wondrue/command",
        headers=reader,
        json={
            "mode": "read-only", "target": "real",
            "intent": {"intent": "inspect_inference_usage", "args": {}},
        },
    )
    assert usage.status_code == 200
    snapshot = usage.json()["data"]["inference_usage"]
    assert snapshot["used"] > 0
    agents = {entry["agent"] for entry in snapshot["by_agent_capability"]}
    assert {"wondrue", "raphael", "michael", "uriel", "onesimus", "gabriel"} <= agents

    # Extraction is consequential: read-only refused, ask previews.
    refused = client.post(
        "/wondrue/assist/extract",
        headers=reader,
        json={"mode": "read-only", "doc_id": "d", "source_ref": "s", "pages": []},
    )
    assert refused.status_code == 403
    preview = client.post(
        "/wondrue/assist/extract",
        headers=operator,
        json={
            "mode": "ask", "doc_id": "d", "source_ref": "s",
            "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hello."}],
        },
    )
    assert preview.status_code == 200
    assert preview.json()["outcome"] == "preview"
    assert preview.json()["preview"]["inputs"]["use_live"] is False
    # Invalid input is rejected before any preview is issued.
    invalid = client.post(
        "/wondrue/assist/extract",
        headers=operator,
        json={"mode": "ask", "doc_id": "d", "source_ref": "s", "pages": []},
    )
    assert invalid.status_code == 400
    assert seen["calls"] == 7


# --- Finding 1: extraction approval binds the complete operation ---


def test_extraction_approval_binds_full_operation(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.inference as inference_module
    import app.routes.wondrue as wondrue_routes
    from fastapi.testclient import TestClient

    from app.main import app

    budget_db = _new_budget_db()
    flow_db, ids = _seed_flow()
    flow_conn = _FlowConn(flow_db)
    monkeypatch.setattr(
        inference_module, "get_connection", lambda: _BudgetConn(budget_db)
    )
    monkeypatch.setattr(wondrue_routes, "get_connection", lambda: flow_conn)
    seen = _provider(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    operator = {"X-Wondrue-Key": "operator-secret"}
    run = str(ids["run_id"])

    def _spec(**overrides: Any) -> dict[str, Any]:
        body = {
            "mode": "ask", "use_live": False,
            "doc_id": "bind-doc", "source_ref": "owner scan",
            "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hall spans fifty."}],
            "run_id": run,
        }
        body.update(overrides)
        return body

    def _token_for(body: dict[str, Any]) -> str:
        resp = client.post("/wondrue/assist/extract", headers=operator, json=body)
        assert resp.status_code == 200, resp.text
        return str(resp.json()["approval_token"])

    def _execute(body: dict[str, Any], token: str) -> Any:
        return client.post(
            "/wondrue/assist/extract", headers=operator,
            json={**body, "approval_token": token},
        )

    # Identical resubmission executes (replay answers with no claims).
    approved = _execute(_spec(), _token_for(_spec()))
    assert approved.status_code == 200
    assert approved.json()["data"]["verification"].startswith("unverified")
    assert len(flow_db["original_rows"]) == 1

    # Changed page content is rejected even though shape matches.
    changed = _spec()
    changed["pages"] = [
        {"page_no": 1, "mime": "text/plain", "text": "Hall spans ninety."}
    ]
    resp = _execute(changed, _token_for(_spec()))
    assert resp.status_code == 403
    # Changed association is rejected.
    resp = _execute(_spec(run_id=None), _token_for(_spec()))
    assert resp.status_code == 403
    # Flipped live/replay selection is rejected.
    resp = _execute(_spec(use_live=True), _token_for(_spec()))
    assert resp.status_code == 403
    # Changed idempotency identity is rejected.
    keyed = _spec(idempotency_key="extract-1")
    resp = _execute(keyed, _token_for(_spec()))
    assert resp.status_code == 403
    # Nothing spent on the rejected attempts.
    assert seen["calls"] == 0
    assert budget_db["allowance"]["used"] == 0


# --- Finding 2: conservative bounds and actual-usage reconciliation ---


def test_reported_overrun_is_charged_not_lost(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.inference_budget import usage_by_agent_in_transaction
    from app.model_adapter import validate_explanation

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)

    def _hungry(method: str, url: str, **kwargs: Any) -> _Resp:
        seen["calls"] += 1
        return _Resp(
            _canned()["assist_explain"],
            usage={"prompt_tokens": 40000, "completion_tokens": 10000, "total_tokens": 50000},
        )

    monkeypatch.setattr(httpx, "stream", _hungry)
    completed = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {"tiny": True}},
        use_live=True, mode="full", approved=True,
        validator=validate_explanation,
    )
    reserved = completed["consumption"]["reserved"]
    assert completed["consumption"]["reported_total"] == 50000
    assert completed["consumption"]["excess"] == 50000 - reserved > 0
    assert budget_db["allowance"]["used"] == 50000
    (row,) = list(budget_db["rows"].values())
    assert row["excess_units"] == 50000 - reserved
    assert row["outcome"] == "completed"
    breakdown = usage_by_agent_in_transaction(_BudgetConn(budget_db))
    assert breakdown[0]["consumed_units"] == 50000

    # Repeating a reconcile never double-charges.
    from app.inference_budget import reconcile_in_transaction

    conn = _BudgetConn(budget_db)
    assert reconcile_in_transaction(conn, completed["request_id"], outcome="completed") is False
    assert budget_db["allowance"]["used"] == 50000


def test_concurrency_refusal_releases_instead_of_charging(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.inference as inference_module
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENT", "1")

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)
    slot = inference_module._concurrency_slot()
    assert slot.acquire(blocking=False)
    try:
        with pytest.raises(ModelError, match="concurrency bound"):
            complete_capability(
                agent="wondrue", capability="assist_explain",
                purpose="assist_explain", payload={"brief": {}},
                use_live=True, mode="full", approved=True,
            )
    finally:
        slot.release()
    (row,) = list(budget_db["rows"].values())
    assert row["outcome"] == "released"
    assert budget_db["allowance"]["used"] == 0


def test_rates_must_be_finite_and_media_bound_explicit(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.inference_budget import BudgetConfigError, get_usd_per_1k_tokens

    for bad in ("NaN", "Infinity", "-0.5", "nope"):
        monkeypatch.setenv("INFERENCE_USD_PER_1K_TOKENS", bad)
        with pytest.raises(BudgetConfigError, match="[Pp]ricing"):
            get_usd_per_1k_tokens()
    monkeypatch.setenv("INFERENCE_USD_PER_1K_TOKENS", "0.002")

    # Live media without an explicit bound is refused; text-only is not.
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    monkeypatch.delenv("INFERENCE_TOKENS_PER_IMAGE")
    with pytest.raises(ModelError, match="media token bound"):
        complete_capability(
            agent="gabriel", capability="extract_document",
            purpose="extract_document", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
            images=[{"mime": "image/png", "data_b64": "eA==", "digest": "d"}],
        )
    completed = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {}},
        use_live=True, mode="full", approved=True,
    )
    assert completed["outcome"] == "completed"


# --- Finding 3: failure accounting and bounded reading ---


def test_refusal_and_missing_usage_accounted(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    replies = [
        _Resp({}, finish="refusal"),
        _Resp(_canned()["assist_explain"], usage={}),
    ]

    def _stream(method: str, url: str, **kwargs: Any) -> _Resp:
        return replies.pop(0)

    monkeypatch.setattr(httpx, "stream", _stream)
    with pytest.raises(ModelError, match="refused"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    ambiguous = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {}},
        use_live=True, mode="full", approved=True,
    )
    assert ambiguous["outcome"] == "unresolved"
    assert ambiguous["result"]["summary"]
    assert ambiguous["consumption"]["reported_total"] is None
    rows = list(budget_db["rows"].values())
    assert [row["outcome"] for row in rows] == ["failed", "unresolved"]
    assert rows[0]["reported_total_tokens"] == 15
    assert rows[1]["reported_total_tokens"] is None


def test_size_limit_enforced_during_streaming_read(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.model_adapter import MetaLiveAdapter, ModelRequest

    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("META_SPEND_APPROVED", "1")
    pulled: list[int] = []

    class _Flood:
        status_code = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> _Flood:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 16384):  # type: ignore[no-untyped-def]
            for _ in range(40):
                pulled.append(1)
                yield b"x" * 16384

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _Flood())
    adapter = MetaLiveAdapter(api_url="http://meta.test/v1", api_key="k", model="m")
    with pytest.raises(ModelError, match="exceeds safe size bound"):
        adapter.complete(ModelRequest("interpret_command", {}), approved=True)
    # 262144 / 16384 = 16 chunks fit; the 17th trips the bound mid-read.
    assert len(pulled) == 17


# --- Finding 4: transmitted instructions and schemas ---


def test_outgoing_requests_carry_versioned_directives(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)
    for purpose in (
        "interpret_command", "propose_configs", "assist_explain",
        "analyze_evidence", "explain_economics", "extract_document",
        "draft_outreach",
    ):
        complete_capability(
            agent="wondrue", capability="probe", purpose=purpose,
            payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    assert seen["purposes"] == [
        "interpret_command", "propose_configs", "assist_explain",
        "analyze_evidence", "explain_economics", "extract_document",
        "draft_outreach",
    ]
    for body in seen["bodies"]:
        content = body["messages"][1]["content"]
        data = json.loads(content)
        directive = data["directive"]
        assert directive["instruction_version"] == "assist-directives-1"
        assert len(directive["instructions"]) > 100
        assert directive["schema"]["type"] == "object"
        assert directive["schema"]["required"]
        assert set(data["context"]) == {"brief"}  # untrusted context stays separate
        assert set(directive) <= {
            "instruction_version", "instructions", "schema", "catalog",
        }
    propose = json.loads(seen["bodies"][1]["messages"][1]["content"])["directive"]
    assert propose["catalog"]["parameter"]["config"] == {"allowed": ["bench", "rack3"]}
    assert propose["catalog"]["financial"]
    evidence = json.loads(seen["bodies"][3]["messages"][1]["content"])["directive"]
    assert evidence["schema"]["finding"]["properties"]["citations"]


def test_unknown_purpose_fails_before_transport(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)
    with pytest.raises(ModelError, match="no instruction spec"):
        complete_capability(
            agent="wondrue", capability="probe", purpose="nope",
            payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    assert budget_db["rows"] == {}


# --- Finding 5: adversarial grounding ---


def test_conflicts_need_citations_questions_need_none(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    flow_db, ids = _seed_flow()

    def _attempt(analysis: dict[str, Any]) -> Any:
        canned = _canned()
        canned["analyze_evidence"] = analysis
        _provider(monkeypatch, canned)
        return handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True,
            intent_override=_override("analyze_evidence", {"run_id": str(ids["run_id"])}),
            conn=_FlowConn(flow_db), principal="wondrue-operator",
        )

    with pytest.raises(WondrueError, match="must cite supporting evidence"):
        _attempt({"conflicts": [{"text": "Uncited assertion.", "citations": []}],
                  "gaps": [], "research_questions": []})
    accepted = _attempt({"conflicts": [], "gaps": [],
                         "research_questions": [{"text": "Who owns it?", "citations": []}]})
    assert accepted["data"]["suggestions"]["research_questions"]


def test_invented_figures_rejected_values_server_rendered(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.model_adapter import validate_economics_explanation

    labels = {"bench:operating_net_usd_per_year"}
    with pytest.raises(ModelError, match="must not state monetary"):
        validate_economics_explanation({
            "summary": "Fine.",
            "points": [{"text": "Net is 400.00 per year.",
                        "figures": ["bench:operating_net_usd_per_year"]}],
        }, labels)
    with pytest.raises(ModelError, match="must not state monetary"):
        validate_economics_explanation({
            "summary": "It costs $500 to run.",
            "points": [],
        }, labels)
    # Calendar years are not financial figures.
    cleaned = validate_economics_explanation({
        "summary": "In 2026 the outlook holds; small counts like 3 racks are fine.",
        "points": [{"text": "See the labeled net.",
                    "figures": ["bench:operating_net_usd_per_year"]}],
    }, labels)
    assert cleaned["points"][0]["figures"] == ["bench:operating_net_usd_per_year"]

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    result = handle_command(
        mode="full", max_mode="full", text="", target="real",
        use_live=True,
        intent_override=_override("explain_economics", {"run_id": str(ids["run_id"])}),
        conn=_FlowConn(flow_db), principal="wondrue-operator",
    )
    rendered = result["data"]["rendered"][0]["values"][0]
    assert rendered["value"] == "400.00"  # from Michael's stored outputs
    assert result["data"]["commentary_note"].startswith("model commentary")


def test_draft_brief_preserves_unknown_counts(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    result = handle_command(
        mode="full", max_mode="full", text="", target="real",
        use_live=True,
        intent_override=_override("draft_outreach", {
            "package_entry": str(ids["entry"]), "draft_kind": "flyer",
        }),
        conn=_FlowConn(flow_db), principal="wondrue-operator",
    )
    brief = result["data"]["brief"]
    assert brief["unknowns"] == ["roof load unverified"]
    assert brief["omitted"] == {"unknowns": 0, "blockers": 0}


# --- Finding 6: extraction identity, provenance, retention ---


def test_image_extraction_labels_locators_and_retains_originals(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import assist as assist_module

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    canned = _canned()
    canned["extract_document"] = {"claims": [{
        "page": "img-doc:1", "kind": "dimension", "claim": "Hall spans fifty.",
    }]}
    seen = _provider(monkeypatch, canned)
    flow_db, ids = _seed_flow()
    body = {
        "doc_id": "img-doc", "source_ref": "owner scan",
        "pages": [{
            "page_no": 1, "mime": "image/png",
            "data_b64": base64.b64encode(_MINIMAL_PNG).decode(),
        }],
        "run_id": str(ids["run_id"]),
    }
    ctx = {"principal": "wondrue-operator", "use_live": True, "mode": "full"}
    result = assist_module.extract_document(_FlowConn(flow_db), body, ctx)
    assert result["extraction"]["claims"][0]["page"] == "img-doc:1"
    assert result["verification"].startswith("unverified")
    assert result["association"]["dossier"] == "stored"
    assert seen["images"] == 1
    assert seen["image_labels"] == ["[image for page img-doc:1]"]
    (stored,) = flow_db["original_rows"]
    assert stored["doc"] == "img-doc"
    assert stored["content"] == base64.b64encode(_MINIMAL_PNG).decode()
    assert result["artifact"]["kind"] == "wondrue_assistance"


def test_originals_conflict_refused_resubmission_reused(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import assist as assist_module

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    conn = _FlowConn(flow_db)
    ctx = {"principal": "wondrue-operator", "use_live": True, "mode": "full"}
    body = {
        "doc_id": "brochure", "source_ref": "owner scan",
        "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hall spans fifty."}],
        "run_id": str(ids["run_id"]),
    }
    first = assist_module.extract_document(conn, body, ctx)
    assert first["extraction"]["claims"]
    assert len(flow_db["original_rows"]) == 1
    # Identical resubmission reuses the retained original.
    again = assist_module.extract_document(conn, body, ctx)
    assert again["extraction"]["claims"]
    assert len(flow_db["original_rows"]) == 1
    # Same locator, changed content: refused before any spend.
    calls = {"count": 0}

    def _counting(method: str, url: str, **kwargs: Any) -> Any:
        calls["count"] += 1
        raise AssertionError("unreachable")

    monkeypatch.setattr(httpx, "stream", _counting)
    altered = {
        "doc_id": "brochure", "source_ref": "owner scan",
        "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hall spans ninety."}],
        "run_id": str(ids["run_id"]),
    }
    with pytest.raises(WondrueError, match="different digest"):
        assist_module.extract_document(conn, altered, ctx)
    assert calls["count"] == 0
    # Identical bytes from a newly claimed source do not silently merge.
    resourced = {
        "doc_id": "brochure", "source_ref": "another scan",
        "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hall spans fifty."}],
        "run_id": str(ids["run_id"]),
    }
    with pytest.raises(WondrueError, match="different source"):
        assist_module.extract_document(conn, resourced, ctx)
    assert calls["count"] == 0


def test_run_dossier_relationship_validated(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import assist as assist_module
    from app.custody import dossier_id_for_property

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    conn = _FlowConn(flow_db)
    ctx = {"principal": "wondrue-operator", "use_live": True, "mode": "full"}

    def _body(**overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "doc_id": "rel-doc", "source_ref": "owner scan",
            "pages": [{"page_no": 1, "mime": "text/plain", "text": "Hall spans fifty."}],
        }
        body.update(overrides)
        return body

    canned_claims = {"claims": [{
        "page": "rel-doc:1", "kind": "statement", "claim": "Hall spans fifty.",
    }]}
    canned = _canned()
    canned["extract_document"] = canned_claims
    _provider(monkeypatch, canned)
    stored = dossier_id_for_property(ids["prop"])
    # Matching stored relationship attributes to the stored dossier.
    matched = assist_module.extract_document(
        conn, _body(run_id=str(ids["run_id"]), dossier_id=str(stored)), ctx
    )
    assert matched["association"]["dossier"] == "stored"
    # Mismatched dossier is refused.
    with pytest.raises(WondrueError, match="does not match"):
        assist_module.extract_document(
            conn,
            _body(run_id=str(ids["run_id"]), dossier_id=str(uuid4())),
            ctx,
        )
    # Dossier-only links stay explicitly caller-asserted.
    asserted = assist_module.extract_document(
        conn, _body(dossier_id=str(uuid4())), ctx
    )
    assert asserted["association"]["dossier"] == "caller-asserted"
    assert asserted.get("artifact") is None
    # Global requests stay explicitly unassociated.
    unassociated = assist_module.extract_document(conn, _body(), ctx)
    assert unassociated["association"]["dossier"] == "none"


def test_extraction_aggregate_bound_refused(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import assist as assist_module

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _must_not_call(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("provider must not be reached")

    monkeypatch.setattr(httpx, "stream", _must_not_call)
    flow_db, _ids = _seed_flow()
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (4 * 1024 * 1024)
    with pytest.raises(WondrueError, match="aggregate bound"):
        assist_module.extract_document(_FlowConn(flow_db), {
            "doc_id": "big", "source_ref": "s",
            "pages": [
                {"page_no": 1, "mime": "image/png",
                 "data_b64": base64.b64encode(big).decode()},
                {"page_no": 2, "mime": "image/png",
                 "data_b64": base64.b64encode(big).decode()},
            ],
        }, {"principal": "w", "use_live": True, "mode": "full"})
    assert budget_db["rows"] == {}


# --- Finding 7: replay identity and lock discipline ---


def test_replay_binds_principal_capability_and_context(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.model_adapter import validate_explanation

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    kwargs: dict[str, Any] = {
        "agent": "wondrue", "capability": "assist_explain",
        "purpose": "assist_explain", "payload": {"brief": {}},
        "principal": "wondrue-operator", "use_live": True, "mode": "full",
        "approved": True, "idempotency_key": "bound-1",
        "validator": validate_explanation,
    }
    first = complete_capability(**kwargs)
    assert first["outcome"] == "completed"
    for change in (
        {"principal": "wondrue-reader"},
        {"capability": "analyze_evidence"},
        {"max_tokens": 512},
    ):
        attempt = dict(kwargs)
        attempt.update(change)
        with pytest.raises(ModelError, match="different request"):
            complete_capability(**attempt)


def test_no_workflow_writes_happen_before_provider_call(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    flow_db, ids = _seed_flow()
    _provider(monkeypatch, None, flow_db["ops"])
    handle_command(
        mode="full", max_mode="full", text="", target="real",
        use_live=True,
        intent_override=_override("assist_explain", {"run_id": str(ids["run_id"])}),
        conn=_FlowConn(flow_db), principal="wondrue-operator",
    )
    assert flow_db["ops"][0] == "provider-call"
    assert set(flow_db["ops"][1:]) == {
        "insert-record:wondrue_assistance", "insert-record:model_execution",
    }


# --- Gap 1: deterministic dossier resolution ---


def _add_dossier(
    flow_db: dict[str, Any], run_id: Any, key: str, prop: Any, version: int = 1
) -> None:
    flow_db["record_keys"].add((str(run_id), "dossier_version", key, 1))
    flow_db["records"].append({
        "record_id": uuid4(), "run_id": run_id, "kind": "dossier_version",
        "record_key": key, "version": 1,
        "payload": {
            "property_id": str(prop), "version": version,
            "dossier": {
                "category_attribution": {"status": "observed", "observed": "warehouse"},
                "county_attribution": {"evidence_status": "verified", "value": "Tarrant"},
            },
        },
        "digest": "d",
    })


def test_dossier_key_selection_and_ambiguity(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.custody import dossier_id_for_property

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    other = uuid4()
    _add_dossier(flow_db, ids["run_id"], "cell-b", other, version=3)
    run = str(ids["run_id"])
    conn = _FlowConn(flow_db)

    def _explain(**args: Any) -> dict[str, Any]:
        return handle_command(
            mode="full", max_mode="full", text="", target="real",
            use_live=True, intent_override=_override("assist_explain", args),
            conn=conn, principal="wondrue-operator",
        )

    with pytest.raises(WondrueError, match="supply dossier_key"):
        _explain(run_id=run)
    with pytest.raises(WondrueError, match="unknown dossier key"):
        _explain(run_id=run, dossier_key="cell-zz")
    explained = _explain(run_id=run, dossier_key="cell-b")
    assert explained["data"]["brief"]["facts"]["property_id"] == str(other)
    assert explained["data"]["brief"]["dossier"] == {
        "key": "cell-b", "dossier_id": str(dossier_id_for_property(other)),
        "version": 3,
    }
    (row,) = list(budget_db["rows"].values())
    assert str(row["dossier_id"]) == str(dossier_id_for_property(other))
    assert row["dossier_version"] == 3
    # The legacy "key" argument still selects.
    legacy = _explain(run_id=run, key="main")
    assert legacy["data"]["brief"]["facts"]["property_id"] == str(ids["prop"])


def test_single_unassociated_dossier_selected_with_null_identity(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    _provider(monkeypatch)
    flow_db, ids = _seed_flow()
    for record in flow_db["records"]:
        if record["kind"] == "dossier_version":
            record["payload"] = {"dossier": {}}
    explained = handle_command(
        mode="full", max_mode="full", text="", target="real",
        use_live=True,
        intent_override=_override("assist_explain", {"run_id": str(ids["run_id"])}),
        conn=_FlowConn(flow_db), principal="wondrue-operator",
    )
    assert explained["data"]["brief"]["dossier"] == {
        "key": "main", "dossier_id": None, "version": 1,
    }
    (row,) = list(budget_db["rows"].values())
    assert row["dossier_id"] is None


# --- Gap 3: unresolved billing exposure ---


def test_connect_error_releases_at_zero_spend(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "stream", _broken)
    with pytest.raises(ModelError, match="no spend recorded"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )
    (row,) = list(budget_db["rows"].values())
    assert row["outcome"] == "released"
    assert budget_db["allowance"]["used"] == 0


def test_midstream_interruption_oversize_and_envelope_stay_unresolved(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    class _Interrupting:
        status_code = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> _Interrupting:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 16384):  # type: ignore[no-untyped-def]
            yield b'{"id": "req-x", '
            raise httpx.ReadError("connection dropped")

    class _Raw:
        def __init__(self, raw: bytes) -> None:
            self.status_code = 200
            self.headers: dict[str, str] = {}
            self._raw = raw

        def __enter__(self) -> _Raw:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 16384):  # type: ignore[no-untyped-def]
            yield self._raw

    replies = [_Interrupting(), _Raw(b"x" * 300000), _Raw(b"not json")]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: replies.pop(0))

    def _attempt() -> None:
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )

    for match in ("interrupted", "safe size bound", "unusable"):
        with pytest.raises(ModelError, match=match) as excinfo:
            _attempt()
        assert "double-charge" in str(excinfo.value)
    rows = list(budget_db["rows"].values())
    assert [row["outcome"] for row in rows] == ["unresolved"] * 3
    assert budget_db["allowance"]["used"] == sum(row["reserved_units"] for row in rows)


def test_http_status_maps_4xx_failed_and_5xx_unresolved(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    class _Status:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def __enter__(self) -> _Status:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 16384):  # type: ignore[no-untyped-def]
            yield b'{"error": "nope"}'

    replies = [_Status(400), _Status(500)]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: replies.pop(0))

    def _attempt() -> None:
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )

    with pytest.raises(ModelError, match="HTTP 400"):
        _attempt()
    with pytest.raises(ModelError, match="HTTP 500"):
        _attempt()
    rows = list(budget_db["rows"].values())
    assert [row["outcome"] for row in rows] == ["failed", "unresolved"]
    assert budget_db["allowance"]["used"] == sum(row["reserved_units"] for row in rows)


def test_failure_cost_estimate_preserved_and_missing_usage_unknown(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    replies = [
        _Resp(
            {"error": "bad request"}, status_code=400,
            usage={"prompt_tokens": 10, "completion_tokens": 5,
                   "total_tokens": 15},
        ),
        _Resp({"error": "boom"}, status_code=500, usage={}),
    ]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: replies.pop(0))

    def _attempt() -> None:
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
        )

    with pytest.raises(ModelError, match="HTTP 400"):
        _attempt()
    with pytest.raises(ModelError, match="double-charge"):
        _attempt()
    failed, ambiguous = list(budget_db["rows"].values())
    assert failed["outcome"] == "failed"
    assert failed["reported_total_tokens"] == 15
    assert failed["estimated_cost_usd"] == "0.000030"
    assert ambiguous["outcome"] == "unresolved"
    assert ambiguous["reported_total_tokens"] is None
    assert ambiguous["estimated_cost_usd"] is None
    assert budget_db["allowance"]["used"] == sum(
        row["reserved_units"] for row in (failed, ambiguous)
    )


def test_rejected_output_with_missing_usage_keeps_exposure(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.model_adapter import validate_explanation

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)

    def _stream(method: str, url: str, **kwargs: Any) -> _Resp:
        return _Resp({"summary": "", "points": [], "unknowns": []}, usage={})

    monkeypatch.setattr(httpx, "stream", _stream)
    with pytest.raises(ModelError, match="non-empty text"):
        complete_capability(
            agent="wondrue", capability="assist_explain",
            purpose="assist_explain", payload={"brief": {}},
            use_live=True, mode="full", approved=True,
            validator=validate_explanation,
        )
    (row,) = list(budget_db["rows"].values())
    assert row["outcome"] == "rejected"  # acceptance: rejected...
    assert row["reported_total_tokens"] is None  # ...billing: unresolved
    assert budget_db["allowance"]["used"] == row["reserved_units"] > 0


# --- Gap 4: centralized sizing and reconciled summaries ---


def test_budget_uses_exact_transmitted_sizing(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.inference import complete_capability
    from app.inference_budget import bound_input_units
    from app.model_adapter import build_chat_body

    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)
    payload = {"brief": {"facts": {"a": 1}}}
    completed = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload=payload,
        use_live=True, mode="full", approved=True, max_tokens=512,
    )
    _, text_bytes = build_chat_body(
        "assist_explain", payload, [], "meta-models/muse-spark-open-1026", 512
    )
    assert completed["consumption"]["reserved"] == bound_input_units(text_bytes, 0) + 512
    transmitted = seen["bodies"][0]
    assert transmitted["max_tokens"] == 512
    # Image labels and framing are inside the measured bytes.
    images = [{"mime": "image/png", "data_b64": "eA==", "digest": "d", "page": "d:1"}]
    _, with_media = build_chat_body(
        "extract_document", payload, images, "m", 1024
    )
    _, without_media = build_chat_body("extract_document", payload, [], "m", 1024)
    assert with_media > without_media + len(b"[image for page d:1]")


def test_consumed_reconciles_across_outcomes(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.inference as inference_module
    from app.inference import complete_capability
    from app.inference_budget import usage_by_agent_in_transaction

    httpx = pytest.importorskip("httpx")
    budget_db = _new_budget_db()
    _wired(monkeypatch, budget_db)
    seen = _provider(monkeypatch)
    kwargs: dict[str, Any] = {
        "agent": "wondrue", "capability": "assist_explain",
        "purpose": "assist_explain", "payload": {"brief": {}},
        "principal": "w", "use_live": True, "mode": "full", "approved": True,
    }
    first = complete_capability(**{**kwargs, "idempotency_key": "rec-1"})
    complete_capability(**{**kwargs, "idempotency_key": "rec-1"})  # replay: zero
    monkeypatch.setenv("INFERENCE_MAX_CONCURRENT", "1")
    slot = inference_module._concurrency_slot()
    assert slot.acquire(blocking=False)
    try:
        with pytest.raises(ModelError, match="concurrency bound"):
            complete_capability(**kwargs)
    finally:
        slot.release()
    breakdown = usage_by_agent_in_transaction(_BudgetConn(budget_db))
    (entry,) = breakdown
    assert entry["consumed_units"] == budget_db["allowance"]["used"]
    assert entry["consumed_units"] == first["consumption"]["reserved"]
    assert entry["released_units"] > 0
    assert entry["calls"] == 2  # completed + released; the replay is separate
    assert seen["calls"] == 1


# --- Live PostgreSQL: migration-016 round trip (owner-executed) ---

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


@needs_live_pg
def test_live_budget_reserve_reconcile_round_trip(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: reserve, ledger insert, idempotent replay, restore.

    Provider HTTP stays mocked; only the migration-016 SQL runs for
    real. The singleton allowance row is restored afterward so repeat
    runs stay stable on the disposable database.
    """
    from app.inference import complete_capability
    from app.inference_budget import read_budget_in_transaction
    from app.model_adapter import validate_explanation
    from app.properties_db import get_connection

    _provider(monkeypatch)
    key = f"live-assist-{uuid4().hex}"
    probe = get_connection()
    try:
        before = read_budget_in_transaction(probe)
    finally:
        probe.close()
    first = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {"live": True}},
        principal="wondrue-operator", use_live=True, mode="full",
        approved=True, idempotency_key=key,
        validator=validate_explanation,
    )
    assert first["outcome"] == "completed"
    assert first["model_result"].mode == "live"
    second = complete_capability(
        agent="wondrue", capability="assist_explain",
        purpose="assist_explain", payload={"brief": {"live": True}},
        principal="wondrue-operator", use_live=True, mode="full",
        approved=True, idempotency_key=key,
        validator=validate_explanation,
    )
    assert second["outcome"] == "replayed"
    assert second["replay_of"] == first["request_id"]
    conn = get_connection()
    try:
        after = read_budget_in_transaction(conn)
        assert after["used"] == first["budget"]["used"] > before["used"]
        # Reconciling twice never double-charges on real PostgreSQL.
        from app.inference_budget import reconcile_in_transaction as _reconcile

        assert (
            _reconcile(conn, first["request_id"], outcome="completed") is False
        )
        conn.commit()
        steady = read_budget_in_transaction(conn)
        assert steady["used"] == after["used"]
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE inference_allowance SET allowance_units = %(a)s, "
                "used_units = %(u)s, demo_reserve_units = %(r)s "
                "WHERE id = 1",
                {
                    "a": before["allowance"],
                    "u": before["used"],
                    "r": before["demo_reserve"],
                },
            )
        conn.commit()
        restored = read_budget_in_transaction(conn)
        assert restored["used"] == before["used"]
    finally:
        conn.close()


@needs_live_pg
def test_live_concurrent_reservations_cannot_overspend(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: racing reservations grant at most the allowance.

    Eight threads race 200-unit reservations against a 1,000-unit
    allowance; exactly five win, losers are refused, and the final
    used total equals the allowance. The singleton row is restored
    afterward so repeat runs stay stable.
    """
    from app.inference_budget import (
        BudgetExhausted,
        insert_request_in_transaction,
        read_budget_in_transaction,
        reconcile_in_transaction,
        reserve_in_transaction,
    )
    from app.properties_db import get_connection

    monkeypatch.setenv("INFERENCE_TOKEN_ALLOWANCE", "1000")
    setup = get_connection()
    try:
        before = read_budget_in_transaction(setup)
        with setup.cursor() as cur:
            cur.execute(
                "UPDATE inference_allowance SET allowance_units = 1000, "
                "used_units = 0, demo_reserve_units = 0 WHERE id = 1"
            )
        setup.commit()
    finally:
        setup.close()
    outcomes: list[str] = []
    guard = threading.Lock()

    def _worker() -> None:
        conn = get_connection()
        try:
            try:
                reserve_in_transaction(conn, 200)
            except BudgetExhausted:
                conn.rollback()
                result = "refused"
            else:
                request_id = f"live-conc-{uuid4().hex}"
                insert_request_in_transaction(
                    conn,
                    {
                        "request_id": request_id,
                        "principal": "wondrue-operator",
                        "agent": "wondrue",
                        "capability": "probe",
                        "provider": "meta",
                        "model": "m",
                        "prompt_version": "p",
                        "schema_version": "s",
                        "request_digest": "d",
                        "requested_max_tokens": 100,
                        "bound_input_units": 100,
                        "reserved_units": 200,
                        "live": True,
                    },
                )
                conn.commit()
                reconcile_in_transaction(
                    conn, request_id, outcome="completed",
                    reported={"total_tokens": 200},
                )
                conn.commit()
                result = "granted"
        finally:
            conn.close()
        with guard:
            outcomes.append(result)

    try:
        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert outcomes.count("granted") == 5
        assert outcomes.count("refused") == 3
        check = get_connection()
        try:
            state = read_budget_in_transaction(check)
            assert state["used"] == 1000
        finally:
            check.close()
    finally:
        restore = get_connection()
        try:
            with restore.cursor() as cur:
                cur.execute(
                    "UPDATE inference_allowance SET allowance_units = %(a)s, "
                    "used_units = %(u)s, demo_reserve_units = %(r)s "
                    "WHERE id = 1",
                    {
                        "a": before["allowance"],
                        "u": before["used"],
                        "r": before["demo_reserve"],
                    },
                )
            restore.commit()
        finally:
            restore.close()


@needs_live_pg
def test_live_originals_conflicting_writers_serialize(
    _env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real PG: conflicting originals writers cannot both appear successful.

    Two threads race different content for one locator: exactly one
    stores, the other is refused, and identical resubmission reuses
    the retained original with zero new inserts. The doc id is unique
    per run, so no cleanup can collide with other data.
    """
    import hashlib

    from app.assist import store_originals_in_transaction
    from app.properties_db import get_connection
    from app.wondrue import WondrueError

    doc = f"race-{uuid4().hex}"
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    guard = threading.Lock()

    def _page(text: str) -> dict[str, Any]:
        raw = text.encode("utf-8")
        return {
            "page_no": 1, "mime": "text/plain", "text": text,
            "digest": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
        }

    def _worker(text: str) -> None:
        conn = get_connection()
        try:
            barrier.wait(timeout=30)
            try:
                stored = store_originals_in_transaction(
                    conn, doc, "race", [_page(text)], f"req-{text}",
                )
                conn.commit()
                result = f"stored:{stored}"
            except WondrueError:
                conn.rollback()
                result = "conflict"
        finally:
            conn.close()
        with guard:
            outcomes.append(result)

    threads = [
        threading.Thread(target=_worker, args=("alpha",)),
        threading.Thread(target=_worker, args=("beta",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(outcomes) == ["conflict", "stored:1"]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT digest FROM extraction_originals "
                "WHERE doc_id = %(doc)s AND page_no = 1",
                {"doc": doc},
            )
            row = cur.fetchone()
        assert row is not None
        winner = "alpha" if row["digest"] == _page("alpha")["digest"] else "beta"
        reused = store_originals_in_transaction(
            conn, doc, "race", [_page(winner)], "req-again",
        )
        conn.commit()
        assert reused == 0
    finally:
        conn.close()
