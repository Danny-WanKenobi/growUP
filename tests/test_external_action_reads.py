"""Read-only external-action inspection tests (Phase 3C-3B-3A).

Self-contained SELECT-only fake PostgreSQL: the fake raises on any
non-SELECT statement, so every test structurally proves reads never
write. Rows are hand-crafted to ledger shapes (including stale
timestamps no live claim could produce); gated live-PostgreSQL
tests (GROWUP_PG_INTEGRATION=1, migrations 005+006 applied) seed
through the real ledger and prove SQL correctness plus shape
compatibility for real. No execution, no routes, no state changes.
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


class _ReadConn:
    def __init__(self, tables):
        self.tables = tables
        self.statements: list = []
        self.commits = 0
        self.closes = 0

    def cursor(self):
        return _ReadCursor(self)

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
        if "count(*)" in sql:  # stale_summary aggregate
            matches = _stale_matches(tables, params)
            oldest = min((m["claimed_at"] for m in matches), default=None)
            self._rows = [{"stale_count": len(matches), "oldest_claimed_at": oldest}]
        elif "a.state = 'ATTEMPTING'" in sql:  # find_stale_attempting scan
            matches = _stale_matches(tables, params)
            matches.sort(key=lambda m: (m["claimed_at"], str(m["attempt_id"])))
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
        elif sql.startswith("SELECT a.action_id"):  # get_action (auth in SQL)
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
            found.sort(key=lambda m: (m["attempt_number"], str(m["attempt_id"])))
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
    before = (copy.deepcopy(tables.by_id), copy.deepcopy(tables.attempts))
    reads.get_action(action_id=ids["a"], **_wondrue())
    reads.list_attempts(action_id=ids["a"], **_wondrue())
    reads.list_attempts(action_id=ids["a"], **_exe("w-1"))
    reads.list_own_open_attempts(**_exe("w-1"))
    reads.find_stale_attempting(now=_T0, **_wondrue())
    reads.stale_summary(now=_T0, **_wondrue())
    assert tables.by_id == before[0] and tables.attempts == before[1]
    blob = json.dumps([s for c in conns for s in c.statements], default=str)
    assert "agent_events" not in blob  # no audit writes during inspection
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


# --- Structural read-only guards ---


def test_reads_module_is_select_only() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_action_reads.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"commit", "rollback"}, node.func.attr
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
