"""Health endpoint tests (no database access)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "growup"
    assert body["version"]


def test_health_does_not_expose_secrets(monkeypatch) -> None:
    secret = "health-secret-pw-12345"
    monkeypatch.setenv("DATABASE_PASSWORD", secret)
    response = client.get("/health")
    assert response.status_code == 200
    assert secret not in response.text
