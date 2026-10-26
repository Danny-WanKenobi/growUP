"""Readiness endpoint tests with a mocked database check (no real PostgreSQL)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.routes.readiness as readiness
from app.db import DatabaseUnavailableError
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

_CONFIGURED_ENV = {
    "DATABASE_HOST": "db.example.invalid",
    "DATABASE_PORT": "5432",
    "DATABASE_NAME": "growup_dev",
    "DATABASE_USER": "growup_app",
}


def _set_configured_env(monkeypatch, password: str = "unit-test-pw") -> None:
    for key, value in _CONFIGURED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_PASSWORD", password)


def test_ready_connected_when_database_answers(monkeypatch) -> None:
    _set_configured_env(monkeypatch)
    monkeypatch.setattr(readiness, "check_database_connection", lambda cfg: True)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_ready_unreachable_when_database_fails(monkeypatch) -> None:
    secret = "ready-secret-pw-67890"
    _set_configured_env(monkeypatch, password=secret)

    def _fail(cfg):
        raise DatabaseUnavailableError("connection_failed")

    monkeypatch.setattr(readiness, "check_database_connection", _fail)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}
    assert secret not in response.text


def test_ready_not_configured_when_password_missing(monkeypatch) -> None:
    _set_configured_env(monkeypatch)
    monkeypatch.delenv("DATABASE_PASSWORD")

    def _must_not_run(cfg):  # pragma: no cover - guard assertion
        raise AssertionError("database check must not run without configuration")

    monkeypatch.setattr(readiness, "check_database_connection", _must_not_run)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "not_configured"}


def test_ready_never_exposes_password_on_unexpected_error(monkeypatch) -> None:
    secret = "ready-leak-probe-pw-24680"
    _set_configured_env(monkeypatch, password=secret)

    def _boom(cfg):
        raise RuntimeError(f"boom host=db user=u password={secret}")

    monkeypatch.setattr(readiness, "check_database_connection", _boom)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}
    assert secret not in response.text
