"""Review queue endpoint tests (mocked reader; real auth)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.routes.external_actions as queue_route
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity import AuthenticationFailedError, AuthorizationDeniedError
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

_ORCHESTRATORS = {
    "sched-1": "test-only-orchestrator-proof-s1",
    "sched-2": "test-only-orchestrator-proof-s2",
}
_EXECUTORS = {"w-1": "test-only-executor-proof-w-1-aaaaaaaa"}

_QUEUE_URL = "/external-actions/review-queue"


@pytest.fixture(autouse=True)
def _queue_identities(monkeypatch):
    """Enroll orchestrator/executor credentials plus scopes."""
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _ORCHESTRATORS.items()),
    )
    monkeypatch.setenv(
        "EXECUTOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _EXECUTORS.items()),
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_SCOPES",
        "sched-1:outreach_email,sched-2:property_research",
    )
    monkeypatch.setenv("WONDRUE_READER_ID", "sched-1")


def _headers(orchestrator_id="sched-1", proof=None):
    if proof is None:
        proof = _ORCHESTRATORS.get(
            orchestrator_id, _ORCHESTRATORS["sched-1"])
    return {
        "X-Orchestrator-ID": orchestrator_id,
        "X-Orchestrator-Proof": proof,
    }


def _record(action_id=None, action_type="property_research",
            ordinal=0, observed=None):
    aid = action_id or uuid4()
    return {
        "action_id": aid,
        "action_type": action_type,
        # "agent" is not a real reader key; the allowlist must drop
        # it even if a future reader ever emits it.
        "agent": "gabriel",
        "state": "UNKNOWN",
        "attempt_count": ordinal + 1,
        "idempotency_key": f"secret-key-{aid.hex[:8]}",
        "authorized_by": "operator-alice",
        "authorization_reference": "ticket-7",
        "authorized_at": _T0 - timedelta(hours=6),
        "updated_at": _T0 - timedelta(minutes=5),
        "attempt_id": uuid4(),
        "attempt_number": ordinal + 1,
        "executor_id": "w-1-secret-executor",
        "outcome": "UNKNOWN",
        "claimed_at": _T0 - timedelta(hours=2),
        "outcome_recorded_at": (
            observed if observed is not None
            else _T0 - timedelta(hours=ordinal + 1)
        ),
    }


def _mock_queue(monkeypatch, records):
    calls = []

    def _fake(**kwargs):
        calls.append(kwargs)
        return records

    monkeypatch.setattr(queue_route, "list_unknown_actions", _fake)
    return calls


# --- Success and shaping ---


def test_wondrue_queue_access(monkeypatch) -> None:
    first = _record(action_type="outreach_email", ordinal=1)
    second = _record(action_type="property_research", ordinal=0)
    calls = _mock_queue(monkeypatch, [first, second])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert set(body) == {"cases", "count", "limit"}
    assert body["count"] == 2
    assert body["limit"] == 50
    assert [case["action_id"] for case in body["cases"]] == [
        str(first["action_id"]), str(second["action_id"])]
    assert calls == [{
        "orchestrator_id": "sched-1",
        "orchestrator_proof": _ORCHESTRATORS["sched-1"],
        "limit": 50,
    }]
    json.dumps(body)  # response is JSON-native


def test_scoped_orchestrator_passes_credentials_through(monkeypatch) -> None:
    # Scope filtering itself lives in list_unknown_actions (covered by
    # the reader tests); the route forwards the identity verbatim and
    # shapes whatever the reader returns.
    record = _record(action_type="property_research")
    calls = _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers("sched-2"))
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert calls[0]["orchestrator_id"] == "sched-2"
    assert calls[0]["orchestrator_proof"] == _ORCHESTRATORS["sched-2"]


def test_empty_queue_returns_empty_200(monkeypatch) -> None:
    calls = _mock_queue(monkeypatch, [])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"cases": [], "count": 0, "limit": 50}
    assert resp.headers["cache-control"] == "no-store"
    assert len(calls) == 1


def test_exact_response_field_allowlist(monkeypatch) -> None:
    record = _record()
    _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"cases", "count", "limit"}
    assert set(body["cases"][0]) == {
        "action_id", "action_type", "state", "attempt_count",
        "awaiting_review_since",
    }
    blob = resp.text
    for secret in (record["idempotency_key"], "operator-alice",
                   "ticket-7", "w-1-secret-executor",
                   str(record["attempt_id"]),
                   _ORCHESTRATORS["sched-1"], "gabriel"):
        assert secret not in blob
    for absent in ("agent", "has_more", "idempotency_key", "executor_id",
                   "authorized_by", "authorization_reference",
                   "attempt_id"):
        assert absent not in blob
    for header in ("X-Orchestrator-ID", "X-Orchestrator-Proof"):
        assert header.lower() not in {k.lower() for k in resp.headers}


def test_uuid_and_timestamp_serialization(monkeypatch) -> None:
    aid = UUID("00000000-0000-4000-8000-000000000001")
    plain = _record(action_id=aid, observed=datetime(
        2026, 10, 27, 18, 30, 0, tzinfo=timezone.utc))
    micro = _record(observed=datetime(
        2026, 10, 27, 18, 31, 0, 123456, tzinfo=timezone.utc))
    offset = _record(observed=datetime(
        2026, 10, 28, 7, 30, 0,
        tzinfo=timezone(timedelta(hours=-5))))
    offset_micro = _record(observed=datetime(
        2026, 10, 28, 7, 30, 0, 500000,
        tzinfo=timezone(timedelta(hours=-5))))
    _mock_queue(monkeypatch, [plain, micro, offset, offset_micro])
    resp = client.get(_QUEUE_URL, headers=_headers())
    cases = resp.json()["cases"]
    assert cases[0]["action_id"] == "00000000-0000-4000-8000-000000000001"
    assert cases[0]["awaiting_review_since"] == "2026-10-27T18:30:00Z"
    assert cases[1]["awaiting_review_since"] == "2026-10-27T18:31:00.123456Z"
    assert cases[2]["awaiting_review_since"] == "2026-10-28T12:30:00Z"
    assert cases[3]["awaiting_review_since"] == "2026-10-28T12:30:00.500000Z"


def test_reader_ordering_preserved_verbatim(monkeypatch) -> None:
    records = [_record(ordinal=n) for n in (2, 0, 1)]
    _mock_queue(monkeypatch, records)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert [case["action_id"] for case in resp.json()["cases"]] == [
        str(record["action_id"]) for record in records]


# --- Malformed records fail closed ---


@pytest.mark.parametrize("mode", ["absent", "none"])
def test_missing_timestamp_maps_to_503(monkeypatch, mode) -> None:
    record = _record()
    if mode == "absent":
        del record["outcome_recorded_at"]
    else:
        record["outcome_recorded_at"] = None
    calls = _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    assert len(calls) == 1  # retrieved, then rejected at presentation


@pytest.mark.parametrize("observed", ["marker-bad-timestamp-zzz", 12345])
def test_non_datetime_timestamp_maps_to_503(monkeypatch, observed) -> None:
    record = _record(observed=observed)
    calls = _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    assert "marker-bad-timestamp" not in resp.text
    assert "queue_record_invalid" not in resp.text
    assert len(calls) == 1


def test_naive_timestamp_maps_to_503(monkeypatch) -> None:
    record = _record(observed=datetime(2026, 10, 27, 18, 30, 0))
    calls = _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["SUCCEEDED", "PENDING"])
def test_non_unknown_state_maps_to_503(monkeypatch, state) -> None:
    record = _record()
    record["state"] = state
    calls = _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "database_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    assert len(calls) == 1


def test_malformed_record_fails_entire_queue(monkeypatch) -> None:
    bad = _record(observed=datetime(2026, 10, 27, 18, 30, 0))
    records = [_record(ordinal=0), bad, _record(ordinal=2)]
    calls = _mock_queue(monkeypatch, records)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    # Exact error body: no partial cases leak alongside the failure.
    assert resp.json() == {"detail": "database_unavailable"}
    assert resp.headers["cache-control"] == "no-store"
    assert len(calls) == 1


# --- Authentication ---


def test_missing_credentials_return_401(monkeypatch) -> None:
    calls = _mock_queue(monkeypatch, [_record()])
    assert client.get(_QUEUE_URL).status_code == 401
    assert client.get(
        _QUEUE_URL, headers={"X-Orchestrator-ID": "sched-1"}
    ).status_code == 401
    assert client.get(
        _QUEUE_URL,
        headers={"X-Orchestrator-Proof": _ORCHESTRATORS["sched-1"]},
    ).status_code == 401
    assert client.get(
        _QUEUE_URL,
        headers={"X-Orchestrator-ID": "sched-1", "X-Orchestrator-Proof": "  "},
    ).status_code == 401
    resp = client.get(_QUEUE_URL)
    assert resp.json() == {"detail": "unauthorized"}
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []  # rejected before any queue retrieval


def test_invalid_credentials_return_401(monkeypatch) -> None:
    calls = _mock_queue(monkeypatch, [_record()])
    assert client.get(
        _QUEUE_URL, headers=_headers(proof="wrong-proof-value-0000")
    ).status_code == 401
    assert client.get(
        _QUEUE_URL, headers=_headers(orchestrator_id="ghost-9")
    ).status_code == 401
    assert calls == []


def test_executor_credentials_rejected(monkeypatch) -> None:
    calls = _mock_queue(monkeypatch, [_record()])
    resp = client.get(_QUEUE_URL, headers={
        "X-Orchestrator-ID": "w-1",
        "X-Orchestrator-Proof": _EXECUTORS["w-1"],
    })
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []  # executor ids never verify as orchestrators


# --- Limits ---


@pytest.mark.parametrize("limit", [1, 7, 200])
def test_valid_limits_forwarded_and_echoed(monkeypatch, limit) -> None:
    calls = _mock_queue(monkeypatch, [])
    resp = client.get(f"{_QUEUE_URL}?limit={limit}", headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["limit"] == limit
    assert calls[0]["limit"] == limit


@pytest.mark.parametrize("limit", ["0", "201", "-5", "abc", "", "50.5"])
def test_invalid_limits_rejected_before_database(monkeypatch, limit) -> None:
    calls = _mock_queue(monkeypatch, [_record()])
    resp = client.get(f"{_QUEUE_URL}?limit={limit}", headers=_headers())
    assert resp.status_code == 422
    assert isinstance(resp.json().get("detail"), list)  # framework shape
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []


# --- Error mapping ---


@pytest.mark.parametrize("error,detail", [
    (DatabaseNotConfiguredError("nope"), "database_not_configured"),
    (DatabaseUnavailableError("inconsistent"), "database_unavailable"),
])
def test_database_errors_map_to_503(monkeypatch, error, detail) -> None:
    # DatabaseUnavailableError also carries the reader's
    # inconsistent-record fail-closed signal; the queue reports it
    # as 503 without exposing which record tripped it.
    def _failing(**kwargs):
        raise error

    monkeypatch.setattr(queue_route, "list_unknown_actions", _failing)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": detail}
    assert resp.headers["cache-control"] == "no-store"
    assert "nope" not in resp.text and "inconsistent" not in resp.text


def test_authorization_denied_maps_to_403(monkeypatch) -> None:
    def _denied(**kwargs):
        raise AuthorizationDeniedError("scope map malformed")

    monkeypatch.setattr(queue_route, "list_unknown_actions", _denied)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "forbidden"}
    assert resp.headers["cache-control"] == "no-store"
    assert "malformed" not in resp.text


def test_reader_authentication_failure_maps_to_401(monkeypatch) -> None:
    def _denied(**kwargs):
        raise AuthenticationFailedError("wondrue designation invalid")

    monkeypatch.setattr(queue_route, "list_unknown_actions", _denied)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert resp.headers["cache-control"] == "no-store"


def test_queue_framework_405_gains_no_store() -> None:
    for method in ("post", "put", "delete", "patch"):
        resp = getattr(client, method)(_QUEUE_URL)
        assert resp.status_code == 405
        assert resp.json() == {"detail": "Method Not Allowed"}
        assert "GET" in resp.headers["allow"]  # unrelated header kept
        assert resp.headers["cache-control"] == "no-store"


def test_no_store_on_every_queue_response(monkeypatch) -> None:
    _mock_queue(monkeypatch, [_record()])
    assert client.get(_QUEUE_URL, headers=_headers()).headers[
        "cache-control"] == "no-store"
    assert client.get(_QUEUE_URL).headers["cache-control"] == "no-store"
    assert client.get(
        f"{_QUEUE_URL}?limit=0", headers=_headers()).headers[
        "cache-control"] == "no-store"


# --- Routing and posture ---


def test_queue_path_not_swallowed_by_dynamic_route(monkeypatch) -> None:
    record = _record()
    _mock_queue(monkeypatch, [record])
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"cases", "count", "limit"}
    assert body["cases"][0]["action_id"] == str(record["action_id"])


def test_individual_review_route_still_works(monkeypatch) -> None:
    def _bomb(**kwargs):
        raise AssertionError("queue reader must not run here")

    monkeypatch.setattr(queue_route, "list_unknown_actions", _bomb)
    monkeypatch.setattr(
        queue_route, "get_reconciliation_case",
        lambda **kwargs: {"sentinel": "dossier"},
    )
    monkeypatch.setattr(
        queue_route, "build_reconciliation_review",
        lambda dossier: {"mock": "report", "seen": dossier},
    )
    resp = client.get(
        f"/external-actions/{uuid4()}/review", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"mock": "report", "seen": {"sentinel": "dossier"}}
    assert resp.headers["cache-control"] == "no-store"


def test_queue_performs_single_read_only_call(monkeypatch) -> None:
    calls = _mock_queue(monkeypatch, [_record()])

    def _bomb(**kwargs):
        raise AssertionError("individual review must not run here")

    monkeypatch.setattr(queue_route, "get_reconciliation_case", _bomb)
    monkeypatch.setattr(queue_route, "build_reconciliation_review", _bomb)
    resp = client.get(_QUEUE_URL, headers=_headers())
    assert resp.status_code == 200
    # One queue-reader call, no other data access; the module-wide
    # structural test in the sibling review-route file additionally
    # forbids SQL, commits, and network calls in this module.
    assert len(calls) == 1
