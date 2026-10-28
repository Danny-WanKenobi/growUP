"""Review endpoint and path-scoped no-store middleware tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.routes.external_actions as review_route
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity import AuthenticationFailedError, AuthorizationDeniedError
from app.main import app
from app.middleware import ReviewNoStoreMiddleware

client = TestClient(app, raise_server_exceptions=False)

_T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

_ORCHESTRATORS = {"sched-1": "test-only-orchestrator-proof-s1"}
_EXECUTORS = {"w-1": "test-only-executor-proof-w-1-aaaaaaaa"}


@pytest.fixture(autouse=True)
def _review_identities(monkeypatch):
    """Enroll orchestrator/executor credentials plus scopes."""
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _ORCHESTRATORS.items()),
    )
    monkeypatch.setenv(
        "EXECUTOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _EXECUTORS.items()),
    )
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:outreach_email")
    monkeypatch.setenv("WONDRUE_READER_ID", "sched-1")


def _headers(orchestrator_id="sched-1", proof=None):
    return {
        "X-Orchestrator-ID": orchestrator_id,
        "X-Orchestrator-Proof": (
            proof if proof is not None else _ORCHESTRATORS["sched-1"]
        ),
    }


def _dossier(action_id, state="UNKNOWN", status="unresolved_unknown"):
    aid = action_id if isinstance(action_id, UUID) else UUID(action_id)
    tid = uuid4()
    return {
        "action": {
            "action_id": aid,
            "idempotency_key": f"key-{aid.hex[:12]}",
            "action_type": "outreach_email",
            "agent": "michael",
            "property_id": None,
            "request_fingerprint": "sha256:test-fixture",
            "authorized_by": "operator-alice",
            "authorization_reference": "ticket-1",
            "authorized_at": _T0 - timedelta(hours=4),
            "state": state,
            "attempt_count": 1,
            "created_at": _T0 - timedelta(hours=4),
            "updated_at": _T0 - timedelta(hours=1),
        },
        "attempts": [{
            "attempt_id": tid,
            "action_id": aid,
            "attempt_number": 1,
            "executor_id": "w-1",
            "claimed_at": _T0 - timedelta(hours=2),
            "outcome": "UNKNOWN",
            "outcome_recorded_at": _T0 - timedelta(hours=1),
            "created_at": _T0 - timedelta(hours=2),
            "updated_at": _T0 - timedelta(hours=1),
        }],
        "attempts_truncated": False,
        "reconciliation": None,
        "audits": [{
            "event_id": "evt-1",
            "occurred_at": _T0 - timedelta(hours=3),
            "event_type": "external_action.authorized",
            "severity": "INFO",
            "summary": "Authorized outreach_email action",
        }],
        "audits_truncated": False,
        "interpretation": {"status": status, "detail": "dossier detail"},
        "generated_at": _T0,
    }


def _mock_reader(monkeypatch, dossier):
    calls = []

    def _fake(*, action_id, orchestrator_id, orchestrator_proof, **kwargs):
        calls.append({
            "action_id": action_id,
            "orchestrator_id": orchestrator_id,
            "orchestrator_proof": orchestrator_proof,
        })
        return dossier

    monkeypatch.setattr(review_route, "get_reconciliation_case", _fake)
    return calls


def _url(action_id):
    return f"/external-actions/{action_id}/review"


# --- Success and sanitization ---


def test_valid_request_returns_sanitized_report(monkeypatch) -> None:
    aid = uuid4()
    dossier = _dossier(aid)
    dossier["reconciliation"] = None
    calls = _mock_reader(monkeypatch, dossier)
    resp = client.get(_url(aid), headers=_headers())
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert set(body) == {
        "case_summary", "authorization", "execution_history",
        "evidence_assessment", "audit_timeline", "review_guidance",
        "readiness",
    }
    assert body["case_summary"]["action_id"] == str(aid)
    assert body["readiness"]["status"] == "ready_for_human_review"
    assert len(calls) == 1  # exactly one dossier retrieval
    assert calls[0]["action_id"] == aid
    assert calls[0]["orchestrator_id"] == "sched-1"
    assert calls[0]["orchestrator_proof"] == _ORCHESTRATORS["sched-1"]
    json.dumps(body)  # response is JSON-native


def test_wondrue_reader_accesses_case(monkeypatch) -> None:
    aid = uuid4()
    _mock_reader(monkeypatch, _dossier(aid))
    resp = client.get(_url(aid), headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["case_summary"]["agent"] == "michael"


def test_out_of_scope_reads_as_not_found(monkeypatch) -> None:
    _mock_reader(monkeypatch, None)  # reader hides unauthorized actions
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "review_not_found"}
    assert resp.headers["cache-control"] == "no-store"


def test_missing_and_unknown_actions_indistinguishable(monkeypatch) -> None:
    _mock_reader(monkeypatch, None)
    first = client.get(_url(uuid4()), headers=_headers())
    second = client.get(_url(uuid4()), headers=_headers())
    assert (first.status_code, first.json(), first.headers["cache-control"]) == (
        second.status_code, second.json(), second.headers["cache-control"],
    ) == (404, {"detail": "review_not_found"}, "no-store")


def test_credentials_and_raw_dossier_never_in_response(monkeypatch) -> None:
    aid = uuid4()
    dossier = _dossier(aid)
    dossier["action"]["executor_proof"] = "supersecret-proof"
    dossier["reconciliation"] = {
        "decision_id": uuid4(),
        "attempt_id": dossier["attempts"][0]["attempt_id"],
        "outcome": "SUCCEEDED",
        "prior_outcome": "UNKNOWN",
        "reconciled_by": "operator-alice",
        "reconciliation_reference": "recon-1",
        "approval_signature": "ab" * 64,
        "signing_public_key": "cd" * 32,
        "evidence_kind": "provider_receipt",
        "evidence_source": "provider-support-desk",
        "evidence_observed_at": _T0 - timedelta(hours=2),
        "evidence_digest": "sha256:" + "12" * 32,
        "evidence_summary": "Provider confirmed completion.",
        "artifact_ref": "receipt-8842",
        "provider_reference": None,
        "storage_ref": None,
        "verification_tier": "human_reviewed",
        "decided_at": _T0 - timedelta(minutes=30),
    }
    dossier["action"]["state"] = "SUCCEEDED"
    dossier["interpretation"]["status"] = "reconciled"
    _mock_reader(monkeypatch, dossier)
    resp = client.get(_url(aid), headers=_headers())
    assert resp.status_code == 200
    blob = resp.text
    for secret in ("supersecret-proof", "ab" * 64, "cd" * 32,
                   _ORCHESTRATORS["sched-1"], "sched-1-proof"):
        assert secret not in blob
    assert "approval_signature" not in blob
    assert "signing_public_key" not in blob
    for header in ("X-Orchestrator-ID", "X-Orchestrator-Proof"):
        assert header.lower() not in {k.lower() for k in resp.headers}


# --- Authentication ---


def test_missing_authentication_returns_401(monkeypatch) -> None:
    calls = _mock_reader(monkeypatch, _dossier(uuid4()))
    aid = uuid4()
    assert client.get(_url(aid)).status_code == 401
    assert client.get(
        _url(aid), headers={"X-Orchestrator-ID": "sched-1"}
    ).status_code == 401
    assert client.get(
        _url(aid),
        headers={"X-Orchestrator-Proof": _ORCHESTRATORS["sched-1"]},
    ).status_code == 401
    assert client.get(
        _url(aid),
        headers={"X-Orchestrator-ID": "sched-1", "X-Orchestrator-Proof": "  "},
    ).status_code == 401
    resp = client.get(_url(aid))
    assert resp.json() == {"detail": "unauthorized"}
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []  # rejected before any dossier retrieval


def test_invalid_credentials_return_401(monkeypatch) -> None:
    calls = _mock_reader(monkeypatch, _dossier(uuid4()))
    aid = uuid4()
    assert client.get(
        _url(aid), headers=_headers(proof="wrong-proof-value-0000")
    ).status_code == 401
    assert client.get(
        _url(aid), headers=_headers(orchestrator_id="ghost-9")
    ).status_code == 401
    assert calls == []


def test_executor_credentials_rejected(monkeypatch) -> None:
    calls = _mock_reader(monkeypatch, _dossier(uuid4()))
    resp = client.get(_url(uuid4()), headers={
        "X-Orchestrator-ID": "w-1",
        "X-Orchestrator-Proof": _EXECUTORS["w-1"],
    })
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}
    assert calls == []  # executor ids never verify as orchestrators


def test_credentials_ignored_outside_headers(monkeypatch) -> None:
    calls = _mock_reader(monkeypatch, _dossier(uuid4()))
    aid = uuid4()
    resp = client.get(
        f"/external-actions/{aid}/review"
        f"?orchestrator_id=sched-1&proof={_ORCHESTRATORS['sched-1']}"
    )
    assert resp.status_code == 401
    assert calls == []


# --- Error mapping ---


def test_malformed_uuid_rejected_before_database(monkeypatch) -> None:
    calls = _mock_reader(monkeypatch, _dossier(uuid4()))
    resp = client.get(
        "/external-actions/not-a-uuid/review", headers=_headers()
    )
    assert resp.status_code == 422
    assert isinstance(resp.json().get("detail"), list)  # framework shape kept
    assert resp.headers["cache-control"] == "no-store"
    assert calls == []


def test_review_conflict_maps_to_409(monkeypatch) -> None:
    dossier = _dossier(uuid4())
    dossier["action"]["state"] = "ATTEMPTING"  # contradicts UNKNOWN status
    _mock_reader(monkeypatch, dossier)
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 409
    assert resp.json() == {"detail": "review_conflict"}
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error,detail", [
    (DatabaseNotConfiguredError("nope"), "database_not_configured"),
    (DatabaseUnavailableError("down"), "database_unavailable"),
])
def test_database_errors_map_to_503(monkeypatch, error, detail) -> None:
    def _failing(**kwargs):
        raise error

    monkeypatch.setattr(review_route, "get_reconciliation_case", _failing)
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 503
    assert resp.json() == {"detail": detail}
    assert resp.headers["cache-control"] == "no-store"
    assert "nope" not in resp.text and "down" not in resp.text


def test_authorization_denied_maps_to_404(monkeypatch) -> None:
    def _denied(**kwargs):
        raise AuthorizationDeniedError("scope map malformed")

    monkeypatch.setattr(review_route, "get_reconciliation_case", _denied)
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 404
    assert resp.json() == {"detail": "review_not_found"}
    assert "malformed" not in resp.text


def test_reader_authentication_failure_maps_to_401(monkeypatch) -> None:
    def _denied(**kwargs):
        raise AuthenticationFailedError("unknown orchestrator")

    monkeypatch.setattr(review_route, "get_reconciliation_case", _denied)
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


def test_no_store_on_every_endpoint_response(monkeypatch) -> None:
    _mock_reader(monkeypatch, _dossier(uuid4()))
    aid = uuid4()
    assert client.get(_url(aid), headers=_headers()).headers[
        "cache-control"] == "no-store"
    assert client.get(_url(aid)).headers["cache-control"] == "no-store"
    _mock_reader(monkeypatch, None)
    assert client.get(_url(aid), headers=_headers()).headers[
        "cache-control"] == "no-store"


# --- Posture ---


def test_route_module_is_thin_and_read_only() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/routes/external_actions.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app."):
            assert node.module in {
                "app.db",
                "app.external_action_reads",
                "app.external_action_review",
                "app.identity",
            }, node.module
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "commit", "execute", "open", "connect", "request", "send",
            }, node.func.attr
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            first = node.value.lstrip().upper().split(None, 1)
            assert not first or first[0].rstrip(";") not in {
                "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                "DROP", "TRUNCATE", "GRANT", "REVOKE",
            }, first


def test_existing_endpoints_still_function() -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/discovery/sources").status_code == 200


# --- Path-scoped no-store middleware ---


def test_framework_405_gains_no_store_and_keeps_body() -> None:
    aid = uuid4()
    # Trailing slash included: the client normalizes it to the same path.
    paths = (_url(aid), _url(aid) + "/")
    for method in ("post", "put", "delete", "patch"):
        for target in paths:
            resp = getattr(client, method)(target)
            assert resp.status_code == 405
            assert resp.json() == {"detail": "Method Not Allowed"}
            assert "GET" in resp.headers["allow"]  # unrelated header kept
            assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", [
    "/health",
    "/discovery/sources",
    "/external-actions",
    "/external-actions/{aid}",
    "/external-actions/{aid}/reviews",
    "/external-actions/{aid}/review/extra",
    "/external-actions//review",
    # No trailing-slash entry: the client normalizes it to the canonical
    # path, which is covered by the positive no-store tests instead.
])
def test_unrelated_paths_gain_no_header(path) -> None:
    resp = client.get(path.format(aid=uuid4()))
    assert resp.status_code in (200, 307, 404)
    assert "cache-control" not in resp.headers


def test_unexpected_error_returns_plain_500(monkeypatch) -> None:
    def _boom(**kwargs):
        raise RuntimeError("boom-marker-never-client-visible")

    monkeypatch.setattr(review_route, "get_reconciliation_case", _boom)
    resp = client.get(_url(uuid4()), headers=_headers())
    assert resp.status_code == 500
    assert resp.text == "Internal Server Error"  # framework plain text
    assert "boom-marker" not in resp.text
    # Honest boundary, not an oversight: unhandled-exception 500s are
    # emitted by Starlette's ServerErrorMiddleware, which wraps OUTSIDE
    # all user middleware, so no pure ASGI middleware can stamp them.
    # The route maps every known failure to a handled, stamped response.
    assert "cache-control" not in resp.headers


def _run_middleware(path, *, method="GET", scope_type="http", status=200,
                     body=b"ok", headers=(), request_body=b"req-bytes",
                     echo_request=False):
    seen: dict = {}
    sent: list = []

    async def stub_app(scope, receive, send):
        seen["request_headers"] = list(scope.get("headers", []))
        if echo_request:
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            seen["request_body"] = b"".join(chunks)
        await send({"type": "http.response.start", "status": status,
                    "headers": list(headers)})
        await send({"type": "http.response.body", "body": body})

    fired = {"once": False}

    async def receive():
        if fired["once"]:
            return {"type": "http.disconnect"}
        fired["once"] = True
        return {"type": "http.request", "body": request_body,
                "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": scope_type, "http_version": "1.1", "method": method,
             "path": path, "headers": [(b"x-probe", b"probe-value")]}
    asyncio.run(ReviewNoStoreMiddleware(stub_app)(scope, receive, send))
    return seen, sent


def _start_headers(sent):
    (start, body_msg) = sent
    assert start["type"] == "http.response.start"
    assert body_msg["type"] == "http.response.body"
    return start, body_msg


def test_middleware_stamps_matching_path_regardless_of_id() -> None:
    for path in (f"/external-actions/{uuid4()}/review",
                 f"/external-actions/{uuid4()}/review/",  # raw ASGI keeps /
                 "/external-actions/not-a-uuid/review",
                 "/external-actions/%%%/review"):
        _, sent = _run_middleware(path, status=404,
                                   body=b'{"detail":"Not Found"}')
        start, body_msg = _start_headers(sent)
        assert start["status"] == 404
        assert body_msg["body"] == b'{"detail":"Not Found"}'
        assert (b"cache-control", b"no-store") in start["headers"]


def test_middleware_replaces_cache_control_and_keeps_rest() -> None:
    _, sent = _run_middleware(
        f"/external-actions/{uuid4()}/review", status=200, body=b"report",
        headers=[(b"content-type", b"application/json"),
                 (b"Cache-Control", b"max-age=60")],
    )
    start, body_msg = _start_headers(sent)
    assert start["status"] == 200
    assert body_msg["body"] == b"report"
    kept = [(k, v) for k, v in start["headers"]
            if bytes(k).lower() != b"cache-control"]
    assert kept == [(b"content-type", b"application/json")]
    assert start["headers"].count(
        (b"cache-control", b"no-store")) == 1


@pytest.mark.parametrize("path", [
    "/", "/health", "/external-actions", "/external-actions/abc",
    "/external-actions/abc/review/extra", "/external-actions/abc/reviews",
    "/external-actions//review", "/other/abc/review", "",
    "/external-actions//abc/review", "/external-actions/abc//review",
    "/external-actions/abc/review//", "//external-actions/abc/review",
])
def test_middleware_ignores_non_matching_paths(path) -> None:
    _, sent = _run_middleware(path)
    start, _ = _start_headers(sent)
    assert start["headers"] == []


def test_middleware_preserves_request_body_and_headers() -> None:
    seen, sent = _run_middleware(
        f"/external-actions/{uuid4()}/review", echo_request=True)
    assert seen["request_body"] == b"req-bytes"
    assert (b"x-probe", b"probe-value") in seen["request_headers"]
    start, _ = _start_headers(sent)
    assert (b"cache-control", b"no-store") in start["headers"]


def test_middleware_passes_websocket_through() -> None:
    _, sent = _run_middleware(f"/external-actions/{uuid4()}/review",
                              scope_type="websocket")
    start, _ = _start_headers(sent)
    assert start["headers"] == []
