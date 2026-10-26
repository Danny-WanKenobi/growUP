"""Database configuration tests (stdlib only; no fastapi/psycopg needed)."""

from __future__ import annotations

import pytest

from app.config import DatabaseConfig, get_database_config

DATABASE_ENV_VARS = (
    "DATABASE_HOST",
    "DATABASE_PORT",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)


@pytest.fixture(autouse=True)
def _clean_database_env(monkeypatch):
    for var in DATABASE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_leave_database_unconfigured() -> None:
    config = get_database_config()
    assert config.host == "localhost"
    assert config.port == 5432
    assert config.is_configured is False


def test_env_values_are_wired_through(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "pg.example.invalid")
    monkeypatch.setenv("DATABASE_PORT", "5433")
    monkeypatch.setenv("DATABASE_NAME", "growup_dev")
    monkeypatch.setenv("DATABASE_USER", "growup_app")
    monkeypatch.setenv("DATABASE_PASSWORD", "s3cret-pw")
    config = get_database_config()
    assert config.is_configured is True
    kwargs = config.connection_kwargs()
    assert kwargs["host"] == "pg.example.invalid"
    assert kwargs["port"] == 5433
    assert kwargs["dbname"] == "growup_dev"
    assert kwargs["user"] == "growup_app"
    assert kwargs["password"] == "s3cret-pw"


def test_invalid_port_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "not-a-port")
    monkeypatch.setenv("DATABASE_NAME", "growup_dev")
    monkeypatch.setenv("DATABASE_USER", "growup_app")
    monkeypatch.setenv("DATABASE_PASSWORD", "s3cret-pw")
    assert get_database_config().is_configured is False


def test_missing_password_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "growup_dev")
    monkeypatch.setenv("DATABASE_USER", "growup_app")
    assert get_database_config().is_configured is False


def test_repr_redacts_password() -> None:
    config = DatabaseConfig(
        host="h", port=5432, name="db", user="u", password="super-secret-pw"
    )
    assert "super-secret-pw" not in repr(config)
