"""Identity-review inspection endpoint tests (IR-3B).

Local TestClient coverage with mocked readers and real
X-Intake-Key authentication, plus reader unit tests over a
recording fake connection (SQL shape, truncation, read-only
guarantees). Live PostgreSQL read tests live in
tests/test_identity_reviews.py alongside the queue fixtures.
"""

from __future__ import annotations

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
    assert not _is_identity_review_path("/identity-reviews//")
    assert not _is_identity_review_path("/identity-reviews/a/b")
    assert not _is_identity_review_path("/identity-reviews-other")
    assert not _is_identity_review_path("/properties")
    assert not _is_identity_review_path(None)


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
