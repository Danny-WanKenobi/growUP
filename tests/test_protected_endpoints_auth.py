"""Intake-key enforcement on discovery-runs and property endpoints.

Step 73A: POST /discovery/runs, POST /properties, GET /properties,
and GET /properties/{id} require the shared X-Intake-Key header.
Business functions are mocked throughout; these tests prove that
unauthorized requests never reach them and that error responses
leak no key material.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.routes.discovery as discovery_routes
import app.routes.properties as property_routes
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

TEST_INTAKE_KEY = "unit-test-intake-key-only"
_DETAIL_ID = "12345678-1234-5678-1234-567812345678"

_RUNS_BODY = {
    "source": "overpass",
    "bbox": {"south": 32.770, "west": -96.810, "north": 32.790,
             "east": -96.785},
}
_CREATE_BODY = {
    "name": "Auth Test Property",
    "category": "other",
    "provenance": "unit-test evidence record QA-001",
}

# method, url, body, mock target, mock attribute, success status.
_ENDPOINTS = [
    ("POST", "/discovery/runs", _RUNS_BODY,
     "discovery", "run_discovery", 200),
    ("POST", "/properties", _CREATE_BODY,
     "properties", "create_property", 201),
    ("GET", "/properties", None,
     "properties", "list_properties", 200),
    ("GET", f"/properties/{_DETAIL_ID}", None,
     "properties", "get_property", 200),
]
_ENDPOINT_IDS = ["runs", "create", "list", "detail"]


@pytest.fixture(autouse=True)
def _intake_key_configured(monkeypatch):
    """Provide the server-side test key (authenticates nothing alone).

    Every request below still passes its X-Intake-Key header
    explicitly; unconfigured-server tests override this per-test.
    """
    monkeypatch.setenv("INTAKE_API_KEY", TEST_INTAKE_KEY)


def _headers():
    return {"X-Intake-Key": TEST_INTAKE_KEY}


def _request(method, url, body, headers=None):
    kwargs = {"headers": headers} if headers is not None else {}
    if body is not None:
        kwargs["json"] = body
    return client.request(method, url, **kwargs)


def _stored_row():
    return {
        "id": _DETAIL_ID,
        "name": "Auth Test Property",
        "category": "other",
        "data_status": "unknown",
        "created_at": "2026-10-26T00:00:00+00:00",
        "updated_at": "2026-10-26T00:00:00+00:00",
    }


def _discovery_result():
    from app.agents.gabriel.schemas import (
        DiscoveryRunCounts,
        DiscoveryRunResult,
    )

    return DiscoveryRunResult(
        source="overpass",
        retrieved_at="2026-10-27T00:00:00+00:00",
        categories=["downtown_commercial"],
        limit=3,
        counts=DiscoveryRunCounts(returned=1, created=1),
        created_ids=["id-1"],
    )


def _mock_business(monkeypatch, module, attribute):
    calls = []
    module_obj = (
        discovery_routes if module == "discovery" else property_routes
    )

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        if attribute == "run_discovery":
            return _discovery_result()
        if attribute == "list_properties":
            return [_stored_row()]
        return _stored_row()

    monkeypatch.setattr(module_obj, attribute, _fake)
    return calls


def _assert_no_key_leak(response) -> None:
    assert TEST_INTAKE_KEY not in response.text
    assert "wrong-key" not in response.text


@pytest.mark.parametrize(
    "method,url,body,module,attribute,expected", _ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_missing_header_returns_401(
        monkeypatch, method, url, body, module, attribute, expected,
) -> None:
    calls = _mock_business(monkeypatch, module, attribute)
    response = _request(method, url, body)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    _assert_no_key_leak(response)
    assert calls == []


@pytest.mark.parametrize(
    "method,url,body,module,attribute,expected", _ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_wrong_header_returns_401_without_leak(
        monkeypatch, method, url, body, module, attribute, expected,
) -> None:
    calls = _mock_business(monkeypatch, module, attribute)
    response = _request(method, url, body, {"X-Intake-Key": "wrong-key"})
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    _assert_no_key_leak(response)
    assert calls == []


@pytest.mark.parametrize(
    "method,url,body,module,attribute,expected", _ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
@pytest.mark.parametrize("mode", ["missing", "blank"])
def test_unconfigured_key_fails_closed_503(
        monkeypatch, method, url, body, module, attribute, expected,
        mode,
) -> None:
    calls = _mock_business(monkeypatch, module, attribute)
    if mode == "missing":
        monkeypatch.delenv("INTAKE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("INTAKE_API_KEY", "   ")
    response = _request(method, url, body, _headers())
    assert response.status_code == 503
    assert response.json() == {"detail": "intake_not_configured"}
    _assert_no_key_leak(response)
    assert calls == []


@pytest.mark.parametrize(
    "method,url,body,module,attribute,expected", _ENDPOINTS,
    ids=_ENDPOINT_IDS,
)
def test_valid_key_permits_execution(
        monkeypatch, method, url, body, module, attribute, expected,
) -> None:
    calls = _mock_business(monkeypatch, module, attribute)
    response = _request(method, url, body, _headers())
    assert response.status_code == expected
    assert len(calls) == 1
