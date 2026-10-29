"""Identity-review inspection endpoint tests (IR-3B).

Local TestClient coverage with mocked readers and real
X-Intake-Key authentication, plus reader unit tests over a
recording fake connection (SQL shape, truncation, read-only
guarantees). Live PostgreSQL read tests live in
tests/test_identity_reviews.py alongside the queue fixtures.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.routes.identity_reviews as review_routes
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

TEST_INTAKE_KEY = "unit-test-intake-key-only"
_LIST_URL = "/identity-reviews"
_DETAIL_ID = "12345678-1234-5678-1234-567812345678"

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _intake_key_configured(monkeypatch):
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)


def _headers(key: str | None = TEST_INTAKE_KEY) -> dict[str, str] | None:
    if key is None:
        return None
    return {"X-Intake-Key": key}


def _review_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "review_id": uuid4(),
        "dedup_key": '{"v":1}',
        "source": "intake",
        "reason_code": "NAME_CONFLICT",
        "observed_identity": {"name": "Commerce Tower B"},
        "provider": "Harborline Brokerage Records",
        "external_id": None,
        "listing_url": "https://example.com/listings/review-b",
        "conflicting_property_ids": [uuid4()],
        "candidate_key": None,
        "identity_disputes": None,
        "decision_event_id": f"evt-{uuid4().hex}",
        "run_id": f"run-{uuid4().hex}",
        "status": "PENDING",
        "occurrence_count": 1,
        "last_event_id": f"evt-{uuid4().hex}",
        "supersedes_id": None,
        "created_at": _T0,
        "updated_at": _T0,
    }
    row.update(overrides)
    return row


def _detail_row(**overrides: Any) -> dict[str, Any]:
    row = _review_row(**overrides)
    row["occurrences"] = [
        {
            "occurrence_id": uuid4(),
            "event_id": row["decision_event_id"],
            "created_at": _T0,
        }
    ]
    row["occurrences_truncated"] = False
    row["decision_event"] = {
        "event_id": row["decision_event_id"],
        "event_type": "gabriel.intake.decision",
        "outcome": "needs_identity_review",
        "reason_code": "NAME_CONFLICT",
        "occurred_at": _T0,
    }
    return row


# --- Authentication -----------------------------------------------------------


@pytest.mark.parametrize("url", [_LIST_URL, f"{_LIST_URL}/{_DETAIL_ID}"])
def test_missing_key_is_rejected_without_reaching_reader(monkeypatch, url: str) -> None:
    calls: list[dict[str, Any]] = []

    def _boom(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        raise AssertionError("reader must not run unauthenticated")

    monkeypatch.setattr(review_routes, "list_identity_reviews", _boom)
    monkeypatch.setattr(review_routes, "get_identity_review", _boom)
    resp = client.get(url)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert calls == []


@pytest.mark.parametrize("url", [_LIST_URL, f"{_LIST_URL}/{_DETAIL_ID}"])
def test_wrong_key_is_rejected_and_never_echoed(monkeypatch, url: str) -> None:
    monkeypatch.setattr(
        review_routes, "list_identity_reviews", lambda **kwargs: ([], 0)
    )
    monkeypatch.setattr(review_routes, "get_identity_review", lambda *a, **k: None)
    resp = client.get(url, headers=_headers("wrong-key"))
    assert resp.status_code == 401
    assert "wrong-key" not in resp.text
    assert TEST_INTAKE_KEY not in resp.text


@pytest.mark.parametrize("url", [_LIST_URL, f"{_LIST_URL}/{_DETAIL_ID}"])
def test_unconfigured_server_fails_closed(monkeypatch, url: str) -> None:
    monkeypatch.delenv("INTAKE_API_KEY", raising=True)
    resp = client.get(url, headers=_headers())
    assert resp.status_code == 503


# --- List endpoint --------------------------------------------------------------


def test_list_defaults_and_envelope(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return ([_review_row()], 1)

    monkeypatch.setattr(review_routes, "list_identity_reviews", _fake)
    resp = client.get(_LIST_URL, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["reviews"]) == 1
    assert seen == {
        "status": None,
        "source": None,
        "reason_code": None,
        "limit": 50,
        "offset": 0,
    }


def test_list_filters_pass_through_exactly(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def _fake(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return ([], 0)

    monkeypatch.setattr(review_routes, "list_identity_reviews", _fake)
    resp = client.get(
        _LIST_URL,
        params={
            "status": "PENDING",
            "source": "discovery",
            "reason_code": "AMBIGUOUS_ADDRESS",
            "limit": 10,
            "offset": 20,
        },
        headers=_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"reviews": [], "total": 0, "limit": 10, "offset": 20}
    assert seen == {
        "status": "PENDING",
        "source": "discovery",
        "reason_code": "AMBIGUOUS_ADDRESS",
        "limit": 10,
        "offset": 20,
    }


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"status": "BOGUS"},
        {"source": "carrier-pigeon"},
    ],
)
def test_list_rejects_bad_pagination_and_filters(params: dict[str, Any]) -> None:
    resp = client.get(_LIST_URL, params=params, headers=_headers())
    assert resp.status_code == 422


def test_list_database_failures_map_to_503(monkeypatch) -> None:
    for error in (
        DatabaseNotConfiguredError("database_not_configured"),
        DatabaseUnavailableError("database_unavailable"),
    ):
        def _fail(**kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(review_routes, "list_identity_reviews", _fail)
        resp = client.get(_LIST_URL, headers=_headers())
        assert resp.status_code == 503
        assert "secret" not in resp.text.lower()


# --- Detail endpoint --------------------------------------------------------------


def test_detail_returns_case_history_and_provenance(monkeypatch) -> None:
    row = _detail_row()
    monkeypatch.setattr(review_routes, "get_identity_review", lambda *a, **k: row)
    resp = client.get(f"{_LIST_URL}/{row['review_id']}", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["review_id"] == str(row["review_id"])
    assert body["occurrence_count"] == 1
    assert body["occurrences_truncated"] is False
    assert [occ["event_id"] for occ in body["occurrences"]] == [row["decision_event_id"]]
    assert body["decision_event"]["event_type"] == "gabriel.intake.decision"
    # Allowlist: the full event-details payload never leaves the server.
    assert "details" not in body["decision_event"]
    assert set(body["decision_event"]) == {
        "event_id",
        "event_type",
        "outcome",
        "reason_code",
        "occurred_at",
    }


def test_detail_unknown_id_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(review_routes, "get_identity_review", lambda *a, **k: None)
    resp = client.get(f"{_LIST_URL}/{uuid4()}", headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "review_not_found"}


def test_detail_rejects_malformed_uuid(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        review_routes, "get_identity_review", lambda *a, **k: calls.append(1)
    )
    resp = client.get(f"{_LIST_URL}/not-a-uuid", headers=_headers())
    assert resp.status_code == 422
    assert calls == []


def test_detail_database_failures_map_to_503(monkeypatch) -> None:
    for error in (
        DatabaseNotConfiguredError("database_not_configured"),
        DatabaseUnavailableError("database_unavailable"),
    ):
        def _fail(*args: Any, **kwargs: Any) -> Any:
            raise error

        monkeypatch.setattr(review_routes, "get_identity_review", _fail)
        resp = client.get(f"{_LIST_URL}/{_DETAIL_ID}", headers=_headers())
        assert resp.status_code == 503


# --- Cache protection --------------------------------------------------------------


def test_no_store_on_all_review_response_classes(monkeypatch) -> None:
    monkeypatch.setattr(
        review_routes, "list_identity_reviews", lambda **kwargs: ([_review_row()], 1)
    )
    monkeypatch.setattr(review_routes, "get_identity_review", lambda *a, **k: _detail_row())
    cases = [
        client.get(_LIST_URL, headers=_headers()),
        client.get(f"{_LIST_URL}/{_DETAIL_ID}", headers=_headers()),
        client.get(_LIST_URL),
        client.get(f"{_LIST_URL}/not-a-uuid", headers=_headers()),
        client.get(_LIST_URL, params={"limit": 0}, headers=_headers()),
    ]
    assert [resp.status_code for resp in cases] == [200, 200, 401, 422, 422]
    monkeypatch.setattr(review_routes, "get_identity_review", lambda *a, **k: None)
    missing = client.get(f"{_LIST_URL}/{uuid4()}", headers=_headers())
    assert missing.status_code == 404
    for resp in [*cases, missing]:
        assert resp.headers.get("cache-control") == "no-store"


def test_unrelated_routes_untouched_by_no_store() -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") != "no-store"


def test_middleware_path_matching() -> None:
    from app.middleware import _is_identity_review_path

    assert _is_identity_review_path("/identity-reviews")
    assert _is_identity_review_path("/identity-reviews/")
    assert _is_identity_review_path(f"/identity-reviews/{_DETAIL_ID}")
    assert _is_identity_review_path(f"/identity-reviews/{_DETAIL_ID}/")
    assert _is_identity_review_path(f"/identity-reviews/{_DETAIL_ID}/resolve")
    assert _is_identity_review_path(f"/identity-reviews/{_DETAIL_ID}/resolve/")
    assert _is_identity_review_path("/identity-reviews/a/b")
    assert not _is_identity_review_path("/identity-reviews//")
    assert not _is_identity_review_path("/identity-reviews-other")
    assert not _is_identity_review_path("/properties")
    assert not _is_identity_review_path(None)


# --- Resolve endpoint (IR-4B) -----------------------------------------------------------


def _resolve_url(review_id) -> str:
    return f"{_LIST_URL}/{review_id}/resolve"


def _resolve_body(**overrides):
    from datetime import timedelta

    body = {
        "proposed_outcome": "SAME_PROPERTY",
        "expected_occurrence_count": 1,
        "resolved_by": "operator-alice",
        "resolution_reference": "ticket-7",
        "approval_signature": "ab" * 64,
        "approval_issued_at": _T0.isoformat(),
        "approval_expires_at": (_T0 + timedelta(hours=1)).isoformat(),
        "evidence_summary": "reviewed records",
    }
    body.update(overrides)
    return body


def _resolution_dict(**overrides):
    from app.identity_review_resolve import ResolutionResult

    review_id = overrides.pop("review_id", uuid4())
    decision_id = uuid4()
    return ResolutionResult(
        decision={
            "decision_id": decision_id,
            "review_id": review_id,
            "outcome": "SAME_PROPERTY",
            "prior_status": "PENDING",
            "resolved_by": "operator-alice",
            "resolution_reference": "ticket-7",
            "approval_signature": "ab" * 64,
            "signing_public_key": "cd" * 32,
            "signing_key_fingerprint": "sha256:" + "ef" * 32,
            "evidence_digest": "sha256:" + "12" * 32,
            "expected_occurrence_count": 1,
        },
        review={"review_id": review_id, "status": "RESOLVED", "occurrence_count": 1},
        created=overrides.pop("created", True),
        audit_event_id=overrides.pop("audit_event_id", f"evt-{uuid4().hex}"),
    )


def test_resolve_missing_key_never_reaches_writer(monkeypatch) -> None:
    def _boom(**kwargs):
        raise AssertionError("writer must not run unauthenticated")

    monkeypatch.setattr(review_routes, "record_review_resolution", _boom)
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_wrong_key_never_echoed(monkeypatch) -> None:
    monkeypatch.setattr(
        review_routes, "record_review_resolution", lambda **k: _resolution_dict()
    )
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(),
                       headers=_headers("wrong-key"))
    assert resp.status_code == 401
    assert "wrong-key" not in resp.text
    assert TEST_INTAKE_KEY not in resp.text


def test_resolve_unconfigured_key_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("INTAKE_API_KEY", raising=True)
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "intake_not_configured"}


def test_resolve_auth_precedes_malformed_uuid_and_body(monkeypatch) -> None:
    def _boom(**kwargs):
        raise AssertionError("writer must not run unauthenticated")

    monkeypatch.setattr(review_routes, "record_review_resolution", _boom)
    resp = client.post(_resolve_url("not-a-uuid"), json={"bogus": True})
    assert resp.status_code == 401


def test_resolve_rejects_malformed_uuid(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        review_routes, "record_review_resolution",
        lambda **k: calls.append(k) or _resolution_dict(),
    )
    resp = client.post(_resolve_url("not-a-uuid"), json=_resolve_body(),
                       headers=_headers())
    assert resp.status_code == 422
    assert calls == []
    assert resp.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize(
    "override",
    [
        {"proposed_outcome": "MAYBE"},
        {"proposed_outcome": "same_property"},
        {"expected_occurrence_count": 0},
        {"expected_occurrence_count": -2},
        {"expected_occurrence_count": "many"},
        {"resolved_by": "x" * 65},
        {"resolution_reference": ""},
        {"resolution_reference": "x" * 257},
        {"approval_signature": "zz" * 64},
        {"approval_signature": "AB" * 64},
        {"approval_signature": "ab" * 63},
        {"approval_issued_at": "2026-10-28T12:00:00"},
        {"approval_expires_at": "not-a-time"},
        {"evidence_summary": "x" * 501},
        {"review_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_resolve_rejects_invalid_body_shapes(monkeypatch, override) -> None:
    calls: list = []
    monkeypatch.setattr(
        review_routes, "record_review_resolution",
        lambda **k: calls.append(k) or _resolution_dict(),
    )
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(**override),
                       headers=_headers())
    assert resp.status_code == 422, override
    assert calls == []


def test_resolve_forbids_extra_body_fields(monkeypatch) -> None:
    body = _resolve_body()
    body["request_fingerprint"] = "smuggled"
    resp = client.post(_resolve_url(uuid4()), json=body, headers=_headers())
    assert resp.status_code == 422


def _writer_error(monkeypatch, error: Exception) -> None:
    def _fail(**kwargs):
        raise error

    monkeypatch.setattr(review_routes, "record_review_resolution", _fail)


def test_resolve_invalid_approval_maps_to_401(monkeypatch) -> None:
    from app.identity import AuthenticationFailedError

    _writer_error(monkeypatch, AuthenticationFailedError("approval signature mismatch"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid_approval"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_denied_maps_to_403(monkeypatch) -> None:
    from app.identity import AuthorizationDeniedError

    _writer_error(monkeypatch, AuthorizationDeniedError("denied"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "forbidden"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_unknown_review_maps_to_404(monkeypatch) -> None:
    from app.identity_review_resolve import ReviewNotFoundError

    _writer_error(monkeypatch, ReviewNotFoundError("unknown review_id"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "review_not_found"}


def test_resolve_conflict_maps_to_409(monkeypatch) -> None:
    from app.identity_review_resolve import ReviewResolutionConflictError

    _writer_error(monkeypatch, ReviewResolutionConflictError("different decision"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 409
    assert resp.json() == {"detail": "resolution_conflict"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_stale_evidence_maps_to_409(monkeypatch) -> None:
    from app.identity_review_resolve import StaleReviewError

    _writer_error(monkeypatch, StaleReviewError("expected count 1, found 2"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 409
    assert resp.json() == {"detail": "stale_evidence"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_writer_validation_maps_to_422(monkeypatch) -> None:
    _writer_error(monkeypatch, ValueError("invalid proposed_outcome"))
    resp = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_resolution"}
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_database_failures_map_to_503(monkeypatch) -> None:
    for error, detail in (
        (DatabaseNotConfiguredError("x"), "database_not_configured"),
        (DatabaseUnavailableError("x"), "database_unavailable"),
    ):
        _writer_error(monkeypatch, error)
        resp = client.post(_resolve_url(uuid4()), json=_resolve_body(),
                           headers=_headers())
        assert resp.status_code == 503
        assert resp.json() == {"detail": detail}
        assert resp.headers.get("cache-control") == "no-store"


def test_resolve_success_returns_201_and_result_shape(monkeypatch) -> None:
    review_id = uuid4()
    expected = _resolution_dict(review_id=review_id)
    seen: dict = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(review_routes, "record_review_resolution", _fake)
    resp = client.post(_resolve_url(review_id), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 201
    body = resp.json()
    assert body["review_id"] == str(review_id)
    assert body["decision_id"] == str(expected.decision["decision_id"])
    assert body["outcome"] == "SAME_PROPERTY"
    assert body["status"] == "RESOLVED"
    assert body["created"] is True
    assert body["audit_event_id"] == expected.audit_event_id
    assert body["occurrence_count"] == 1
    assert body["resolved_by"] == "operator-alice"
    assert body["resolution_reference"] == "ticket-7"
    assert body["evidence_digest"] == "sha256:" + "12" * 32
    assert set(body) == {
        "review_id", "decision_id", "outcome", "status", "created",
        "audit_event_id", "occurrence_count", "resolved_by",
        "resolution_reference", "evidence_digest",
    }
    # review_id travels once, from the path; the writer gets every body field.
    assert seen["review_id"] == review_id
    assert seen["proposed_outcome"] == "SAME_PROPERTY"
    assert seen["expected_occurrence_count"] == 1
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_replay_returns_200_with_same_decision(monkeypatch) -> None:
    review_id = uuid4()
    expected = _resolution_dict(review_id=review_id, created=False, audit_event_id=None)
    monkeypatch.setattr(
        review_routes, "record_review_resolution", lambda **k: expected
    )
    resp = client.post(_resolve_url(review_id), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["decision_id"] == str(expected.decision["decision_id"])
    assert body["audit_event_id"] is None
    assert resp.headers.get("cache-control") == "no-store"


def test_resolve_responses_carry_no_secrets(monkeypatch) -> None:
    review_id = uuid4()
    expected = _resolution_dict(review_id=review_id)
    monkeypatch.setattr(
        review_routes, "record_review_resolution", lambda **k: expected
    )
    resp = client.post(_resolve_url(review_id), json=_resolve_body(), headers=_headers())
    assert resp.status_code == 201
    assert "ab" * 64 not in resp.text  # submitted approval signature
    assert "cd" * 32 not in resp.text  # signing public key
    assert TEST_INTAKE_KEY not in resp.text


def test_resolve_no_store_on_all_response_classes(monkeypatch) -> None:
    from app.identity_review_resolve import ReviewNotFoundError

    monkeypatch.setattr(
        review_routes, "record_review_resolution", lambda **k: _resolution_dict()
    )
    rid = uuid4()
    ok = client.post(_resolve_url(rid), json=_resolve_body(), headers=_headers())
    bad_uuid = client.post(_resolve_url("not-a-uuid"), json=_resolve_body(),
                           headers=_headers())
    bad_body = client.post(_resolve_url(rid), json={"bogus": True}, headers=_headers())
    no_key = client.post(_resolve_url(rid), json=_resolve_body())
    assert [r.status_code for r in (ok, bad_uuid, bad_body, no_key)] == [201, 422, 422, 401]
    monkeypatch.setattr(
        review_routes, "record_review_resolution",
        lambda **k: (_ for _ in ()).throw(ReviewNotFoundError("nope")),
    )
    missing = client.post(_resolve_url(uuid4()), json=_resolve_body(), headers=_headers())
    assert missing.status_code == 404
    unmatched = client.get(f"{_resolve_url(rid)}/extra", headers=_headers())
    assert unmatched.status_code == 404
    for resp in (ok, bad_uuid, bad_body, no_key, missing, unmatched):
        assert resp.headers.get("cache-control") == "no-store", resp.status_code


def test_resolve_method_not_allowed_is_no_store() -> None:
    resp = client.get(_resolve_url(uuid4()), headers=_headers())
    assert resp.status_code == 405
    assert resp.headers.get("cache-control") == "no-store"


# --- Genuine resolve flow (real writer + real Ed25519, fake connection) --------


class _ResolveFakeCursor:
    def __init__(self, state: dict) -> None:
        self._state = state
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        params = dict(params or {})
        lowered = " ".join(sql.split()).lower()
        self._state["statements"].append(lowered.split(" where ")[0].split(" values ")[0])
        if lowered.startswith("select * from identity_reviews where review_id"):
            self._row = dict(self._state["reviews"].get(str(params["review_id"]) or ""))
            if self._row == {}:
                self._row = None
        elif lowered.startswith("select * from identity_review_decisions where review_id"):
            self._row = dict(self._state["decisions"].get(str(params["review_id"]) or "") or {}) or None
        elif lowered.startswith("insert into identity_review_decisions"):
            rid = str(params["review_id"])
            if rid in self._state["decisions"]:
                self._row = None
            else:
                row = {**params, "decided_at": _T0}
                self._state["decisions"][rid] = row
                self._row = dict(row)
        elif lowered.startswith("update identity_reviews set status"):
            row = self._state["reviews"][str(params["review_id"])]
            row["status"] = params["status"]
            self._row = dict(row)
        elif lowered.startswith("insert into agent_events"):
            self._state["events"][params["event_id"]] = dict(params)
            self._row = None
        else:
            raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def fetchone(self):
        return dict(self._row) if self._row is not None else None


class _ResolveFakeConn:
    def __init__(self, state: dict) -> None:
        self._state = state
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _ResolveFakeCursor(self._state)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _genuine_setup(monkeypatch, occurrence_count: int = 1):
    """Wire the REAL writer to a fake connection with one PENDING review."""
    import app.identity_review_resolve as writer_module
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    priv_hex = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    pub_hex = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    review_id = uuid4()
    state = {
        "reviews": {
            str(review_id): {
                "review_id": str(review_id),
                "status": "PENDING",
                "occurrence_count": occurrence_count,
                "run_id": "run-genuine",
            }
        },
        "decisions": {},
        "events": {},
        "statements": [],
    }
    monkeypatch.setattr(writer_module, "get_connection", lambda config=None: _ResolveFakeConn(state))
    return review_id, priv_hex, state


def _genuine_body(review_id, priv_hex, **overrides):
    from datetime import datetime, timedelta, timezone

    from app.identity import canonical_review_digest, sign_review_approval
    from app.identity_review_resolve import canonical_review_snapshot

    issued = datetime.now(timezone.utc)
    expires = issued + timedelta(hours=1)
    snapshot = canonical_review_snapshot(
        review_id=review_id,
        outcome=overrides.get("proposed_outcome", "SAME_PROPERTY"),
        expected_occurrence_count=overrides.get("expected_occurrence_count", 1),
        summary=overrides.get("evidence_summary", "reviewed records"),
    )
    body = {
        "proposed_outcome": snapshot["outcome"],
        "expected_occurrence_count": snapshot["expected_occurrence_count"],
        "resolved_by": "operator-alice",
        "resolution_reference": "ticket-7",
        "approval_signature": sign_review_approval(
            private_key_hex=priv_hex,
            review_id=review_id,
            proposed_outcome=snapshot["outcome"],
            evidence_digest=canonical_review_digest(snapshot),
            expected_occurrence_count=snapshot["expected_occurrence_count"],
            resolved_by="operator-alice",
            resolution_reference="ticket-7",
            issued_at=issued,
            expires_at=expires,
        ),
        "approval_issued_at": issued.isoformat(),
        "approval_expires_at": expires.isoformat(),
        "evidence_summary": snapshot["summary"],
    }
    return body


def test_genuine_signed_resolution_returns_201(monkeypatch) -> None:
    review_id, priv_hex, state = _genuine_setup(monkeypatch)
    resp = client.post(
        _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
        headers=_headers(),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["review_id"] == str(review_id)
    assert body["created"] is True
    assert body["occurrence_count"] == 1
    assert state["reviews"][str(review_id)]["status"] == "RESOLVED"
    assert len(state["decisions"]) == 1
    assert len(state["events"]) == 1
    # Queue state only: no property, evidence, or occurrence writes.
    joined = " ".join(state["statements"])
    assert "properties" not in joined
    assert "listing_evidence" not in joined
    assert "identity_review_occurrences" not in joined


def test_genuine_forged_signature_returns_401_without_writes(monkeypatch) -> None:
    review_id, _priv_hex, state = _genuine_setup(monkeypatch)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )

    other_priv = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    ).hex()
    resp = client.post(
        _resolve_url(review_id), json=_genuine_body(review_id, other_priv),
        headers=_headers(),
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid_approval"}
    assert state["decisions"] == {}
    assert state["events"] == {}
    assert state["reviews"][str(review_id)]["status"] == "PENDING"


def test_genuine_stale_count_returns_409(monkeypatch) -> None:
    review_id, priv_hex, state = _genuine_setup(monkeypatch, occurrence_count=3)
    resp = client.post(
        _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
        headers=_headers(),
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "stale_evidence"}
    assert state["decisions"] == {}


def test_genuine_replay_returns_200_without_duplicate_writes(monkeypatch) -> None:
    review_id, priv_hex, state = _genuine_setup(monkeypatch)
    first = client.post(
        _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
        headers=_headers(),
    )
    assert first.status_code == 201
    second = client.post(
        _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
        headers=_headers(),
    )
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["decision_id"] == first.json()["decision_id"]
    assert len(state["decisions"]) == 1
    assert len(state["events"]) == 1


# --- Reader units (recording fake connection; no database) ------------------------------


class _RecordingCursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self.statements.append((query, dict(params or {})))

    def fetchall(self) -> list[Any]:
        return list(self._script.pop(0))

    def fetchone(self) -> Any:
        rows = self._script.pop(0)
        return rows[0] if rows else None


class _RecordingConn:
    def __init__(self, script: list[Any]) -> None:
        self.cursor_obj = _RecordingCursor(script)

    def __enter__(self) -> _RecordingConn:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def cursor(self) -> _RecordingCursor:
        return self.cursor_obj


def _reader_conn(monkeypatch, script: list[Any]) -> _RecordingConn:
    from app import identity_reviews_db as reviews_db

    conn = _RecordingConn(script)
    monkeypatch.setattr(reviews_db, "get_connection", lambda config=None: conn)
    return conn


def test_list_reader_builds_filtered_stable_selects(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    row = _review_row()
    conn = _reader_conn(monkeypatch, [[row], [{"n": 3}]])
    reviews, total = reviews_db.list_identity_reviews(
        status="PENDING", source="intake", reason_code="NAME_CONFLICT",
        limit=10, offset=20,
    )
    assert total == 3
    assert reviews[0]["review_id"] == row["review_id"]
    assert len(conn.cursor_obj.statements) == 2
    page_sql, page_params = conn.cursor_obj.statements[0]
    count_sql, count_params = conn.cursor_obj.statements[1]
    for sql in (page_sql, count_sql):
        assert "status = %(filter_status)s" in sql
        assert "source = %(filter_source)s" in sql
        assert "reason_code = %(filter_reason_code)s" in sql
    assert "ORDER BY updated_at DESC, review_id DESC" in page_sql
    assert "LIMIT %(limit)s OFFSET %(offset)s" in page_sql
    assert page_params["filter_status"] == "PENDING"
    assert page_params["limit"] == 10
    assert page_params["offset"] == 20
    assert count_params == {
        "filter_status": "PENDING",
        "filter_source": "intake",
        "filter_reason_code": "NAME_CONFLICT",
    }


def test_list_reader_clamps_bounds_and_skips_absent_filters(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    conn = _reader_conn(monkeypatch, [[], [{"n": 0}]])
    reviews, total = reviews_db.list_identity_reviews(limit=9999, offset=-5)
    assert (reviews, total) == ([], 0)
    page_sql, page_params = conn.cursor_obj.statements[0]
    assert "WHERE" not in page_sql
    assert page_params["limit"] == 200
    assert page_params["offset"] == 0


def test_readers_execute_select_statements_only(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    row = _detail_row()
    conn = _reader_conn(
        monkeypatch,
        [[_review_row()], [{"n": 1}], [row], row["occurrences"], [row["decision_event"]]],
    )
    reviews_db.list_identity_reviews()
    reviews_db.get_identity_review(row["review_id"])
    assert len(conn.cursor_obj.statements) == 5
    for sql, _ in conn.cursor_obj.statements:
        assert sql.lstrip().upper().startswith("SELECT")


def test_detail_reader_orders_and_bounds_occurrences(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    row = _review_row()
    occurrences = [
        {"occurrence_id": uuid4(), "event_id": f"evt-{n}", "created_at": _T0}
        for n in range(201)
    ]
    event = {
        "event_id": row["decision_event_id"],
        "event_type": "gabriel.intake.decision",
        "outcome": "needs_identity_review",
        "reason_code": "NAME_CONFLICT",
        "occurred_at": _T0,
    }
    conn = _reader_conn(monkeypatch, [[row], occurrences, [event]])
    detail = reviews_db.get_identity_review(row["review_id"])
    assert detail is not None
    assert len(detail["occurrences"]) == 200
    assert detail["occurrences_truncated"] is True
    assert detail["decision_event"]["event_id"] == row["decision_event_id"]
    occ_sql, occ_params = conn.cursor_obj.statements[1]
    assert "ORDER BY created_at ASC, occurrence_id ASC" in occ_sql
    assert occ_params["limit"] == 201


def test_detail_reader_returns_none_when_missing(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    conn = _reader_conn(monkeypatch, [[]])
    assert reviews_db.get_identity_review(uuid4()) is None
    assert len(conn.cursor_obj.statements) == 1


def test_detail_reader_fails_closed_without_provenance(monkeypatch) -> None:
    from app import identity_reviews_db as reviews_db

    row = _review_row()
    _reader_conn(monkeypatch, [[row], [], []])
    with pytest.raises(DatabaseUnavailableError):
        reviews_db.get_identity_review(row["review_id"])


def test_readers_wrap_driver_failures(monkeypatch) -> None:
    import psycopg

    from app import identity_reviews_db as reviews_db
    from app.db import DatabaseNotConfiguredError

    class _BoomCursor(_RecordingCursor):
        def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
            raise psycopg.OperationalError("synthetic outage")

    class _BoomConn(_RecordingConn):
        def cursor(self) -> _BoomCursor:
            return _BoomCursor([])

    monkeypatch.setattr(reviews_db, "get_connection", lambda config=None: _BoomConn([]))
    with pytest.raises(DatabaseUnavailableError):
        reviews_db.list_identity_reviews()
    with pytest.raises(DatabaseUnavailableError):
        reviews_db.get_identity_review(uuid4())

    def _missing(config: Any = None) -> Any:
        raise DatabaseNotConfiguredError("database_not_configured")

    monkeypatch.setattr(reviews_db, "get_connection", _missing)
    with pytest.raises(DatabaseNotConfiguredError):
        reviews_db.list_identity_reviews()


# --- Live PostgreSQL HTTP integration (GROWUP_PG_INTEGRATION=1) ----------------
#
# One end-to-end resolve through TestClient against real PostgreSQL:
# genuine Ed25519 approval, real writer transaction, direct row
# assertions, replay idempotency, and table invariance. Self-cleaning
# via try/finally; deletion order follows the foreign keys
# (decisions -> occurrences -> reviews -> events).


_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _live_http_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _require_http_decision_table() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.identity_reviews') AS reviews, "
                "to_regclass('public.identity_review_decisions') AS decisions"
            )
            row = cur.fetchone()
    if row is None or row["reviews"] is None or row["decisions"] is None:
        pytest.skip("identity review tables not present (migrations 009/010 not applied)")


def _live_http_seed(tag: str):
    """Insert one parent event, PENDING review, and occurrence row."""
    from app.properties_db import get_connection

    review_id = uuid4()
    event_id = f"evt-http-resolve-{tag}"
    occ_event_id = f"evt-http-resolve-{tag}-occ"
    pid = uuid4()
    with get_connection() as conn:
        with conn.cursor() as cur:
            for eid in (event_id, occ_event_id):
                cur.execute(
                    "INSERT INTO agent_events (event_id, occurred_at, agent, run_id,"
                    " correlation_id, event_type, severity, summary, details,"
                    " evidence_refs, retention_class)"
                    " VALUES (%(eid)s, now(), 'gabriel', %(run)s, '',"
                    " 'gabriel.intake.decision', 'INFO', 'synthetic http seed',"
                    " '{}', '[]', 'permanent')",
                    {"eid": eid, "run": f"run-http-resolve-{tag}"},
                )
            cur.execute(
                "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code,"
                " observed_identity, conflicting_property_ids, decision_event_id,"
                " run_id, status, occurrence_count, last_event_id)"
                " VALUES (%(rid)s, %(key)s, 'intake', 'AMBIGUOUS_ADDRESS', '{}',"
                " %(pids)s, %(eid)s, %(run)s, 'PENDING', 1, %(eid)s)",
                {
                    "rid": str(review_id),
                    "key": f"live-http-resolve:{tag}",
                    "pids": [str(pid)],
                    "eid": event_id,
                    "run": f"run-http-resolve-{tag}",
                },
            )
            cur.execute(
                "INSERT INTO identity_review_occurrences (occurrence_id, review_id, event_id)"
                " VALUES (%(oid)s, %(rid)s, %(eid)s)",
                {"oid": str(uuid4()), "rid": str(review_id), "eid": occ_event_id},
            )
        conn.commit()
    return review_id, [event_id, occ_event_id]


def _live_http_cleanup(review_ids, event_ids) -> None:
    from app.properties_db import get_connection

    reviews = [str(item) for item in review_ids if item]
    events = [str(item) for item in event_ids if item]
    if not reviews and not events:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            if reviews:
                cur.execute(
                    "DELETE FROM identity_review_decisions WHERE review_id = ANY(%(ids)s)",
                    {"ids": reviews},
                )
                cur.execute(
                    "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(ids)s)",
                    {"ids": reviews},
                )
                cur.execute(
                    "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
                    {"ids": reviews},
                )
            if events:
                cur.execute(
                    "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)",
                    {"ids": events},
                )
        conn.commit()


def _live_http_counts():
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            counts = {}
            for table in ("properties", "listing_evidence", "identity_review_occurrences"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                counts[table] = cur.fetchone()["n"]
    return counts


def _live_http_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )

    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex(),
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
    )


@needs_live_pg
def test_live_resolve_end_to_end(monkeypatch) -> None:
    _live_http_config()
    _require_http_decision_table()
    from app.properties_db import get_connection

    tag = uuid4().hex
    priv_hex, pub_hex = _live_http_keypair()
    monkeypatch.setenv("REVIEWER_PUBLIC_KEYS", f"operator-alice:{pub_hex}")
    review_id, event_ids = _live_http_seed(tag)
    before = _live_http_counts()
    try:
        first = client.post(
            _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
            headers=_headers(),
        )
        assert first.status_code == 201
        created = first.json()
        assert created["created"] is True
        assert created["review_id"] == str(review_id)
        event_ids.append(created["audit_event_id"])
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM identity_review_decisions WHERE review_id = %(rid)s",
                    {"rid": str(review_id)},
                )
                decision = cur.fetchone()
                cur.execute(
                    "SELECT status, occurrence_count FROM identity_reviews"
                    " WHERE review_id = %(rid)s",
                    {"rid": str(review_id)},
                )
                review = cur.fetchone()
                cur.execute(
                    "SELECT event_type, retention_class, property_id FROM agent_events"
                    " WHERE event_id = %(eid)s",
                    {"eid": created["audit_event_id"]},
                )
                audit = cur.fetchone()
        assert decision is not None
        assert str(decision["decision_id"]) == created["decision_id"]
        assert decision["outcome"] == "SAME_PROPERTY"
        assert decision["resolved_by"] == "operator-alice"
        assert decision["evidence_digest"] == created["evidence_digest"]
        assert decision["expected_occurrence_count"] == 1
        assert review["status"] == "RESOLVED"
        assert review["occurrence_count"] == 1
        assert audit["event_type"] == "identity.review.resolved"
        assert audit["retention_class"] == "permanent"
        assert audit["property_id"] is None
        assert _live_http_counts() == before
        # Identical replay: HTTP 200, same decision, zero new rows.
        second = client.post(
            _resolve_url(review_id), json=_genuine_body(review_id, priv_hex),
            headers=_headers(),
        )
        assert second.status_code == 200
        replayed = second.json()
        assert replayed["created"] is False
        assert replayed["decision_id"] == created["decision_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM identity_review_decisions"
                    " WHERE review_id = %(rid)s",
                    {"rid": str(review_id)},
                )
                assert cur.fetchone()["n"] == 1
                cur.execute(
                    "SELECT COUNT(*) AS n FROM agent_events WHERE event_id = ANY(%(ids)s)",
                    {"ids": event_ids},
                )
                assert cur.fetchone()["n"] == len(event_ids)
        assert _live_http_counts() == before
    finally:
        _live_http_cleanup([review_id], event_ids)
