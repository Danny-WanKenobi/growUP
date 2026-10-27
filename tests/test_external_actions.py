"""External-action ledger tests (Phase 3C-3A).

Fake PostgreSQL connections model the UNIQUE-key race honestly
(shared backing store, ON CONFLICT DO NOTHING semantics). Gated
live-PostgreSQL tests (GROWUP_PG_INTEGRATION=1) prove the
database-level guarantees with isolated identifiers and cleanup.
No Brave, no network, no routes.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

import app.external_actions_db as ledger
from app.db import DatabaseUnavailableError
from app.identity import (
    AuthenticationFailedError,
    canonical_identity_name,
    sign_approval,
)
from app.observability import AgentId

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_TEST_OPERATORS = ("operator-alice", "operator-bob")
_TEST_CREDS: dict[str, str] = {}  # canonical name -> private hex, per-test only


def _generate_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


@pytest.fixture(autouse=True)
def _enrolled_test_operators(monkeypatch):
    """Enroll fresh runtime-generated operator keys for every test.

    Private keys exist only in this process and only for the calling
    test (monkeypatch-confined enrollment + cleared dict). ``_auth()``
    signs with them; tests needing no enrollment delete the env vars.
    """
    _TEST_CREDS.clear()
    entries = []
    for name in _TEST_OPERATORS:
        priv, pub = _generate_keypair()
        _TEST_CREDS[name] = priv
        entries.append(f"{name}:{pub}")
    monkeypatch.setenv("AUTHORIZER_PUBLIC_KEYS", ",".join(entries))
    yield
    _TEST_CREDS.clear()


def _auth(name="operator-alice", sign=True, **overrides):
    base = {
        "idempotency_key": f"key-{uuid4().hex}",
        "action_type": "outreach_email",
        "agent": AgentId.MICHAEL,
        "request": {"template": "owner_intro", "property_ref": "prop-1"},
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
        signature = "00" * 64  # never inspected: shape errors precede verification
    base["authorization_signature"] = signature
    base["authorization_issued_at"] = issued
    base["authorization_expires_at"] = expires
    return base


class _LedgerConn:
    """Fake PG connection with a shared action table and op log."""

    def __init__(self, table=None):
        self.rows: dict = table if table is not None else {}
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.commit_fails = False
        self.audit_fails: Exception | None = None
        self.hide_rows = False  # SELECT sees nothing: uncommitted-winner race

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _LedgerCursor(self)

    def commit(self):
        if self.commit_fails:
            raise RuntimeError("simulated commit failure")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


class _LedgerCursor:
    def __init__(self, conn):
        self._conn = conn
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        params = dict(params or {})
        self._conn.statements.append((sql, params))
        if sql.startswith("INSERT INTO external_actions"):
            key = params["idempotency_key"]
            if key in self._conn.rows:
                self._fetch = None  # ON CONFLICT DO NOTHING
            else:
                row = {
                    "action_id": params["action_id"],
                    "idempotency_key": key,
                    "action_type": params["action_type"],
                    "agent": params["agent"],
                    "property_id": params["property_id"],
                    "request_fingerprint": params["request_fingerprint"],
                    "authorized_by": params["authorized_by"],
                    "authorization_reference": params["authorization_reference"],
                    "authorized_at": _T0,
                    "state": "AUTHORIZED",
                    "attempt_count": 0,
                    "created_at": _T0,
                    "updated_at": _T0,
                }
                self._conn.rows[key] = row
                self._fetch = row
        elif sql.startswith("SELECT") and "external_actions" in sql:
            self._fetch = None if self._conn.hide_rows else self._conn.rows.get(
                params["idempotency_key"]
            )
        elif "INTO agent_events" in sql:
            if self._conn.audit_fails is not None:
                raise self._conn.audit_fails
            self._fetch = None
        else:  # pragma: no cover - ledger issues only these statements
            raise AssertionError(f"unexpected SQL: {sql[:60]}")

    def fetchone(self):
        return self._fetch


def _factory(monkeypatch, conn=None, exc=None):
    made: list = []
    conn = conn if conn is not None else _LedgerConn()

    def _make(config=None):
        made.append(config)
        if exc is not None:
            raise exc
        return conn

    monkeypatch.setattr(ledger, "get_connection", _make)
    return conn, made


def _audit_inserts(conn):
    return [(s, p) for s, p in conn.statements if "INTO agent_events" in s]


# --- 1-2. Creation commits action + audit in one transaction ---


def test_successful_authorization_creation(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    prop = uuid4()
    result = ledger.record_authorization(**_auth(property_id=prop))
    assert result.created is True
    action = result.action
    assert action["idempotency_key"].startswith("key-")
    assert action["action_type"] == "outreach_email"
    assert action["agent"] == "michael"
    assert action["property_id"] == prop
    assert action["authorized_by"] == "operator-alice"
    assert action["authorization_reference"] == "ticket-123"
    assert action["state"] == "AUTHORIZED"
    assert action["attempt_count"] == 0
    assert action["request_fingerprint"].startswith("sha256:")
    assert isinstance(action["action_id"], UUID)
    assert len(made) == 1
    assert (conn.commits, conn.rollbacks, conn.closes) == (1, 0, 1)


def test_audit_commits_in_same_transaction(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    ledger.record_authorization(**_auth())
    kinds = [sql for sql, _ in conn.statements]
    assert len(kinds) == 2  # action INSERT, then audit INSERT, then commit
    assert kinds[0].startswith("INSERT INTO external_actions")
    assert "INTO agent_events" in kinds[1]
    assert len(made) == 1 and conn.commits == 1
    (_, params), *_ = _audit_inserts(conn)
    assert params["retention_class"] == "permanent"
    assert params["event_type"] == "external_action.authorized"
    assert params["agent"] == "michael"
    details = json.loads(params["details"])
    assert details["request_fingerprint"].startswith("sha256:")
    assert details["state"] == "AUTHORIZED"
    assert "template" not in params["summary"]  # summary carries no request details


def test_server_derived_state_and_timestamp(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    ledger.record_authorization(**_auth())
    (sql, params), *_ = [
        (s, p) for s, p in conn.statements if s.startswith("INSERT INTO external_actions")
    ]
    assert "'AUTHORIZED'" in sql and "now()" in sql
    assert "state" not in params and "authorized_at" not in params


def test_raw_request_never_persisted(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    marker = "marker-secret-note-77"
    ledger.record_authorization(**_auth(request={"note": marker}))
    blob = json.dumps(conn.statements, default=str)
    assert marker not in blob  # only the fingerprint hash is stored


# --- 3. Audit failure rolls the authorization back ---


def test_audit_failure_rolls_back_authorization(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    with pytest.raises(DatabaseUnavailableError):
        ledger.record_authorization(**_auth())
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


# --- 4-6. Idempotency: replay, conflict, race ---


def test_duplicate_key_identical_request_replays(monkeypatch) -> None:
    table: dict = {}
    conn, _ = _factory(monkeypatch, _LedgerConn(table))
    args = _auth()
    first = ledger.record_authorization(**args)
    second = ledger.record_authorization(**args)
    assert first.created is True and second.created is False
    assert second.action == first.action
    assert len(_audit_inserts(conn)) == 1  # no duplicate audit event
    assert len(table) == 1


def test_replay_tolerates_request_key_reordering(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth(request={"b": 2, "a": 1})
    first = ledger.record_authorization(**args)
    args["request"] = {"a": 1, "b": 2}  # same canonical request
    second = ledger.record_authorization(**args)
    assert second.created is False
    assert second.action["action_id"] == first.action["action_id"]


def test_duplicate_key_conflicting_request_rejected(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth()
    ledger.record_authorization(**args)
    clash = _auth(
        idempotency_key=args["idempotency_key"],
        request={"template": "different_template"},
    )
    with pytest.raises(ledger.IdempotencyConflictError, match="different request"):
        ledger.record_authorization(**clash)
    assert len(_audit_inserts(conn)) == 1
    assert (conn.commits, conn.rollbacks) == (1, 1)


def test_duplicate_key_different_principal_rejected(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth()
    first = ledger.record_authorization(**args)
    clash = _auth("operator-bob", idempotency_key=args["idempotency_key"])
    with pytest.raises(ledger.IdempotencyConflictError, match="different request"):
        ledger.record_authorization(**clash)
    assert len(_audit_inserts(conn)) == 1  # no audit for the rejected claim
    assert first.action["authorized_by"] == "operator-alice"  # original untouched


def test_duplicate_key_different_reference_rejected(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth()
    ledger.record_authorization(**args)
    clash = _auth(
        idempotency_key=args["idempotency_key"], authorization_reference="ticket-999"
    )
    with pytest.raises(ledger.IdempotencyConflictError, match="different request"):
        ledger.record_authorization(**clash)
    assert len(_audit_inserts(conn)) == 1


def test_concurrent_submissions_serialize_on_unique_key(monkeypatch) -> None:
    table: dict = {}  # one shared PostgreSQL table, two connections
    args = _auth()
    first_conn = _LedgerConn(table)
    second_conn = _LedgerConn(table)
    conns = iter([first_conn, second_conn])
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: next(conns))
    first = ledger.record_authorization(**args)
    second = ledger.record_authorization(**args)
    assert (first.created, second.created) == (True, False)
    assert second.action["action_id"] == first.action["action_id"]
    assert len(table) == 1


def test_uncommitted_winner_reports_retryable_race(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    key = f"key-{uuid4().hex}"
    conn.rows[key] = {"request_fingerprint": "sha256:uncommitted-winner"}
    conn.hide_rows = True  # INSERT conflicted, SELECT cannot see the row yet
    with pytest.raises(DatabaseUnavailableError, match="authorization_race"):
        ledger.record_authorization(**_auth(idempotency_key=key))
    assert (conn.commits, conn.rollbacks) == (0, 1)


# --- Fingerprint contract ---


def _fp(**overrides):
    base = {
        "action_type": "t",
        "agent": AgentId.MICHAEL,
        "property_id": None,
        "request": {"z": [1, {"y": None}], "a": "x"},
        "authorized_by": "operator:alice",
        "authorization_reference": "ticket-123",
    }
    base.update(overrides)
    return ledger.canonical_request_fingerprint(
        base["action_type"],
        base["agent"],
        base["property_id"],
        base["request"],
        base["authorized_by"],
        base["authorization_reference"],
    )


def test_fingerprint_is_deterministic_and_binding() -> None:
    prop = uuid4()
    a = _fp(property_id=prop)
    b = _fp(agent="michael", property_id=str(prop), request={"a": "x", "z": [1, {"y": None}]})
    assert a == b and a.startswith("sha256:")
    assert _fp(property_id=prop, action_type="other") != a
    assert _fp(property_id=prop, agent=AgentId.GABRIEL) != a
    assert _fp(property_id=None) != a
    assert _fp(property_id=prop, request={}) != a
    assert _fp(property_id=prop, authorized_by="operator:bob") != a
    assert _fp(property_id=prop, authorization_reference="ticket-999") != a
    with pytest.raises(TypeError):
        _fp(property_id=prop, request=["not-a-dict"])
    with pytest.raises(TypeError):
        _fp(property_id=prop, request={1: "bad-key"})


# --- 7. Missing identity or reference fails before I/O ---


def test_missing_authorization_identity_rejected(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    for field in ("authorized_by", "authorization_reference", "idempotency_key", "action_type"):
        for bad in (None, "", "   "):
            args = _auth(sign=False, **{field: bad})
            with pytest.raises(ValueError):
                ledger.record_authorization(**args)
    assert made == []


def test_oversized_and_mistyped_inputs_rejected(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    with pytest.raises(ValueError):
        ledger.record_authorization(**_auth(sign=False, idempotency_key="k" * 129))
    with pytest.raises(ValueError):
        ledger.record_authorization(**_auth(sign=False, authorized_by="p" * 201))
    with pytest.raises(ValueError):
        ledger.record_authorization(**_auth(sign=False, agent="mallory"))
    with pytest.raises(ValueError):
        ledger.record_authorization(**_auth(sign=False, agent=None))
    with pytest.raises(ValueError):
        ledger.record_authorization(**_auth(sign=False, property_id="not-a-uuid"))
    with pytest.raises(TypeError):
        ledger.record_authorization(**_auth(sign=False, request="not-a-dict"))
    assert made == []


def test_property_id_forms_accepted(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    prop = uuid4()
    assert ledger.record_authorization(**_auth(property_id=str(prop))).action["property_id"] == prop
    assert ledger.record_authorization(**_auth(property_id=None)).action["property_id"] is None
    assert conn.commits == 2


# --- 8. Transition policy rejects by default ---


def test_transition_policy_matrix() -> None:
    allowed = {
        ("AUTHORIZED", "PENDING"),
        ("PENDING", "ATTEMPTING"),
        ("PENDING", "FAILED"),
        ("ATTEMPTING", "SUCCEEDED"),
        ("ATTEMPTING", "FAILED"),
        ("ATTEMPTING", "UNKNOWN"),
        ("FAILED", "PENDING"),
        ("UNKNOWN", "SUCCEEDED"),
        ("UNKNOWN", "FAILED"),
    }
    states = [s.value for s in ledger.ActionState]
    assert len(states) == 6
    for current in states:
        for new in states:
            if (current, new) in allowed:
                ledger.validate_transition(current, new)  # must not raise
            else:
                with pytest.raises(ValueError):
                    ledger.validate_transition(current, new)
    with pytest.raises(ValueError):
        ledger.validate_transition("AUTHORIZED", "BOGUS")
    with pytest.raises(ValueError):
        ledger.validate_transition("BOGUS", "PENDING")


def test_creation_accepts_no_caller_state() -> None:
    import inspect

    assert "state" not in inspect.signature(ledger.record_authorization).parameters
    assert "attempt_count" not in inspect.signature(ledger.record_authorization).parameters


# --- 10-11. PostgreSQL failure fails closed; no SQLite bypass ---


def test_postgresql_unavailable_fails_closed(monkeypatch) -> None:
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not touch SQLite")
        ),
    )
    _, made = _factory(monkeypatch, exc=DatabaseUnavailableError("connection_failed"))
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        ledger.record_authorization(**_auth())
    assert len(made) == 1


def test_healthy_outbox_does_not_authorize(tmp_path, monkeypatch) -> None:
    from app.observability import Outbox

    box = Outbox(tmp_path / "o.sqlite3")
    box.initialize()  # SQLite is available and healthy
    _, _ = _factory(monkeypatch, exc=DatabaseUnavailableError("connection_failed"))
    with pytest.raises(DatabaseUnavailableError):
        ledger.record_authorization(**_auth())
    assert box.count_pending() == 0  # nothing diverted to the outbox


def test_ledger_has_no_fallback_or_network_imports() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_actions_db.py").read_text(encoding="utf-8")
    )
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not {m for m in modules if "outbox" in m or "jsonlog" in m}, modules
    assert not (modules & {"urllib", "requests", "httpx", "smtplib", "socket", "email", "fastapi"}), modules
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names.isdisjoint({"audit_event", "require_durable", "emit_nonfatal"}), names


# --- 12. No execution, no routes ---


def test_no_execution_or_route_surface() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_actions_db.py").read_text(encoding="utf-8")
    )
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names.isdisjoint(
        {"execute_action", "perform_action", "attempt_action", "run_action", "send", "transmit"}
    ), names
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "record_authorization" in defs
    offenders = []
    for source in sorted(Path("app/routes").glob("*.py")):
        text = source.read_text(encoding="utf-8")
        if "external_actions_db" in text or "record_authorization" in text:
            offenders.append(source.name)
    assert offenders == [], offenders


# --- Verified-identity boundary (3C-3B-2A) ---


def test_forged_signature_rejected_before_connection(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    args = _auth()
    donor = _auth()  # transplant a signature bound to a different key
    args["authorization_signature"] = donor["authorization_signature"]
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        ledger.record_authorization(**args)
    assert made == [] and conn.statements == []


def test_unknown_authorizer_rejected_without_mutation(monkeypatch) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    alice_pub = Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(_TEST_CREDS["operator-alice"])
    ).public_key().public_bytes_raw().hex()
    monkeypatch.setenv("AUTHORIZER_PUBLIC_KEYS", f"operator-alice:{alice_pub}")
    conn, made = _factory(monkeypatch)
    args = _auth("operator-bob")  # validly signed, but bob is not enrolled
    with pytest.raises(AuthenticationFailedError, match="unknown authorizer"):
        ledger.record_authorization(**args)
    assert made == [] and conn.statements == []


def test_expired_and_overlong_approvals_rejected(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    now = datetime.now(timezone.utc)
    with pytest.raises(AuthenticationFailedError, match="expired"):
        ledger.record_authorization(
            **_auth(issued_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1))
        )
    with pytest.raises(AuthenticationFailedError, match="maximum TTL"):
        ledger.record_authorization(
            **_auth(issued_at=now, expires_at=now + timedelta(hours=25))
        )
    with pytest.raises(AuthenticationFailedError, match="later than issued"):
        ledger.record_authorization(
            **_auth(issued_at=now, expires_at=now - timedelta(hours=1))
        )
    assert made == [] and conn.statements == []


def test_missing_enrollment_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("AUTHORIZER_PUBLIC_KEYS", raising=False)
    conn, made = _factory(monkeypatch)
    with pytest.raises(AuthenticationFailedError, match="unknown authorizer"):
        ledger.record_authorization(**_auth())
    assert made == [] and conn.statements == []


def test_cross_role_executor_credential_denied(monkeypatch) -> None:
    monkeypatch.setenv("EXECUTOR_KEYS", "w-1:test-only-cross-role-secret-0000")
    conn, made = _factory(monkeypatch)
    args = _auth(sign=False, authorized_by="w-1")
    args["authorization_signature"] = "00" * 64
    with pytest.raises(AuthenticationFailedError, match="unknown authorizer"):
        ledger.record_authorization(**args)
    assert made == [] and conn.statements == []


def test_canonical_replay_across_case_variants(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    key = f"key-{uuid4().hex}"
    first = ledger.record_authorization(
        **_auth(idempotency_key=key, authorized_by="Operator-Alice")
    )
    assert first.action["authorized_by"] == "operator-alice"  # stored canonical
    second = ledger.record_authorization(
        **_auth(idempotency_key=key, authorized_by="operator-alice")
    )
    assert second.created is False
    assert second.action["action_id"] == first.action["action_id"]
    assert len(_audit_inserts(conn)) == 1


def test_identical_approval_replay_returns_existing(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth()
    first = ledger.record_authorization(**args)
    second = ledger.record_authorization(**dict(args))  # byte-identical resubmission
    assert second.created is False
    assert second.action["action_id"] == first.action["action_id"]
    assert len(_audit_inserts(conn)) == 1  # replay writes no duplicate audit


def test_future_issued_approval_rejected(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    now = datetime.now(timezone.utc)
    args = _auth(
        issued_at=now + timedelta(hours=1),
        expires_at=now + timedelta(hours=2),
    )
    with pytest.raises(AuthenticationFailedError, match="issued in the future"):
        ledger.record_authorization(**args)
    assert made == [] and conn.statements == []


def test_proof_material_never_persisted(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    args = _auth()
    ledger.record_authorization(**args)
    blob = json.dumps(conn.statements, default=str)
    assert args["authorization_signature"] not in blob
    assert _TEST_CREDS["operator-alice"] not in blob


# --- Gated PostgreSQL-backed integration ---


def _live_table_ready():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_cleanup(action_ids, event_keys=("action_ids",)):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM external_actions WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
        conn.commit()


def _live_audit_cleanup(correlation_ids):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_events WHERE correlation_id = ANY(%(ids)s)",
                {"ids": list(correlation_ids)},
            )
        conn.commit()


@needs_live_pg
def test_live_expired_approval_leaves_no_row() -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    key = f"live-3c3a-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    with pytest.raises(AuthenticationFailedError, match="expired"):
        ledger.record_authorization(
            **_auth(idempotency_key=key,
                    issued_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1))
        )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM external_actions WHERE idempotency_key = %(key)s",
                {"key": key},
            )
            assert cur.fetchone()["n"] == 0  # denied before any write; nothing to clean


@needs_live_pg
def test_live_create_commits_action_and_audit() -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    key = f"live-3c3a-{uuid4().hex}"
    try:
        result = ledger.record_authorization(**_auth(idempotency_key=key))
        assert result.created is True
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count FROM external_actions WHERE action_id = %(id)s",
                    {"id": result.action["action_id"]},
                )
                row = cur.fetchone()
                assert (row["state"], row["attempt_count"]) == ("AUTHORIZED", 0)
                cur.execute(
                    "SELECT retention_class FROM agent_events WHERE correlation_id = %(key)s",
                    {"key": key},
                )
                audits = cur.fetchall()
                assert [a["retention_class"] for a in audits] == ["permanent"]
    finally:
        _live_cleanup([result.action["action_id"]] if "result" in dir() else [])
        _live_audit_cleanup([key])


@needs_live_pg
def test_live_replay_returns_existing_without_duplicate_audit() -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    key = f"live-3c3a-{uuid4().hex}"
    try:
        first = ledger.record_authorization(**_auth(idempotency_key=key))
        second = ledger.record_authorization(**_auth(idempotency_key=key))
        assert (first.created, second.created) == (True, False)
        assert second.action["action_id"] == first.action["action_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM agent_events WHERE correlation_id = %(key)s",
                    {"key": key},
                )
                assert cur.fetchone()["n"] == 1
    finally:
        _live_cleanup([first.action["action_id"]] if "first" in dir() else [])
        _live_audit_cleanup([key])


@needs_live_pg
def test_live_conflict_rejected_and_single_row_survives() -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    key = f"live-3c3a-{uuid4().hex}"
    try:
        first = ledger.record_authorization(**_auth(idempotency_key=key))
        with pytest.raises(ledger.IdempotencyConflictError):
            ledger.record_authorization(
                **_auth(idempotency_key=key, request={"template": "other"})
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM external_actions WHERE idempotency_key = %(key)s",
                    {"key": key},
                )
                assert cur.fetchone()["n"] == 1
    finally:
        _live_cleanup([first.action["action_id"]] if "first" in dir() else [])
        _live_audit_cleanup([key])


@needs_live_pg
def test_live_audit_failure_rolls_back_action(monkeypatch) -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    key = f"live-3c3a-{uuid4().hex}"
    monkeypatch.setattr(
        ledger, "insert_event_in_transaction",
        lambda conn, event: (_ for _ in ()).throw(
            DatabaseUnavailableError("event_insert_failed")
        ),
    )
    with pytest.raises(DatabaseUnavailableError):
        ledger.record_authorization(**_auth(idempotency_key=key))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM external_actions WHERE idempotency_key = %(key)s",
                {"key": key},
            )
            assert cur.fetchone()["n"] == 0  # rolled back, nothing to clean


@needs_live_pg
def test_live_state_and_attempt_constraints_reject() -> None:
    from app.properties_db import get_connection

    psycopg = pytest.importorskip("psycopg")
    from psycopg.errors import CheckViolation

    _live_table_ready()
    bad_id = uuid4()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                with pytest.raises(CheckViolation):
                    cur.execute(
                        "INSERT INTO external_actions (action_id, idempotency_key, action_type,"
                        " agent, request_fingerprint, authorized_by, authorization_reference,"
                        " authorized_at, state) VALUES (%(id)s, 'live-bad-state', 't', 'michael',"
                        " 'sha256:x', 'op', 'ref', now(), 'EXECUTING')"
                        " ON CONFLICT DO NOTHING",
                        {"id": str(bad_id)},
                    )
                conn.rollback()
                with pytest.raises(CheckViolation):
                    cur.execute(
                        "INSERT INTO external_actions (action_id, idempotency_key, action_type,"
                        " agent, request_fingerprint, authorized_by, authorization_reference,"
                        " authorized_at, attempt_count) VALUES (%(id)s, 'live-bad-count', 't',"
                        " 'michael', 'sha256:x', 'op', 'ref', now(), -1)"
                        " ON CONFLICT DO NOTHING",
                        {"id": str(bad_id)},
                    )
                conn.rollback()
    finally:
        _live_cleanup([bad_id])


@needs_live_pg
def test_live_property_delete_preserves_action_history() -> None:
    from app.properties_db import get_connection

    _live_table_ready()
    prop_id = uuid4()
    key = f"live-3c3a-{uuid4().hex}"
    action_ids: list = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO properties (id, name, category, data_status)"
                    " VALUES (%(id)s, 'ledger-fk-probe', 'other', 'unknown')"
                    " ON CONFLICT (id) DO NOTHING",
                    {"id": str(prop_id)},
                )
            conn.commit()
        result = ledger.record_authorization(
            **_auth(idempotency_key=key, property_id=prop_id)
        )
        action_ids.append(result.action["action_id"])
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM properties WHERE id = %(id)s", {"id": str(prop_id)})
            conn.commit()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT property_id FROM external_actions WHERE action_id = %(id)s",
                    {"id": result.action["action_id"]},
                )
                assert cur.fetchone()["property_id"] is None  # SET NULL, row survives
    finally:
        _live_cleanup(action_ids)
        _live_audit_cleanup([key])
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM properties WHERE id = %(id)s", {"id": str(prop_id)})
            conn.commit()
