"""Read-only external-action inspection tests (Phase 3C-3B-3A).

Self-contained SELECT-only fake PostgreSQL: the fake raises on any
non-SELECT statement, so every test structurally proves reads never
write. Rows are hand-crafted to ledger shapes (including stale
timestamps no live claim could produce); gated live-PostgreSQL
tests (GROWUP_PG_INTEGRATION=1, migrations 005+006 applied, plus
007 for the reconciled-dossier test) seed through the real ledger
and prove SQL correctness plus shape compatibility for real. No
execution, no routes, no state changes.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.external_action_reads as reads
from app.db import DatabaseUnavailableError
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    canonical_identity_name,
    sign_approval,
)
from app.observability import AgentId

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_A1_CLAIMED = _T0 - timedelta(minutes=20)  # stale at the 15-minute default
_B1_CLAIMED = _T0 - timedelta(minutes=5)  # fresh at the default

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_TEST_OPERATORS = ("operator-alice", "operator-bob")
_TEST_CREDS: dict[str, str] = {}  # canonical name -> private hex, per-test only

_TEST_EXECUTORS = {
    "w-1": "test-only-executor-secret-rd-w-1-aaaa",
    "w-2": "test-only-executor-secret-rd-w-2-aaaa",
}
_TEST_ORCHESTRATORS = {
    "wondrue-1": "test-only-orchestrator-secret-rd-w1",
    "sched-1": "test-only-orchestrator-secret-rd-s1",
    "sched-2": "test-only-orchestrator-secret-rd-s2",
    "sched-denied": "test-only-orchestrator-secret-denied",
}


def _generate_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


@pytest.fixture(autouse=True)
def _enrolled_test_identities(monkeypatch):
    """Enroll operators, services, scopes, and the Wondrue designation."""
    _TEST_CREDS.clear()
    entries = []
    for name in _TEST_OPERATORS:
        priv, pub = _generate_keypair()
        _TEST_CREDS[name] = priv
        entries.append(f"{name}:{pub}")
    monkeypatch.setenv("AUTHORIZER_PUBLIC_KEYS", ",".join(entries))
    monkeypatch.setenv(
        "EXECUTOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _TEST_EXECUTORS.items()),
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _TEST_ORCHESTRATORS.items()),
    )
    monkeypatch.setenv(
        "EXECUTOR_SCOPES", "w-1:outreach_email|contract_sign,w-2:outreach_email"
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_SCOPES",
        "wondrue-1:outreach_email,sched-1:outreach_email,sched-2:contract_sign",
    )
    monkeypatch.setenv("WONDRUE_READER_ID", "wondrue-1")
    yield
    _TEST_CREDS.clear()


def _orch(orch_id="sched-1"):
    return {
        "orchestrator_id": orch_id,
        "orchestrator_proof": _TEST_ORCHESTRATORS[orch_id],
    }


def _wondrue():
    return _orch("wondrue-1")


def _exe(executor_id="w-1"):
    return {
        "executor_id": executor_id,
        "executor_proof": _TEST_EXECUTORS[executor_id],
    }


def _action_row(aid, action_type="outreach_email", state="AUTHORIZED", count=0):
    return {
        "action_id": aid,
        "idempotency_key": f"key-{aid.hex[:12]}",
        "action_type": action_type,
        "agent": AgentId.MICHAEL.value,
        "property_id": None,
        "request_fingerprint": "sha256:test-fixture",
        "authorized_by": "operator-alice",
        "authorization_reference": "ticket-1",
        "authorized_at": _T0,
        "state": state,
        "attempt_count": count,
        "created_at": _T0,
        "updated_at": _T0,
    }


def _attempt_row(tid, aid, number, executor, claimed_at, outcome=None):
    return {
        "attempt_id": tid,
        "action_id": aid,
        "attempt_number": number,
        "executor_id": executor,
        "claimed_at": claimed_at,
        "outcome": outcome,
        "outcome_recorded_at": (_T0 if outcome is not None else None),
        "created_at": claimed_at,
        "updated_at": claimed_at,
    }


class _ReadTables:
    def __init__(self):
        self.by_id: dict = {}
        self.attempts: dict = {}
        self.decisions: dict = {}
        self.events: list = []


class _ReadConn:
    def __init__(self, tables):
        self.tables = tables
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return _ReadCursor(self)

    def execute(self, sql, params=None):
        self.statements.append((sql, dict(params or {})))
        if sql != "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY":
            raise AssertionError(f"read path issued a non-snapshot write: {sql[:60]}")
        return None

    def rollback(self):
        self.rollbacks += 1

    def commit(self):  # pragma: no cover - reads must never call this
        self.commits += 1

    def close(self):
        self.closes += 1


class _ReadCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        params = dict(params or {})
        self._conn.statements.append((sql, params))
        tables = self._conn.tables
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssertionError(f"read path issued a write: {sql[:60]}")
        if "count(*)" in sql and "FROM external_action_attempts" in sql:
            n = sum(
                1 for a in tables.attempts.values()
                if a["action_id"] == params["action_id"]
            )
            self._rows = [{"n": n}]  # dossier attempt-count consistency probe
        elif "count(*)" in sql and "oldest_claimed_at" in sql:  # stale_summary
            matches = _stale_matches(tables, params)
            oldest = min((m["claimed_at"] for m in matches), default=None)
            self._rows = [{"stale_count": len(matches), "oldest_claimed_at": oldest}]
        elif "count(*)" in sql:  # unknown_summary aggregate
            matches = _unknown_matches(tables, params)
            consistent = [
                m for m in matches
                if m["outcome"] == "UNKNOWN" and m["outcome_recorded_at"] is not None
            ]
            oldest = min(
                (m["outcome_recorded_at"] for m in consistent), default=None
            )
            self._rows = [{
                "unknown_count": len(consistent),
                "oldest_unresolved_at": oldest,
                "inconsistent_count": len(matches) - len(consistent),
            }]
        elif "a.state = 'ATTEMPTING'" in sql:  # find_stale_attempting scan
            matches = _stale_matches(tables, params)
            matches.sort(key=lambda m: (m["claimed_at"], str(m["attempt_id"])))
            self._rows = [dict(m) for m in matches[: params["limit"]]]
        elif "a.state = 'UNKNOWN'" in sql:  # list_unknown_actions queue
            matches = _unknown_matches(tables, params)
            # NULL outcome timestamps sort last, matching PostgreSQL's
            # default ASC null ordering; the product code fails closed
            # on such rows whenever the page includes them.
            matches.sort(key=lambda m: (
                m["outcome_recorded_at"] is None,
                m["outcome_recorded_at"],
                str(m["action_id"]),
            ))
            self._rows = [dict(m) for m in matches[: params["limit"]]]
        elif "AS action_state" in sql:  # list_own_open_attempts
            found = [
                {**a, "action_state": tables.by_id[a["action_id"]]["state"],
                 "action_type": tables.by_id[a["action_id"]]["action_type"],
                 "action_attempt_count": tables.by_id[a["action_id"]]["attempt_count"]}
                for a in tables.attempts.values()
                if a["executor_id"] == params["executor_id"] and a["outcome"] is None
            ]
            found.sort(key=lambda m: (m["claimed_at"], str(m["attempt_id"])))
            self._rows = found[: params["limit"]]
        elif sql.startswith("SELECT a.action_id"):  # get_action + dossier action
            # Both issue the same authorized single-row SELECT shape.
            row = tables.by_id.get(params["action_id"])
            if row is not None and "types" in params:
                if row["action_type"] not in params["types"]:
                    row = None
            if row is not None and "executor_id" in params:
                owned = any(
                    a["action_id"] == params["action_id"]
                    and a["executor_id"] == params["executor_id"]
                    for a in tables.attempts.values()
                )
                if not owned:
                    row = None
            self._rows = [dict(row)] if row else []
        elif sql.startswith("SELECT t.attempt_id"):  # list_attempts (auth in SQL)
            found = []
            for attempt in tables.attempts.values():
                if attempt["action_id"] != params["action_id"]:
                    continue
                if "executor_id" in params and attempt["executor_id"] != params["executor_id"]:
                    continue
                if "types" in params:
                    action = tables.by_id.get(attempt["action_id"])
                    if action is None or action["action_type"] not in params["types"]:
                        continue
                found.append(dict(attempt))
            found.sort(
                key=lambda m: (m["attempt_number"], str(m["attempt_id"])),
                reverse="DESC" in sql,  # dossier latest-attempt lookup
            )
            self._rows = found[: params.get("limit", 1)]
        elif "FROM reconciliation_decisions" in sql:  # dossier decision
            self._rows = [
                {k: row[k] for k in (
                    "decision_id", "attempt_id", "outcome", "prior_outcome",
                    "reconciled_by", "reconciliation_reference",
                    "evidence_kind", "evidence_source", "evidence_observed_at",
                    "evidence_digest", "evidence_summary",
                    "artifact_ref", "provider_reference", "storage_ref",
                    "verification_tier", "decided_at",
                )}
                for row in tables.decisions.values()
                if row["action_id"] == params["action_id"]
            ]
        elif "FROM agent_events" in sql:  # dossier audits
            found = [
                {"event_id": e["event_id"], "occurred_at": e["occurred_at"],
                 "event_type": e["event_type"], "severity": e["severity"],
                 "summary": e["summary"]}
                for e in tables.events
                if e["retention_class"] == "permanent"
                and e["event_type"] in params["types"]
                and e["correlation_id"] == params["key"]
                and e["details"].get("action_id") == params["action_id"]
            ]
            found.sort(key=lambda m: (m["occurred_at"], m["event_id"]))
            self._rows = found[: params["limit"]]
        else:  # pragma: no cover - reads issue only these statements
            raise AssertionError(f"unexpected SQL: {sql[:70]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _stale_matches(tables, params):
    cutoff = params["cutoff"]
    types = params.get("types")
    matches = []
    for action in tables.by_id.values():
        if action["state"] != "ATTEMPTING":
            continue
        if types is not None and action["action_type"] not in types:
            continue
        for attempt in tables.attempts.values():
            if attempt["action_id"] != action["action_id"]:
                continue
            if attempt["outcome"] is not None or attempt["claimed_at"] >= cutoff:
                continue
            matches.append({
                "action_id": action["action_id"],
                "action_type": action["action_type"],
                "state": action["state"],
                "attempt_count": action["attempt_count"],
                "attempt_id": attempt["attempt_id"],
                "attempt_number": attempt["attempt_number"],
                "executor_id": attempt["executor_id"],
                "claimed_at": attempt["claimed_at"],
            })
    return matches


def _unknown_matches(tables, params):
    """Queue-shaped rows: UNKNOWN actions with their latest attempt.

    Mirrors the LEFT JOIN (attempt_number = attempt_count): a
    missing latest attempt yields NULL attempt columns exactly as
    PostgreSQL would, so the product code's fail-closed check --
    not the fake -- decides the outcome.
    """
    types = params.get("types")
    matches = []
    for action in tables.by_id.values():
        if action["state"] != "UNKNOWN":
            continue
        if types is not None and action["action_type"] not in types:
            continue
        latest = [
            a for a in tables.attempts.values()
            if a["action_id"] == action["action_id"]
            and a["attempt_number"] == action["attempt_count"]
        ]
        attempt = latest[0] if latest else None
        matches.append({
            "action_id": action["action_id"],
            "action_type": action["action_type"],
            "state": action["state"],
            "attempt_count": action["attempt_count"],
            "idempotency_key": action["idempotency_key"],
            "authorized_by": action["authorized_by"],
            "authorization_reference": action["authorization_reference"],
            "authorized_at": action["authorized_at"],
            "updated_at": action["updated_at"],
            "attempt_id": attempt["attempt_id"] if attempt else None,
            "attempt_number": attempt["attempt_number"] if attempt else None,
            "executor_id": attempt["executor_id"] if attempt else None,
            "outcome": attempt["outcome"] if attempt else None,
            "claimed_at": attempt["claimed_at"] if attempt else None,
            "outcome_recorded_at": (
                attempt["outcome_recorded_at"] if attempt else None
            ),
        })
    return matches


_U1_UNRESOLVED = _T0 - timedelta(hours=2)
_U3_UNRESOLVED = _T0 - timedelta(minutes=90)
_U2_UNRESOLVED = _T0 - timedelta(hours=1)


def _seed_unknown_tables():
    """Queue scenario: three UNKNOWN (u1 oldest, u3 two-attempt, u2 newest),
    plus one action in each other state to prove exclusion.

    u3's action updated_at is deliberately NEWER than u2's while
    its attempt outcome timestamp stays older: any assertion of
    [u1, u3, u2] order proves the queue follows outcome_recorded_at,
    not the action row's updated_at.
    """
    tables = _ReadTables()
    ids = {name: uuid4() for name in (
        "u1", "u2", "u3", "u1a", "u2a", "u3a", "u3b",
        "x_auth", "x_pend", "x_att", "x_suc", "x_fail", "x_att1", "x_suc1",
        "x_fail1",
    )}
    tables.by_id[ids["u1"]] = _action_row(ids["u1"], "outreach_email", "UNKNOWN", 1)
    tables.by_id[ids["u1"]]["updated_at"] = _U1_UNRESOLVED
    tables.by_id[ids["u2"]] = _action_row(ids["u2"], "contract_sign", "UNKNOWN", 1)
    tables.by_id[ids["u2"]]["updated_at"] = _U2_UNRESOLVED
    tables.by_id[ids["u3"]] = _action_row(ids["u3"], "outreach_email", "UNKNOWN", 2)
    # Diverged on purpose: ordering must ignore this newer action timestamp.
    tables.by_id[ids["u3"]]["updated_at"] = _T0 - timedelta(minutes=30)
    tables.attempts[ids["u1a"]] = _attempt_row(
        ids["u1a"], ids["u1"], 1, "w-1", _U1_UNRESOLVED - timedelta(minutes=5),
        "UNKNOWN",
    )
    tables.attempts[ids["u1a"]]["outcome_recorded_at"] = _U1_UNRESOLVED
    tables.attempts[ids["u2a"]] = _attempt_row(
        ids["u2a"], ids["u2"], 1, "w-2", _U2_UNRESOLVED - timedelta(minutes=5),
        "UNKNOWN",
    )
    tables.attempts[ids["u2a"]]["outcome_recorded_at"] = _U2_UNRESOLVED
    tables.attempts[ids["u3a"]] = _attempt_row(
        ids["u3a"], ids["u3"], 1, "w-1", _U3_UNRESOLVED - timedelta(hours=1),
        "FAILED",
    )
    tables.attempts[ids["u3b"]] = _attempt_row(
        ids["u3b"], ids["u3"], 2, "w-2", _U3_UNRESOLVED - timedelta(minutes=5),
        "UNKNOWN",
    )
    tables.attempts[ids["u3b"]]["outcome_recorded_at"] = _U3_UNRESOLVED
    # One action per non-UNKNOWN state, split across both types.
    tables.by_id[ids["x_auth"]] = _action_row(
        ids["x_auth"], "contract_sign", "AUTHORIZED", 0
    )
    tables.by_id[ids["x_pend"]] = _action_row(
        ids["x_pend"], "outreach_email", "PENDING", 0
    )
    tables.by_id[ids["x_att"]] = _action_row(
        ids["x_att"], "outreach_email", "ATTEMPTING", 1
    )
    tables.by_id[ids["x_suc"]] = _action_row(
        ids["x_suc"], "contract_sign", "SUCCEEDED", 1
    )
    tables.by_id[ids["x_fail"]] = _action_row(
        ids["x_fail"], "outreach_email", "FAILED", 1
    )
    tables.attempts[ids["x_att1"]] = _attempt_row(
        ids["x_att1"], ids["x_att"], 1, "w-1", _T0 - timedelta(minutes=30)
    )
    tables.attempts[ids["x_suc1"]] = _attempt_row(
        ids["x_suc1"], ids["x_suc"], 1, "w-1", _T0 - timedelta(hours=3),
        "SUCCEEDED",
    )
    tables.attempts[ids["x_fail1"]] = _attempt_row(
        ids["x_fail1"], ids["x_fail"], 1, "w-2", _T0 - timedelta(hours=4),
        "FAILED",
    )
    return tables, ids


def _decision_row(did, aid, tid, outcome="SUCCEEDED", decided_at=None):
    # Full stored shape, INCLUDING cryptographic material the
    # dossier must never select: exclusion is proven, not assumed.
    return {
        "decision_id": did,
        "action_id": aid,
        "attempt_id": tid,
        "outcome": outcome,
        "asserts_outcome": outcome,
        "prior_outcome": "UNKNOWN",
        "reconciled_by": "operator-alice",
        "reconciliation_reference": "recon-1",
        "approval_signature": "ab" * 64,
        "signing_public_key": "cd" * 32,
        "signing_key_fingerprint": "sha256:" + "ef" * 32,
        "approval_issued_at": _T0 - timedelta(hours=1),
        "approval_expires_at": _T0 + timedelta(hours=1),
        "evidence_kind": "provider_receipt",
        "evidence_source": "provider-support-desk",
        "evidence_observed_at": _T0 - timedelta(hours=2),
        "evidence_digest": "sha256:" + "12" * 32,
        "evidence_summary": "Provider confirmed completion by ticket.",
        "artifact_ref": "receipt-8842",
        "provider_reference": "op-71c0",
        "storage_ref": None,
        "verification_tier": "human_reviewed",
        "decided_at": decided_at or _T0 - timedelta(minutes=30),
    }


def _event_row(eid, occurred, etype, key, aid, retention="permanent"):
    return {
        "event_id": eid,
        "occurred_at": occurred,
        "agent": AgentId.MICHAEL.value,
        "run_id": str(aid),
        "correlation_id": key,
        "property_id": None,
        "event_type": etype,
        "severity": "INFO",
        "summary": f"{etype} for {aid}",
        "details": {"action_id": str(aid), "extra": "unexposed"},
        "evidence_refs": [{"kind": "x", "ref": "unexposed"}],
        "parent_event_id": None,
        "retention_class": retention,
    }


def _seed_dossier_tables():
    """Dossier scenario: the UNKNOWN-queue tables plus a reconciled
    action (r1), a fresh ATTEMPTING action (f1), and audit events
    with association decoys."""
    tables, ids = _seed_unknown_tables()
    for name in ("r1", "r1a", "d1", "f1", "f1a"):
        ids[name] = uuid4()
    tables.by_id[ids["r1"]] = _action_row(ids["r1"], "outreach_email", "SUCCEEDED", 1)
    tables.attempts[ids["r1a"]] = _attempt_row(
        ids["r1a"], ids["r1"], 1, "w-1", _T0 - timedelta(hours=2), "UNKNOWN"
    )
    tables.attempts[ids["r1a"]]["outcome_recorded_at"] = _T0 - timedelta(hours=1)
    tables.decisions[ids["d1"]] = _decision_row(ids["d1"], ids["r1"], ids["r1a"])
    tables.by_id[ids["f1"]] = _action_row(ids["f1"], "outreach_email", "ATTEMPTING", 1)
    # Staleness reads live time (no `now` parameter by contract),
    # so both ATTEMPTING fixtures anchor to the live clock.
    live_now = datetime.now(timezone.utc)
    tables.attempts[ids["x_att1"]]["claimed_at"] = live_now - timedelta(hours=2)
    tables.attempts[ids["f1a"]] = _attempt_row(
        ids["f1a"], ids["f1"], 1, "w-1", live_now - timedelta(seconds=60)
    )
    key_u1 = tables.by_id[ids["u1"]]["idempotency_key"]
    tables.events.extend([
        _event_row("evt-u1-auth", _T0 - timedelta(hours=3),
                   "external_action.authorized", key_u1, ids["u1"]),
        _event_row("evt-u1-claim", _T0 - timedelta(hours=2, minutes=5),
                   "external_action.claimed", key_u1, ids["u1"]),
        _event_row("evt-u1-enqueue", _T0 - timedelta(hours=2, minutes=10),
                   "external_action.transitioned", key_u1, ids["u1"]),
        _event_row("evt-u1-unknown", _U1_UNRESOLVED,
                   "external_action.transitioned", key_u1, ids["u1"]),
        # Decoys: each misses exactly one association predicate.
        _event_row("evt-decoy-action", _T0 - timedelta(hours=1),
                   "external_action.authorized", key_u1, uuid4()),
        _event_row("evt-decoy-key", _T0 - timedelta(hours=1),
                   "external_action.claimed", "key-other-action", ids["u1"]),
        _event_row("evt-decoy-type", _T0 - timedelta(hours=1),
                   "gabriel.run_finished", key_u1, ids["u1"]),
        _event_row("evt-decoy-retention", _T0 - timedelta(hours=1),
                   "external_action.claimed", key_u1, ids["u1"],
                   retention="activity"),
    ])
    key_r1 = tables.by_id[ids["r1"]]["idempotency_key"]
    tables.events.extend([
        _event_row("evt-r1-auth", _T0 - timedelta(hours=4),
                   "external_action.authorized", key_r1, ids["r1"]),
        _event_row("evt-r1-reconciled", _T0 - timedelta(minutes=30),
                   "external_action.reconciled", key_r1, ids["r1"]),
    ])
    return tables, ids


def _seed_tables():
    """Scenario: A stale ATTEMPTING, B fresh ATTEMPTING, C done, D pending."""
    tables = _ReadTables()
    ids = {name: uuid4() for name in ("a", "b", "c", "d", "a1", "b1", "c1")}
    tables.by_id[ids["a"]] = _action_row(
        ids["a"], "outreach_email", "ATTEMPTING", 1
    )
    tables.by_id[ids["b"]] = _action_row(
        ids["b"], "contract_sign", "ATTEMPTING", 1
    )
    tables.by_id[ids["c"]] = _action_row(
        ids["c"], "outreach_email", "SUCCEEDED", 1
    )
    tables.by_id[ids["d"]] = _action_row(ids["d"], "outreach_email", "PENDING", 0)
    tables.attempts[ids["a1"]] = _attempt_row(
        ids["a1"], ids["a"], 1, "w-1", _A1_CLAIMED
    )
    tables.attempts[ids["b1"]] = _attempt_row(
        ids["b1"], ids["b"], 1, "w-2", _B1_CLAIMED
    )
    tables.attempts[ids["c1"]] = _attempt_row(
        ids["c1"], ids["c"], 1, "w-1", _T0 - timedelta(hours=2), "SUCCEEDED"
    )
    return tables, ids


def _factory(monkeypatch, tables):
    made: list = []
    conns: list = []

    def _make(config=None):
        made.append(config)
        conn = _ReadConn(tables)
        conns.append(conn)
        return conn

    monkeypatch.setattr(reads, "get_connection", _make)
    return made, conns


# --- Caller authentication plumbing ---


def test_reads_require_exactly_one_credential_pair(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(AuthenticationFailedError, match="authenticated caller"):
        reads.get_action(action_id=ids["a"])
    with pytest.raises(ValueError, match="exactly one"):
        reads.get_action(action_id=ids["a"], **_wondrue(), **_exe())
    with pytest.raises(ValueError, match="supplied together"):
        reads.get_action(action_id=ids["a"], orchestrator_id="sched-1")
    with pytest.raises(ValueError, match="supplied together"):
        reads.list_attempts(action_id=ids["a"], executor_proof="x")
    with pytest.raises(ValueError, match="invalid action_id"):
        reads.get_action(action_id="not-a-uuid", **_wondrue())
    assert made == []  # all shape/auth failures precede any connection


def test_bad_proof_and_unknown_caller_denied_before_connection(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        reads.get_action(
            action_id=ids["a"], orchestrator_id="sched-1",
            orchestrator_proof="wrong-proof-0000000000000000",
        )
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        reads.list_own_open_attempts(
            executor_id="ghost-9", executor_proof="test-only-ghost-00000000"
        )
    assert made == []


def test_unknown_action_reads_as_missing(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    assert reads.get_action(action_id=uuid4(), **_wondrue()) is None
    assert reads.list_attempts(action_id=uuid4(), **_wondrue()) == []
    assert reads.list_attempts(action_id=uuid4(), **_exe()) == []
    assert made  # reads ran; missing rows are None/[], never errors


def test_out_of_scope_indistinguishable_from_missing(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, conns = _factory(monkeypatch, tables)
    ghost = uuid4()
    # Existing contract_sign action vs nonexistent ID, outreach-scoped caller.
    assert reads.get_action(action_id=ids["b"], **_orch("sched-1")) is None
    assert reads.get_action(action_id=ghost, **_orch("sched-1")) is None
    assert reads.list_attempts(action_id=ids["b"], **_orch("sched-1")) == []
    assert reads.list_attempts(action_id=ghost, **_orch("sched-1")) == []
    # Same observable behavior: one parameterized SELECT each, same shapes.
    kinds = [
        "scoped" if "types" in params else "plain"
        for con in conns for _, params in con.statements
    ]
    assert kinds == ["scoped", "scoped", "scoped", "scoped"]
    # And the rows genuinely exist for an authorized reader.
    assert reads.get_action(action_id=ids["b"], **_wondrue()) is not None
    assert len(reads.list_attempts(action_id=ids["b"], **_wondrue())) == 1


def test_foreign_executor_indistinguishable_from_missing(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    ghost = uuid4()
    # w-2 owns nothing on A: identical to a nonexistent ID.
    assert reads.get_action(action_id=ids["a"], **_exe("w-2")) is None
    assert reads.get_action(action_id=ghost, **_exe("w-2")) is None
    assert reads.list_attempts(action_id=ids["a"], **_exe("w-2")) == []
    assert reads.list_attempts(action_id=ghost, **_exe("w-2")) == []
    # The owner still sees everything.
    assert reads.get_action(action_id=ids["a"], **_exe("w-1")) is not None
    assert len(reads.list_attempts(action_id=ids["a"], **_exe("w-1"))) == 1


# --- Wondrue system-wide visibility ---


def test_wondrue_systemwide_reads(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    assert reads.get_action(action_id=ids["a"], **_wondrue())["state"] == "ATTEMPTING"
    assert reads.get_action(action_id=ids["b"], **_wondrue())["action_type"] == "contract_sign"
    assert len(reads.list_attempts(action_id=ids["a"], **_wondrue())) == 1
    stale0 = reads.find_stale_attempting(stale_after_seconds=0, now=_T0, **_wondrue())
    assert {r["action_id"] for r in stale0} == {ids["a"], ids["b"]}
    summary = reads.stale_summary(stale_after_seconds=0, now=_T0, **_wondrue())
    assert summary["stale_count"] == 2


def test_wondrue_designation_missing_falls_back_to_scoped(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    monkeypatch.delenv("WONDRUE_READER_ID", raising=False)
    # wondrue-1 holds an outreach_email grant: scoped behavior, not system-wide.
    assert reads.get_action(action_id=ids["a"], **_wondrue()) is not None
    assert reads.get_action(action_id=ids["b"], **_wondrue()) is None  # not an error
    stale = reads.find_stale_attempting(stale_after_seconds=0, now=_T0, **_wondrue())
    assert {r["action_id"] for r in stale} == {ids["a"]}


def test_wondrue_designation_malformed_fails_closed(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    monkeypatch.setenv("WONDRUE_READER_ID", "not an id!")
    with pytest.raises(AuthenticationFailedError, match="designation invalid"):
        reads.get_action(action_id=ids["a"], **_wondrue())


def test_forged_wondrue_identity_rejected(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    # Executor credentials are not orchestrator credentials, designation or not.
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.find_stale_attempting(
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"], now=_T0
        )
    # A valid but non-designated orchestrator stays scoped.
    assert reads.get_action(action_id=ids["b"], **_orch("sched-1")) is None
    stale = reads.find_stale_attempting(stale_after_seconds=0, now=_T0, **_orch("sched-1"))
    assert {r["action_id"] for r in stale} == {ids["a"]}


# --- Scoped orchestrator visibility ---


def test_scoped_orchestrator_type_gate(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    assert reads.get_action(action_id=ids["a"], **_orch("sched-1"))["action_type"] == "outreach_email"
    assert len(reads.list_attempts(action_id=ids["c"], **_orch("sched-1"))) == 1
    assert reads.get_action(action_id=ids["b"], **_orch("sched-1")) is None
    assert reads.list_attempts(action_id=ids["b"], **_orch("sched-1")) == []
    assert reads.get_action(action_id=ids["b"], **_orch("sched-2")) is not None


def test_unscoped_orchestrator_sees_nothing_without_querying(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    assert reads.get_action(action_id=ids["a"], **_orch("sched-denied")) is None
    assert reads.list_attempts(action_id=ids["a"], **_orch("sched-denied")) == []
    assert reads.find_stale_attempting(now=_T0, **_orch("sched-denied")) == []
    summary = reads.stale_summary(now=_T0, **_orch("sched-denied"))
    assert (summary["stale_count"], summary["oldest_claimed_at"]) == (0, None)
    assert made == []  # empty grants short-circuit before any connection


def test_missing_and_malformed_scopes_deny_scoped_reads(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    monkeypatch.delenv("ORCHESTRATOR_SCOPES", raising=False)
    assert reads.get_action(action_id=ids["a"], **_orch("sched-1")) is None
    # Wondrue bypasses scopes entirely.
    assert reads.get_action(action_id=ids["b"], **_wondrue()) is not None
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "bogus-entry")
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        reads.get_action(action_id=ids["a"], **_orch("sched-1"))


def test_scope_match_is_exact(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:Outreach_Email")
    assert reads.get_action(action_id=ids["a"], **_orch("sched-1")) is None


# --- Executor own-attempt visibility ---


def test_executor_sees_only_actions_with_owned_attempts(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    assert reads.get_action(action_id=ids["a"], **_exe("w-1"))["state"] == "ATTEMPTING"
    assert reads.get_action(action_id=ids["c"], **_exe("w-1"))["state"] == "SUCCEEDED"
    assert reads.get_action(action_id=ids["a"], **_exe("w-2")) is None
    assert reads.get_action(action_id=ids["d"], **_exe("w-1")) is None  # no attempts at all


def test_executor_attempts_filtered_to_owner(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    own = reads.list_attempts(action_id=ids["a"], **_exe("w-1"))
    assert [a["attempt_id"] for a in own] == [ids["a1"]]
    assert reads.list_attempts(action_id=ids["a"], **_exe("w-2")) == []
    closed = reads.list_attempts(action_id=ids["c"], **_exe("w-1"))
    assert [a["outcome"] for a in closed] == ["SUCCEEDED"]  # completed included
    assert reads.list_attempts(action_id=ids["d"], **_exe("w-1")) == []


def test_list_own_open_attempts(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    own = reads.list_own_open_attempts(**_exe("w-1"))
    assert [a["attempt_id"] for a in own] == [ids["a1"]]
    assert own[0]["action_state"] == "ATTEMPTING"
    assert own[0]["action_type"] == "outreach_email"
    assert own[0]["action_attempt_count"] == 1
    assert [a["attempt_id"] for a in reads.list_own_open_attempts(**_exe("w-2"))] == [ids["b1"]]
    # Closed attempts never appear in the open listing.
    assert all(a["outcome"] is None for a in own)
    # Orchestrator credentials are not executor credentials.
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        reads.list_own_open_attempts(
            executor_id="sched-1", executor_proof=_TEST_ORCHESTRATORS["sched-1"]
        )


def test_executors_cannot_scan(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.find_stale_attempting(
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"], now=_T0
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.stale_summary(
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"], now=_T0
        )
    assert made == []


# --- Staleness detection ---


def test_stale_threshold_boundaries(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    default = reads.find_stale_attempting(now=_T0, **_wondrue())  # 900s default
    assert [r["attempt_id"] for r in default] == [ids["a1"]]
    assert default[0]["age_seconds"] == 1200
    wide = reads.find_stale_attempting(stale_after_seconds=60, now=_T0, **_wondrue())
    assert [r["attempt_id"] for r in wide] == [ids["a1"], ids["b1"]]  # oldest first
    exact = reads.find_stale_attempting(stale_after_seconds=1200, now=_T0, **_wondrue())
    assert exact == []  # claimed exactly at the cutoff is NOT stale (strict <)
    at_second = reads.find_stale_attempting(stale_after_seconds=300, now=_T0, **_wondrue())
    assert [r["attempt_id"] for r in at_second] == [ids["a1"]]
    zero = reads.find_stale_attempting(stale_after_seconds=0, now=_T0, **_wondrue())
    assert [r["attempt_id"] for r in zero] == [ids["a1"], ids["b1"]]
    # Closed attempts and non-ATTEMPTING actions never match, even at threshold 0.
    assert ids["c1"] not in [r["attempt_id"] for r in zero]


def test_stale_summary_counts_and_oldest(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    summary = reads.stale_summary(now=_T0, **_wondrue())
    assert summary["stale_count"] == 1
    assert summary["oldest_claimed_at"] == _A1_CLAIMED
    assert summary["oldest_age_seconds"] == 1200
    assert summary["threshold_seconds"] == 900
    assert summary["checked_at"] == _T0
    wide = reads.stale_summary(stale_after_seconds=0, now=_T0, **_wondrue())
    assert (wide["stale_count"], wide["oldest_claimed_at"]) == (2, _A1_CLAIMED)
    empty = reads.stale_summary(stale_after_seconds=999999, now=_T0, **_wondrue())
    assert empty["stale_count"] == 0
    assert empty["oldest_claimed_at"] is None
    assert empty["oldest_age_seconds"] is None  # NULL unknowns, never zero
    scoped = reads.stale_summary(stale_after_seconds=0, now=_T0, **_orch("sched-2"))
    assert (scoped["stale_count"], scoped["oldest_claimed_at"]) == (1, _B1_CLAIMED)


def test_stale_parameters_validated(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(ValueError, match="non-negative"):
        reads.find_stale_attempting(stale_after_seconds=-1, now=_T0, **_wondrue())
    with pytest.raises(TypeError, match="integer"):
        reads.find_stale_attempting(stale_after_seconds="900", now=_T0, **_wondrue())
    with pytest.raises(ValueError, match="timezone-aware"):
        reads.find_stale_attempting(now=_T0.replace(tzinfo=None), **_wondrue())
    with pytest.raises(ValueError, match="timezone-aware"):
        reads.stale_summary(now="2026-10-28", **_wondrue())
    assert made == []  # validation precedes authentication and IO


def test_stale_detection_never_transitions(monkeypatch) -> None:
    tables, ids = _seed_tables()
    _factory(monkeypatch, tables)
    before = (copy.deepcopy(tables.by_id), copy.deepcopy(tables.attempts))
    reads.find_stale_attempting(stale_after_seconds=0, now=_T0, **_wondrue())
    reads.stale_summary(stale_after_seconds=0, now=_T0, **_wondrue())
    assert tables.by_id == before[0] and tables.attempts == before[1]
    assert tables.by_id[ids["a"]]["state"] == "ATTEMPTING"  # still queued, GRW-EXT-001


# --- Reconciliation review queue ---


def test_unknown_queue_lists_multiple_oldest_first(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    queue = reads.list_unknown_actions(**_wondrue())
    assert [r["action_id"] for r in queue] == [ids["u1"], ids["u3"], ids["u2"]]
    first = queue[0]
    assert first["action_type"] == "outreach_email"
    assert first["state"] == "UNKNOWN"
    assert first["attempt_id"] == ids["u1a"]
    assert first["attempt_number"] == 1
    assert first["executor_id"] == "w-1"
    assert first["outcome"] == "UNKNOWN"
    assert first["outcome_recorded_at"] == _U1_UNRESOLVED
    assert first["updated_at"] == _U1_UNRESOLVED
    assert first["authorization_reference"] == "ticket-1"


def test_unknown_queue_excludes_other_states(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    seen = {r["action_id"] for r in reads.list_unknown_actions(**_wondrue())}
    assert seen == {ids["u1"], ids["u2"], ids["u3"]}
    for excluded in ("x_auth", "x_pend", "x_att", "x_suc", "x_fail"):
        assert ids[excluded] not in seen
    # The stale-scenario tables hold no UNKNOWN actions at all.
    plain, _ = _seed_tables()
    _factory(monkeypatch, plain)
    assert reads.list_unknown_actions(**_wondrue()) == []


def test_unknown_queue_systemwide_and_scoped(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    assert len(reads.list_unknown_actions(**_wondrue())) == 3  # both types
    sched1 = reads.list_unknown_actions(**_orch("sched-1"))
    assert [r["action_id"] for r in sched1] == [ids["u1"], ids["u3"]]
    sched2 = reads.list_unknown_actions(**_orch("sched-2"))
    assert [r["action_id"] for r in sched2] == [ids["u2"]]


def test_unknown_queue_unscoped_sees_nothing_without_querying(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    made, _ = _factory(monkeypatch, tables)
    assert reads.list_unknown_actions(**_orch("sched-denied")) == []
    denied = reads.unknown_summary(now=_T0, **_orch("sched-denied"))
    assert denied["unknown_count"] == 0
    assert denied["oldest_unresolved_at"] is None
    assert denied["oldest_age_seconds"] is None
    assert denied["checked_at"] == _T0
    assert made == []  # empty grants short-circuit before any connection


def test_unknown_queue_missing_and_malformed_scopes(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    monkeypatch.delenv("ORCHESTRATOR_SCOPES", raising=False)
    assert reads.list_unknown_actions(**_orch("sched-1")) == []
    assert reads.unknown_summary(now=_T0, **_orch("sched-1"))["unknown_count"] == 0
    # Wondrue bypasses scopes entirely.
    assert len(reads.list_unknown_actions(**_wondrue())) == 3
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "bogus-entry")
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        reads.list_unknown_actions(**_orch("sched-1"))
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        reads.unknown_summary(now=_T0, **_orch("sched-1"))


def test_unknown_queue_rejects_executors_and_bad_credentials(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.list_unknown_actions(
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"]
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.unknown_summary(
            now=_T0,
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"],
        )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.list_unknown_actions(
            orchestrator_id="ghost-9", orchestrator_proof="test-only-ghost-0000"
        )
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        reads.list_unknown_actions(
            orchestrator_id="sched-1",
            orchestrator_proof="wrong-proof-0000000000000000",
        )
    with pytest.raises(TypeError, match="required keyword-only"):
        reads.list_unknown_actions()  # type: ignore[call-arg] -- no credentials
    with pytest.raises(TypeError, match="required keyword-only"):
        reads.unknown_summary()  # type: ignore[call-arg] -- no credentials
    assert made == []


def test_unknown_queue_selects_latest_attempt(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    queue = reads.list_unknown_actions(**_wondrue())
    u3 = next(r for r in queue if r["action_id"] == ids["u3"])
    assert u3["attempt_id"] == ids["u3b"]  # number 2, not the FAILED number 1
    assert u3["attempt_number"] == 2
    assert u3["executor_id"] == "w-2"
    assert u3["outcome"] == "UNKNOWN"


def test_unknown_queue_missing_attempt_fails_closed(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    del tables.attempts[ids["u1a"]]  # UNKNOWN action, latest attempt gone
    ghost = uuid4()
    tables.by_id[ghost] = _action_row(ghost, "outreach_email", "UNKNOWN", 0)
    _factory(monkeypatch, tables)
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.list_unknown_actions(**_wondrue())
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.unknown_summary(now=_T0, **_wondrue())


@pytest.mark.parametrize("bad_outcome", ["SUCCEEDED", "FAILED", None])
def test_unknown_queue_wrong_latest_outcome_fails_closed(monkeypatch, bad_outcome) -> None:
    tables, ids = _seed_unknown_tables()
    tables.attempts[ids["u2a"]]["outcome"] = bad_outcome
    _factory(monkeypatch, tables)
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.list_unknown_actions(**_wondrue())
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.unknown_summary(now=_T0, **_wondrue())


def test_unknown_queue_ignores_out_of_scope_inconsistency(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    tables.attempts[ids["u2a"]]["outcome"] = "SUCCEEDED"  # contract_sign, broken
    _factory(monkeypatch, tables)
    # Outreach-scoped callers never see the broken row: no leak, no failure.
    sched1 = reads.list_unknown_actions(**_orch("sched-1"))
    assert [r["action_id"] for r in sched1] == [ids["u1"], ids["u3"]]
    assert reads.unknown_summary(now=_T0, **_orch("sched-1"))["unknown_count"] == 2
    # System-wide visibility reports the break instead of hiding it.
    with pytest.raises(DatabaseUnavailableError, match="inconsistent"):
        reads.list_unknown_actions(**_wondrue())


def test_unknown_queue_stable_order_and_limits(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    tables.attempts[ids["u2a"]]["outcome_recorded_at"] = _U1_UNRESOLVED  # tie u1
    queue = reads.list_unknown_actions(**_wondrue())
    assert [r["action_id"] for r in queue[:2]] == sorted([ids["u1"], ids["u2"]])
    assert queue[2]["action_id"] == ids["u3"]
    page = reads.list_unknown_actions(limit=1, **_wondrue())
    assert [r["action_id"] for r in page] == [min(ids["u1"], ids["u2"])]
    with pytest.raises(ValueError, match="positive"):
        reads.list_unknown_actions(limit=0, **_wondrue())
    with pytest.raises(TypeError, match="integer"):
        reads.list_unknown_actions(limit="50", **_wondrue())


def test_unknown_queue_orders_by_outcome_recorded_at(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    made, conns = _factory(monkeypatch, tables)
    # u3's action row is newer than u2's, but its attempt outcome
    # is older: the queue must follow the attempt timestamp.
    assert tables.by_id[ids["u3"]]["updated_at"] > tables.by_id[ids["u2"]]["updated_at"]
    assert (
        tables.attempts[ids["u3b"]]["outcome_recorded_at"]
        < tables.attempts[ids["u2a"]]["outcome_recorded_at"]
    )
    queue = reads.list_unknown_actions(**_wondrue())
    assert [r["action_id"] for r in queue] == [ids["u1"], ids["u3"], ids["u2"]]
    assert [r["outcome_recorded_at"] for r in queue] == sorted(
        r["outcome_recorded_at"] for r in queue
    )
    sql = conns[-1].statements[-1][0]
    assert "ORDER BY t.outcome_recorded_at ASC, a.action_id ASC" in sql
    summary = reads.unknown_summary(now=_T0, **_wondrue())
    assert summary["oldest_unresolved_at"] == _U1_UNRESOLVED
    assert "min(t.outcome_recorded_at)" in conns[-1].statements[-1][0]


def test_unknown_queue_null_outcome_timestamp_fails_closed(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    tables.attempts[ids["u1a"]]["outcome_recorded_at"] = None  # UNKNOWN, no history
    _factory(monkeypatch, tables)
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.list_unknown_actions(**_wondrue())
    with pytest.raises(DatabaseUnavailableError, match="unknown_action_inconsistent"):
        reads.unknown_summary(now=_T0, **_wondrue())


def test_unknown_queue_limit_clamped_in_sql(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    made, conns = _factory(monkeypatch, tables)
    assert len(reads.list_unknown_actions(limit=100000, **_wondrue())) == 3
    assert conns[-1].statements[-1][1]["limit"] == reads.MAX_READ_LIMIT


def test_unknown_summary_counts_oldest_and_ages(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    summary = reads.unknown_summary(now=_T0, **_wondrue())
    assert summary["unknown_count"] == 3
    assert summary["oldest_unresolved_at"] == _U1_UNRESOLVED
    assert summary["oldest_age_seconds"] == 7200
    assert summary["checked_at"] == _T0
    scoped = reads.unknown_summary(now=_T0, **_orch("sched-2"))
    assert (scoped["unknown_count"], scoped["oldest_unresolved_at"]) == (1, _U2_UNRESOLVED)
    assert scoped["oldest_age_seconds"] == 3600
    with pytest.raises(ValueError, match="timezone-aware"):
        reads.unknown_summary(now="2026-10-28", **_wondrue())
    with pytest.raises(ValueError, match="timezone-aware"):
        reads.unknown_summary(now=_T0.replace(tzinfo=None), **_wondrue())


def test_unknown_summary_empty_queue(monkeypatch) -> None:
    tables, _ = _seed_tables()  # no UNKNOWN actions
    made, _ = _factory(monkeypatch, tables)
    summary = reads.unknown_summary(now=_T0, **_wondrue())
    assert summary["unknown_count"] == 0
    assert summary["oldest_unresolved_at"] is None
    assert summary["oldest_age_seconds"] is None  # NULL unknowns, never zero
    assert summary["checked_at"] == _T0
    assert made  # the aggregate ran; emptiness is a result, not a skip
    assert reads.list_unknown_actions(**_wondrue()) == []


def test_unknown_queue_parameterization(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    made, conns = _factory(monkeypatch, tables)
    hostile = "x'); DROP TABLE external_actions;--"
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", f"sched-1:{hostile}")
    assert reads.list_unknown_actions(**_orch("sched-1")) == []
    sql, params = conns[-1].statements[-1]
    assert "= ANY(%(types)s)" in sql
    assert hostile not in sql  # bound as data, never interpolated
    assert hostile in params["types"]
    assert reads.unknown_summary(now=_T0, **_orch("sched-1"))["unknown_count"] == 0


def test_unknown_queue_never_transitions(monkeypatch) -> None:
    tables, ids = _seed_unknown_tables()
    _factory(monkeypatch, tables)
    before = (copy.deepcopy(tables.by_id), copy.deepcopy(tables.attempts))
    reads.list_unknown_actions(**_wondrue())
    reads.unknown_summary(now=_T0, **_wondrue())
    assert tables.by_id == before[0] and tables.attempts == before[1]
    assert tables.by_id[ids["u1"]]["state"] == "UNKNOWN"  # still queued, GRW-EXT-001
    assert tables.attempts[ids["u1a"]]["outcome"] == "UNKNOWN"  # history kept


# --- Reconciliation case dossier ---


def test_dossier_wondrue_unresolved_unknown(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    _factory(monkeypatch, tables)
    case = reads.get_reconciliation_case(action_id=ids["u1"], **_wondrue())
    assert case["action"]["state"] == "UNKNOWN"
    assert [a["attempt_id"] for a in case["attempts"]] == [ids["u1a"]]
    assert case["attempts_truncated"] is False
    assert case["reconciliation"] is None
    assert case["interpretation"]["status"] == "unresolved_unknown"
    assert "no execution, retry, or reconciliation" in case["interpretation"]["detail"]
    assert [e["event_type"] for e in case["audits"]] == [
        "external_action.authorized",
        "external_action.transitioned",
        "external_action.claimed",
        "external_action.transitioned",
    ]
    assert case["audits_truncated"] is False
    assert case["generated_at"] is not None


def test_dossier_scoped_visibility_and_empty_scope(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    made, _ = _factory(monkeypatch, tables)
    mine = reads.get_reconciliation_case(action_id=ids["u1"], **_orch("sched-1"))
    assert mine["action"]["action_id"] == ids["u1"]
    assert reads.get_reconciliation_case(action_id=ids["u2"], **_orch("sched-1")) is None
    assert reads.get_reconciliation_case(
        action_id=ids["u2"], **_orch("sched-2")
    ) is not None
    before = len(made)
    assert reads.get_reconciliation_case(
        action_id=ids["u1"], **_orch("sched-denied")
    ) is None
    assert len(made) == before  # empty grants short-circuit, no connection


def test_dossier_credentials_validated_before_connection(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    made, _ = _factory(monkeypatch, tables)
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        reads.get_reconciliation_case(
            action_id=ids["u1"],
            orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"],
        )
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        reads.get_reconciliation_case(
            action_id=ids["u1"], orchestrator_id="sched-1",
            orchestrator_proof="wrong-proof-0000000000000000",
        )
    with pytest.raises(TypeError, match="required keyword-only"):
        reads.get_reconciliation_case(action_id=ids["u1"])  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="invalid action_id"):
        reads.get_reconciliation_case(action_id="not-a-uuid", **_wondrue())
    with pytest.raises(ValueError, match="positive"):
        reads.get_reconciliation_case(
            action_id=ids["u1"], audit_limit=0, **_wondrue()
        )
    assert made == []


def test_dossier_unauthorized_indistinguishable_from_missing(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    made, conns = _factory(monkeypatch, tables)
    ghost = uuid4()
    assert reads.get_reconciliation_case(action_id=ids["u2"], **_orch("sched-1")) is None
    assert reads.get_reconciliation_case(action_id=ghost, **_orch("sched-1")) is None
    assert len(conns) == 2
    for con in conns:
        assert con.statements[0][0].startswith("SET TRANSACTION")
        assert len(con.statements) == 2  # snapshot + scoped action SELECT, then stop
        assert "= ANY(%(types)s)" in con.statements[1][0]
        assert con.commits == 0
        assert con.rollbacks == 1
        assert con.closes == 1
    assert reads.get_reconciliation_case(action_id=ids["u2"], **_wondrue()) is not None


def test_dossier_consistent_snapshot_single_connection(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    made, conns = _factory(monkeypatch, tables)
    assert reads.get_reconciliation_case(action_id=ids["u1"], **_wondrue()) is not None
    assert len(conns) == 1  # one connection, one snapshot, six reads
    first, rest = conns[0].statements[0], conns[0].statements[1:]
    assert first[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert len(rest) == 6
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in rest)
    assert "FROM external_actions" in rest[0][0]
    assert "FROM external_action_attempts" in rest[1][0]
    assert "DESC" in rest[2][0]  # latest-attempt lookup
    assert "count(*)" in rest[3][0]  # unbounded consistency count
    assert "FROM reconciliation_decisions" in rest[4][0]
    assert "FROM agent_events" in rest[5][0]
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1
    assert conns[0].closes == 1


def test_dossier_attempt_ordering_and_truncation(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    _factory(monkeypatch, tables)
    u3 = reads.get_reconciliation_case(action_id=ids["u3"], **_wondrue())
    assert [a["attempt_number"] for a in u3["attempts"]] == [1, 2]
    assert u3["attempts_truncated"] is False
    # Bound stress: 52 attempts (hand-crafted rows past any live shape).
    many = uuid4()
    tables.by_id[many] = _action_row(many, "outreach_email", "UNKNOWN", 52)
    for n in range(1, 53):
        tid = uuid4()
        tables.attempts[tid] = _attempt_row(
            tid, many, n, "w-1", _T0 - timedelta(hours=3),
            "UNKNOWN" if n == 52 else "FAILED",
        )
        if n == 52:
            tables.attempts[tid]["outcome_recorded_at"] = _T0 - timedelta(hours=1)
    big = reads.get_reconciliation_case(action_id=many, **_wondrue())
    assert len(big["attempts"]) == reads.DEFAULT_READ_LIMIT
    assert big["attempts_truncated"] is True
    assert [a["attempt_number"] for a in big["attempts"]] == list(range(1, 51))
    assert big["interpretation"]["status"] == "unresolved_unknown"


def test_dossier_reconciliation_field_selection(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    _factory(monkeypatch, tables)
    case = reads.get_reconciliation_case(action_id=ids["r1"], **_wondrue())
    decision = case["reconciliation"]
    assert set(decision) == {
        "decision_id", "attempt_id", "outcome", "prior_outcome",
        "reconciled_by", "reconciliation_reference",
        "evidence_kind", "evidence_source", "evidence_observed_at",
        "evidence_digest", "evidence_summary",
        "artifact_ref", "provider_reference", "storage_ref",
        "verification_tier", "decided_at",
    }
    assert decision["decision_id"] == ids["d1"]
    assert decision["outcome"] == "SUCCEEDED"
    assert decision["verification_tier"] == "human_reviewed"
    blob = json.dumps(case, default=str)
    # The action's non-sensitive request_fingerprint legitimately
    # appears; the decision's signing material must not.
    for secret in ("approval_signature", "signing_public_key",
                   "signing_key_fingerprint",
                   "ab" * 64, "cd" * 32, "ef" * 32):
        assert secret not in blob  # cryptographic material never selected
    assert "independently verified by Grow Up" in case["interpretation"]["detail"]
    assert "not independently" in case["interpretation"]["detail"]


def test_dossier_audit_association_order_and_truncation(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    made, conns = _factory(monkeypatch, tables)
    case = reads.get_reconciliation_case(action_id=ids["u1"], **_wondrue())
    assert [e["event_id"] for e in case["audits"]] == [
        "evt-u1-auth", "evt-u1-enqueue", "evt-u1-claim", "evt-u1-unknown",
    ]
    for audit in case["audits"]:
        assert set(audit) == {
            "event_id", "occurred_at", "event_type", "severity", "summary"
        }
    assert "unexposed" not in json.dumps(case, default=str)  # no details bodies
    page = reads.get_reconciliation_case(
        action_id=ids["u1"], audit_limit=2, **_wondrue()
    )
    assert [e["event_id"] for e in page["audits"]] == ["evt-u1-auth", "evt-u1-enqueue"]
    assert page["audits_truncated"] is True
    capped = reads.get_reconciliation_case(
        action_id=ids["u1"], audit_limit=100000, **_wondrue()
    )
    assert capped["audits_truncated"] is False
    audit_sql = next(
        params for sql, params in conns[-1].statements if "FROM agent_events" in sql
    )
    assert audit_sql["limit"] == reads.MAX_READ_LIMIT + 1  # clamped, plus one probe
    assert audit_sql["action_id"] == str(ids["u1"])
    assert set(audit_sql["types"]) == {
        "external_action.authorized", "external_action.claimed",
        "external_action.transitioned", "external_action.reconciled",
    }


def test_dossier_statuses_across_states(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    _factory(monkeypatch, tables)
    by_key = {}
    for key in ("u1", "x_att", "f1", "r1", "x_suc", "x_fail", "x_auth", "x_pend"):
        case = reads.get_reconciliation_case(action_id=ids[key], **_wondrue())
        by_key[key] = case["interpretation"]["status"]
    assert by_key == {
        "u1": "unresolved_unknown",
        "x_att": "stale_attempting",
        "f1": "attempting",
        "r1": "reconciled",
        "x_suc": "terminal",
        "x_fail": "terminal",
        "x_auth": "authorized",
        "x_pend": "pending",
    }
    stale = reads.get_reconciliation_case(action_id=ids["x_att"], **_wondrue())
    assert "not proof of failure" in stale["interpretation"]["detail"]
    assert "never permits a retry" in stale["interpretation"]["detail"]
    fresh = reads.get_reconciliation_case(action_id=ids["f1"], **_wondrue())
    assert "not yet established" in fresh["interpretation"]["detail"]
    direct = reads.get_reconciliation_case(action_id=ids["x_suc"], **_wondrue())
    assert direct["reconciliation"] is None
    assert "no reconciliation decision exists" in direct["interpretation"]["detail"]


def test_dossier_preserves_unknown_history_after_reconciliation(monkeypatch) -> None:
    tables, ids = _seed_dossier_tables()
    _factory(monkeypatch, tables)
    case = reads.get_reconciliation_case(action_id=ids["r1"], **_wondrue())
    assert case["action"]["state"] == "SUCCEEDED"
    assert case["attempts"][0]["outcome"] == "UNKNOWN"  # never rewritten
    assert case["reconciliation"]["outcome"] == "SUCCEEDED"
    assert case["reconciliation"]["prior_outcome"] == "UNKNOWN"
    assert case["interpretation"]["status"] == "reconciled"
    assert "preserved history" in case["interpretation"]["detail"]
    assert [e["event_type"] for e in case["audits"]] == [
        "external_action.authorized", "external_action.reconciled",
    ]


def _break_decision_outcome(tables, ids):
    tables.decisions[ids["d1"]]["outcome"] = "FAILED"  # action says SUCCEEDED
    return "r1"


def _break_decision_stale_attempt(tables, ids):
    tables.by_id[ids["u3"]]["state"] = "SUCCEEDED"
    did = uuid4()
    tables.decisions[did] = _decision_row(did, ids["u3"], ids["u3a"])  # not latest
    return "u3"


def _break_rewritten_history(tables, ids):
    tables.attempts[ids["r1a"]]["outcome"] = "SUCCEEDED"  # history rewritten
    return "r1"


def _break_unknown_with_decision(tables, ids):
    did = uuid4()
    tables.decisions[did] = _decision_row(did, ids["u1"], ids["u1a"])
    return "u1"


def _break_terminal_unknown_attempt(tables, ids):
    tables.attempts[ids["x_suc1"]]["outcome"] = "UNKNOWN"  # no decision to explain it
    return "x_suc"


def _break_terminal_mismatched_attempt(tables, ids):
    tables.attempts[ids["x_fail1"]]["outcome"] = "SUCCEEDED"
    return "x_fail"


def _break_attempting_closed_attempt(tables, ids):
    tables.attempts[ids["x_att1"]]["outcome"] = "FAILED"
    return "x_att"


def _break_pending_with_attempts(tables, ids):
    tables.by_id[ids["x_pend"]]["attempt_count"] = 1
    tid = uuid4()
    tables.attempts[tid] = _attempt_row(tid, ids["x_pend"], 1, "w-1", _T0)
    return "x_pend"


def _break_double_decision(tables, ids):
    did = uuid4()
    tables.decisions[did] = _decision_row(did, ids["r1"], ids["r1a"])
    return "r1"


def _break_unknown_missing_attempt(tables, ids):
    del tables.attempts[ids["u1a"]]
    return "u1"


def _break_attempt_count_gap(tables, ids):
    tables.by_id[ids["u1"]]["attempt_count"] = 2  # latest attempt row absent
    return "u1"


def _break_attempt_history_gap(tables, ids):
    gid = uuid4()
    tables.by_id[gid] = _action_row(gid, "outreach_email", "UNKNOWN", 3)
    for n in (1, 3):  # attempt #2 surgically missing; max still matches
        tid = uuid4()
        tables.attempts[tid] = _attempt_row(
            tid, gid, n, "w-1", _T0 - timedelta(hours=3),
            "UNKNOWN" if n == 3 else "FAILED",
        )
        if n == 3:
            tables.attempts[tid]["outcome_recorded_at"] = _T0 - timedelta(hours=1)
    ids["gap"] = gid
    return "gap"


def _break_pending_count_no_rows(tables, ids):
    tables.by_id[ids["x_pend"]]["attempt_count"] = 5  # rows absent, count nonzero
    return "x_pend"


def _break_authorized_count_no_rows(tables, ids):
    tables.by_id[ids["x_auth"]]["attempt_count"] = 2  # rows absent, count nonzero
    return "x_auth"


@pytest.mark.parametrize("breakage", [
    _break_decision_outcome,
    _break_decision_stale_attempt,
    _break_rewritten_history,
    _break_unknown_with_decision,
    _break_terminal_unknown_attempt,
    _break_terminal_mismatched_attempt,
    _break_attempting_closed_attempt,
    _break_pending_with_attempts,
    _break_double_decision,
    _break_unknown_missing_attempt,
    _break_attempt_count_gap,
    _break_attempt_history_gap,
    _break_pending_count_no_rows,
    _break_authorized_count_no_rows,
], ids=[
    "decision-outcome-mismatch",
    "decision-on-stale-attempt",
    "rewritten-history",
    "unknown-with-decision",
    "terminal-unknown-attempt",
    "terminal-mismatched-attempt",
    "attempting-closed-attempt",
    "pending-with-attempts",
    "double-decision",
    "unknown-missing-attempt",
    "attempt-count-gap",
    "attempt-history-gap",
    "pending-count-no-rows",
    "authorized-count-no-rows",
])
def test_dossier_contradictions_fail_closed(monkeypatch, breakage) -> None:
    tables, ids = _seed_dossier_tables()
    target = breakage(tables, ids)
    made, conns = _factory(monkeypatch, tables)
    with pytest.raises(DatabaseUnavailableError, match="reconciliation_case_inconsistent"):
        reads.get_reconciliation_case(action_id=ids[target], **_wondrue())
    assert conns[0].commits == 0
    assert conns[0].rollbacks == 1  # failed snapshot still rolls back
    assert conns[0].closes == 1


# --- Limits, parameterization, and read-only posture ---


def test_result_limit_enforcement(monkeypatch) -> None:
    tables, ids = _seed_tables()
    for n in range(2, 7):  # six open w-1 attempts total (with a1)
        tid = uuid4()
        tables.attempts[tid] = _attempt_row(
            tid, ids["a"], n, "w-1", _T0 - timedelta(minutes=20 + n)
        )
    made, conns = _factory(monkeypatch, tables)
    page = reads.list_own_open_attempts(limit=2, **_exe("w-1"))
    assert len(page) == 2
    huge = reads.list_own_open_attempts(limit=100000, **_exe("w-1"))
    assert len(huge) == 6
    assert conns[-1].statements[-1][1]["limit"] == reads.MAX_READ_LIMIT  # clamped
    scan = reads.find_stale_attempting(stale_after_seconds=0, limit=3, now=_T0, **_wondrue())
    assert len(scan) == 3
    with pytest.raises(ValueError, match="positive"):
        reads.list_attempts(action_id=ids["a"], limit=0, **_wondrue())
    with pytest.raises(TypeError, match="integer"):
        reads.list_attempts(action_id=ids["a"], limit="50", **_wondrue())


def test_sql_parameterization(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, conns = _factory(monkeypatch, tables)
    hostile = "x'); DROP TABLE external_actions;--"
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", f"sched-1:{hostile}")
    assert reads.find_stale_attempting(now=_T0, **_orch("sched-1")) == []
    sql, params = conns[-1].statements[-1]
    assert "= ANY(%(types)s)" in sql
    assert hostile not in sql  # bound as data, never interpolated
    assert hostile in params["types"]
    with pytest.raises(ValueError, match="invalid action_id"):
        reads.get_action(action_id="x' OR '1'='1", **_wondrue())


def test_reads_never_mutate_audit_or_commit(monkeypatch) -> None:
    tables, ids = _seed_tables()
    made, conns = _factory(monkeypatch, tables)
    before = (
        copy.deepcopy(tables.by_id), copy.deepcopy(tables.attempts),
        copy.deepcopy(tables.decisions), copy.deepcopy(tables.events),
    )
    reads.get_action(action_id=ids["a"], **_wondrue())
    reads.list_attempts(action_id=ids["a"], **_wondrue())
    reads.list_attempts(action_id=ids["a"], **_exe("w-1"))
    reads.list_own_open_attempts(**_exe("w-1"))
    reads.find_stale_attempting(now=_T0, **_wondrue())
    reads.stale_summary(now=_T0, **_wondrue())
    reads.list_unknown_actions(**_wondrue())
    reads.unknown_summary(now=_T0, **_wondrue())
    reads.get_reconciliation_case(action_id=ids["a"], **_wondrue())
    assert tables.by_id == before[0] and tables.attempts == before[1]
    assert tables.decisions == before[2] and tables.events == before[3]
    blob = json.dumps([s for c in conns for s in c.statements], default=str)
    for sql, _ in (s for c in conns for s in c.statements):
        if "agent_events" in sql:
            assert sql.lstrip().upper().startswith("SELECT")  # reads, never writes
    assert all(c.commits == 0 for c in conns)  # never committed
    assert all(c.closes > 0 for c in conns)  # connections always released


def test_database_outage_fails_closed(monkeypatch) -> None:
    tables, ids = _seed_tables()
    monkeypatch.setattr(
        reads, "get_connection",
        lambda config=None: (_ for _ in ()).throw(
            DatabaseUnavailableError("connection_failed")
        ),
    )
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        reads.get_action(action_id=ids["a"], **_wondrue())
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        reads.list_unknown_actions(**_wondrue())
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        reads.unknown_summary(now=_T0, **_wondrue())


# --- Structural read-only guards ---


def test_reads_module_is_select_only() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_action_reads.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # Rollback only ever ends the read-only snapshot (it
            # persists nothing); commit must never appear here.
            assert node.func.attr != "commit", node.func.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.lstrip().upper().split(None, 1)
            assert not first or not first[0].rstrip(";") in {
                "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                "DROP", "TRUNCATE", "GRANT", "REVOKE",
            }, first


def test_reads_do_not_import_mutation_module() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_action_reads.py").read_text(encoding="utf-8")
    )
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "app.external_actions_db" not in modules, modules


# --- Gated PostgreSQL-backed integration ---


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_cleanup(action_ids, keys):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM external_action_attempts WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM external_actions WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM agent_events WHERE correlation_id = ANY(%(ids)s)",
                {"ids": list(keys)},
            )
        conn.commit()


def _auth(name="operator-alice", sign=True, **overrides):
    import app.external_actions_db as ledger

    base = {
        "idempotency_key": f"key-{uuid4().hex}",
        "action_type": "outreach_email",
        "agent": AgentId.MICHAEL,
        "request": {"template": "owner_intro"},
        "authorized_by": name,
        "authorization_reference": "ticket-123",
    }
    base.update(overrides)
    issued = base.pop("issued_at", None) or datetime.now(timezone.utc)
    expires = base.pop("expires_at", None) or (issued + timedelta(hours=1))
    if sign:
        canonical = canonical_identity_name(base["authorized_by"])
        fingerprint = ledger.canonical_request_fingerprint(
            base["action_type"],
            base["agent"],
            base.get("property_id"),
            base["request"],
            canonical,
            base["authorization_reference"],
        )
        signature = sign_approval(
            private_key_hex=_TEST_CREDS[canonical],
            idempotency_key=base["idempotency_key"],
            request_fingerprint=fingerprint,
            authorized_by=canonical,
            authorization_reference=base["authorization_reference"],
            issued_at=issued,
            expires_at=expires,
        )
    else:
        signature = "00" * 64
    base["authorization_signature"] = signature
    base["authorization_issued_at"] = issued
    base["authorization_expires_at"] = expires
    return base


_ORCH_FOR_TYPE = {"outreach_email": "sched-1", "contract_sign": "sched-2"}


def _live_seed(key, action_type="outreach_email", claim_as=None):
    """Authorize + enqueue on live PG; optionally claim. Returns action_id."""
    import app.external_actions_db as ledger

    created = ledger.record_authorization(**_auth(idempotency_key=key, action_type=action_type))
    action_id = created.action["action_id"]
    orch = _ORCH_FOR_TYPE[action_type]
    ledger.transition_action(
        action_id=action_id, to_state="PENDING",
        orchestrator_id=orch,
        orchestrator_proof=_TEST_ORCHESTRATORS[orch],
    )
    if claim_as is not None:
        ledger.claim_action(
            action_id=action_id, executor_id=claim_as,
            executor_proof=_TEST_EXECUTORS[claim_as],
        )
    return action_id


def _live_seed_unknown(key, action_type="outreach_email", claim_as="w-1"):
    """Authorize, enqueue, claim, and mark UNKNOWN on live PG.

    Returns (action_id, attempt_id). The attempt lookup uses raw
    SQL, independent of the read module under test.
    """
    import app.external_actions_db as ledger
    from app.properties_db import get_connection

    action_id = _live_seed(key, action_type, claim_as=claim_as)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_id FROM external_action_attempts"
                " WHERE action_id = %(id)s",
                {"id": action_id},
            )
            attempt_id = cur.fetchone()["attempt_id"]
    ledger.transition_action(
        action_id=action_id, to_state="UNKNOWN", attempt_id=attempt_id,
        executor_id=claim_as, executor_proof=_TEST_EXECUTORS[claim_as],
    )
    return action_id, attempt_id


def _live_seed_outcome(key, action_type, claim_as, outcome):
    """Authorize, enqueue, claim, and record one outcome on live PG."""
    import app.external_actions_db as ledger
    from app.properties_db import get_connection

    action_id = _live_seed(key, action_type, claim_as=claim_as)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_id FROM external_action_attempts"
                " WHERE action_id = %(id)s",
                {"id": action_id},
            )
            attempt_id = cur.fetchone()["attempt_id"]
    ledger.transition_action(
        action_id=action_id, to_state=outcome, attempt_id=attempt_id,
        executor_id=claim_as, executor_proof=_TEST_EXECUTORS[claim_as],
    )
    return action_id, attempt_id


@needs_live_pg
def test_live_wondrue_and_scoped_reads() -> None:
    _live_config()
    key_a, key_b = f"live-3b3a-{uuid4().hex}", f"live-3b3a-{uuid4().hex}"
    action_a = action_b = None
    try:
        action_a = _live_seed(key_a, "outreach_email", claim_as="w-1")
        action_b = _live_seed(key_b, "contract_sign", claim_as="w-1")
        assert reads.get_action(action_id=action_a, **_wondrue())["state"] == "ATTEMPTING"
        assert reads.get_action(action_id=action_b, **_wondrue())["action_type"] == "contract_sign"
        assert len(reads.list_attempts(action_id=action_a, **_wondrue())) == 1
        assert reads.get_action(action_id=action_b, **_orch("sched-1")) is None
        assert reads.list_attempts(action_id=action_b, **_orch("sched-1")) == []
        found = reads.find_stale_attempting(
            stale_after_seconds=0, **_orch("sched-1")
        )
        assert action_a in [r["action_id"] for r in found]
        assert action_b not in [r["action_id"] for r in found]
    finally:
        _live_cleanup(
            [i for i in (action_a, action_b) if i], [key_a, key_b]
        )


@needs_live_pg
def test_live_executor_ownership() -> None:
    _live_config()
    key_a, key_b = f"live-3b3a-{uuid4().hex}", f"live-3b3a-{uuid4().hex}"
    action_a = action_b = None
    try:
        action_a = _live_seed(key_a, "outreach_email", claim_as="w-1")
        action_b = _live_seed(key_b, "outreach_email", claim_as="w-2")
        own = reads.list_own_open_attempts(**_exe("w-1"))
        assert action_a in [a["action_id"] for a in own]
        assert action_b not in [a["action_id"] for a in own]
        assert reads.list_attempts(action_id=action_a, **_exe("w-2")) == []
        assert reads.get_action(action_id=action_a, **_exe("w-2")) is None
    finally:
        _live_cleanup(
            [i for i in (action_a, action_b) if i], [key_a, key_b]
        )


@needs_live_pg
def test_live_stale_scan_and_summary() -> None:
    _live_config()
    key = f"live-3b3a-{uuid4().hex}"
    action_id = None
    try:
        action_id = _live_seed(key, "outreach_email", claim_as="w-1")
        found = reads.find_stale_attempting(stale_after_seconds=0, **_wondrue())
        mine = [r for r in found if r["action_id"] == action_id]
        assert len(mine) == 1 and 0 <= mine[0]["age_seconds"] < 600
        fresh = reads.find_stale_attempting(**_wondrue())  # 900s default
        assert action_id not in [r["action_id"] for r in fresh]
        summary = reads.stale_summary(stale_after_seconds=0, **_wondrue())
        assert summary["stale_count"] >= 1
        assert summary["oldest_claimed_at"] is not None
        denied = reads.stale_summary(**_orch("sched-denied"))
        assert (denied["stale_count"], denied["oldest_claimed_at"]) == (0, None)
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_unauthorized_indistinguishable_from_missing() -> None:
    _live_config()
    key = f"live-3b3a-{uuid4().hex}"
    action_id = None
    try:
        action_id = _live_seed(key, "contract_sign", claim_as="w-1")
        ghost = uuid4()
        # Outreach-scoped orchestrator: existing-but-ungranted reads as missing.
        assert reads.get_action(action_id=action_id, **_orch("sched-1")) is None
        assert reads.get_action(action_id=ghost, **_orch("sched-1")) is None
        assert reads.list_attempts(action_id=action_id, **_orch("sched-1")) == []
        assert reads.list_attempts(action_id=ghost, **_orch("sched-1")) == []
        # Non-owning executor: same indistinguishability.
        assert reads.get_action(action_id=action_id, **_exe("w-2")) is None
        assert reads.get_action(action_id=ghost, **_exe("w-2")) is None
        # Authorized readers confirm the rows exist.
        assert reads.get_action(action_id=action_id, **_wondrue()) is not None
        assert len(reads.list_attempts(action_id=action_id, **_exe("w-1"))) == 1
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_reads_write_nothing() -> None:
    _live_config()
    from app.properties_db import get_connection

    key = f"live-3b3a-{uuid4().hex}"
    action_id = None
    try:
        action_id = _live_seed(key, "outreach_email", claim_as="w-1")

        def _snapshot():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state, attempt_count FROM external_actions"
                        " WHERE action_id = %(id)s",
                        {"id": action_id},
                    )
                    action = dict(cur.fetchone())
                    cur.execute(
                        "SELECT attempt_id, outcome FROM external_action_attempts"
                        " WHERE action_id = %(id)s",
                        {"id": action_id},
                    )
                    attempts = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT count(*) AS n FROM agent_events WHERE correlation_id = %(key)s",
                        {"key": key},
                    )
                    audits = cur.fetchone()["n"]
            return action, attempts, audits

        before = _snapshot()
        reads.get_action(action_id=action_id, **_wondrue())
        reads.list_attempts(action_id=action_id, **_wondrue())
        reads.list_own_open_attempts(**_exe("w-1"))
        reads.find_stale_attempting(**_wondrue())
        reads.stale_summary(**_wondrue())
        assert _snapshot() == before
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_unknown_queue_and_summary() -> None:
    _live_config()
    key_u1 = f"live-3b3b3a-{uuid4().hex}"
    key_u2 = f"live-3b3b3a-{uuid4().hex}"
    key_att = f"live-3b3b3a-{uuid4().hex}"
    action_u1 = action_u2 = action_att = None
    try:
        action_u1, attempt_u1 = _live_seed_unknown(key_u1, "outreach_email", "w-1")
        action_u2, _ = _live_seed_unknown(key_u2, "contract_sign", "w-1")
        action_att = _live_seed(key_att, "outreach_email", claim_as="w-1")
        queue = reads.list_unknown_actions(**_wondrue())
        mine = [r for r in queue if r["action_id"] in (action_u1, action_u2)]
        assert len(mine) == 2
        assert all(r["outcome"] == "UNKNOWN" for r in mine)
        first = next(r for r in mine if r["action_id"] == action_u1)
        assert first["attempt_id"] == attempt_u1
        assert first["attempt_number"] == 1
        assert first["executor_id"] == "w-1"
        assert first["outcome_recorded_at"] is not None
        # Wire order is oldest-first (by outcome timestamp) with a tie-break.
        order = [(r["outcome_recorded_at"], str(r["action_id"])) for r in queue]
        assert order == sorted(order)
        assert action_att not in [r["action_id"] for r in queue]
        scoped = reads.list_unknown_actions(**_orch("sched-1"))
        assert action_u1 in [r["action_id"] for r in scoped]
        assert action_u2 not in [r["action_id"] for r in scoped]
        assert reads.list_unknown_actions(**_orch("sched-denied")) == []
        summary = reads.unknown_summary(**_wondrue())
        assert summary["unknown_count"] >= 2
        assert summary["oldest_unresolved_at"] is not None
        assert summary["oldest_age_seconds"] >= 0
        denied = reads.unknown_summary(**_orch("sched-denied"))
        assert (denied["unknown_count"], denied["oldest_unresolved_at"]) == (0, None)
        with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
            reads.list_unknown_actions(
                orchestrator_id="w-1", orchestrator_proof=_TEST_EXECUTORS["w-1"]
            )
    finally:
        _live_cleanup(
            [i for i in (action_u1, action_u2, action_att) if i],
            [key_u1, key_u2, key_att],
        )


@needs_live_pg
def test_live_unknown_reads_write_nothing() -> None:
    _live_config()
    from app.properties_db import get_connection

    key = f"live-3b3b3a-{uuid4().hex}"
    action_id = None
    try:
        action_id, _ = _live_seed_unknown(key, "outreach_email", "w-1")

        def _snapshot():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state, attempt_count, updated_at FROM external_actions"
                        " WHERE action_id = %(id)s",
                        {"id": action_id},
                    )
                    action = dict(cur.fetchone())
                    cur.execute(
                        "SELECT attempt_id, outcome, outcome_recorded_at"
                        " FROM external_action_attempts WHERE action_id = %(id)s",
                        {"id": action_id},
                    )
                    attempts = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT count(*) AS n FROM agent_events WHERE correlation_id = %(key)s",
                        {"key": key},
                    )
                    audits = cur.fetchone()["n"]
            return action, attempts, audits

        before = _snapshot()
        assert before[0]["state"] == "UNKNOWN"
        reads.list_unknown_actions(**_wondrue())
        reads.unknown_summary(**_wondrue())
        assert _snapshot() == before
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


def _live_dossier_snapshot(action_id, key):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, attempt_count, updated_at FROM external_actions"
                " WHERE action_id = %(id)s",
                {"id": action_id},
            )
            action = dict(cur.fetchone())
            cur.execute(
                "SELECT attempt_id, outcome, outcome_recorded_at"
                " FROM external_action_attempts WHERE action_id = %(id)s",
                {"id": action_id},
            )
            attempts = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT count(*) AS n FROM agent_events WHERE correlation_id = %(key)s",
                {"key": key},
            )
            audits = cur.fetchone()["n"]
    return action, attempts, audits


@needs_live_pg
def test_live_dossier_unknown_terminal_and_scoped() -> None:
    _live_config()
    key_u = f"live-3b3b3b1-{uuid4().hex}"
    key_t = f"live-3b3b3b1-{uuid4().hex}"
    key_c = f"live-3b3b3b1-{uuid4().hex}"
    action_u = action_t = action_c = None
    try:
        action_u, attempt_u = _live_seed_unknown(key_u, "outreach_email", "w-1")
        action_t, _ = _live_seed_outcome(key_t, "outreach_email", "w-1", "SUCCEEDED")
        action_c, _ = _live_seed_unknown(key_c, "contract_sign", "w-1")
        before = _live_dossier_snapshot(action_u, key_u)
        unknown = reads.get_reconciliation_case(action_id=action_u, **_wondrue())
        assert unknown["interpretation"]["status"] == "unresolved_unknown"
        assert unknown["action"]["state"] == "UNKNOWN"
        assert [a["attempt_id"] for a in unknown["attempts"]] == [attempt_u]
        assert unknown["attempts"][0]["outcome"] == "UNKNOWN"
        assert unknown["reconciliation"] is None
        assert unknown["attempts_truncated"] is False
        assert [e["event_type"] for e in unknown["audits"]] == [
            "external_action.authorized",
            "external_action.transitioned",
            "external_action.claimed",
            "external_action.transitioned",
        ]
        assert unknown["audits_truncated"] is False
        terminal = reads.get_reconciliation_case(action_id=action_t, **_wondrue())
        assert terminal["interpretation"]["status"] == "terminal"
        assert terminal["attempts"][0]["outcome"] == "SUCCEEDED"
        assert terminal["reconciliation"] is None
        # Scoped visibility: outreach sees outreach, not contract_sign.
        assert reads.get_reconciliation_case(
            action_id=action_u, **_orch("sched-1")
        ) is not None
        assert reads.get_reconciliation_case(
            action_id=action_c, **_orch("sched-1")
        ) is None
        assert reads.get_reconciliation_case(
            action_id=action_c, **_orch("sched-2")
        ) is not None
        assert reads.get_reconciliation_case(
            action_id=uuid4(), **_wondrue()
        ) is None
        assert _live_dossier_snapshot(action_u, key_u) == before
    finally:
        _live_cleanup(
            [i for i in (action_u, action_t, action_c) if i],
            [key_u, key_t, key_c],
        )


@needs_live_pg
def test_live_dossier_reconciled(monkeypatch) -> None:
    import app.external_action_reconcile as reconcile
    from app.identity import (
        sign_reconciliation_approval,
    )
    from app.properties_db import get_connection

    _live_config()
    priv, pub = _generate_keypair()
    monkeypatch.setenv("RECONCILER_PUBLIC_KEYS", f"operator-alice:{pub}")
    key = f"live-3b3b3b1-{uuid4().hex}"
    action_id = None
    try:
        action_id, attempt_id = _live_seed_unknown(key, "outreach_email", "w-1")
        observed = datetime.now(timezone.utc) - timedelta(hours=1)
        evidence: dict[str, object] = {
            "kind": "provider_receipt",
            "source": "provider-support-desk",
            "observed_at": observed.isoformat(),
            "asserts_outcome": "SUCCEEDED",
            "artifact_ref": "receipt-live-1",
            "provider_reference": "op-live-1",
            "summary": "Provider confirmed completion by ticket.",
        }
        issued = datetime.now(timezone.utc) - timedelta(minutes=5)
        expires = issued + timedelta(hours=1)
        validated_evidence = reconcile.ReconciliationEvidence(**evidence)
        signature = sign_reconciliation_approval(
            private_key_hex=priv,
            action_id=action_id,
            attempt_id=attempt_id,
            proposed_outcome="SUCCEEDED",
            evidence_digest=validated_evidence.digest(),
            reconciled_by="operator-alice",
            reconciliation_reference="live-recon-1",
            issued_at=issued,
            expires_at=expires,
        )
        result = reconcile.record_reconciliation(
            action_id=action_id,
            attempt_id=attempt_id,
            proposed_outcome="SUCCEEDED",
            evidence=evidence,
            reconciled_by="operator-alice",
            reconciliation_reference="live-recon-1",
            approval_signature=signature,
            approval_issued_at=issued,
            approval_expires_at=expires,
        )
        assert result.created is True
        case = reads.get_reconciliation_case(action_id=action_id, **_wondrue())
        assert case["interpretation"]["status"] == "reconciled"
        assert case["action"]["state"] == "SUCCEEDED"
        assert case["attempts"][0]["outcome"] == "UNKNOWN"  # history preserved
        decision = case["reconciliation"]
        assert decision["decision_id"] == result.decision["decision_id"]
        assert decision["outcome"] == "SUCCEEDED"
        assert decision["reconciled_by"] == "operator-alice"
        assert decision["verification_tier"] == "human_reviewed"
        assert "approval_signature" not in decision
        assert "signing_public_key" not in decision
        assert "external_action.reconciled" in [
            e["event_type"] for e in case["audits"]
        ]
    finally:
        if action_id is not None:
            with get_connection() as conn:  # decisions reference actions
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM reconciliation_decisions"
                        " WHERE action_id = %(id)s",
                        {"id": action_id},
                    )
                conn.commit()
        _live_cleanup([action_id] if action_id else [], [key])
