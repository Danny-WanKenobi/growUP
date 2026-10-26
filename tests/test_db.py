"""Database check unit tests with a stubbed psycopg driver (no real server)."""

from __future__ import annotations

import pytest

import app.db as db_module
from app.config import DatabaseConfig, get_database_config
from app.db import (
    DatabaseNotConfiguredError,
    DatabaseUnavailableError,
    check_database_connection,
)


class _FakeCursor:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, *args, **kwargs) -> None:
        self._recorder["query"] = query

    def fetchone(self):
        return (1,)


class _FakeConnection:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _FakeCursor(self._recorder)


def test_check_raises_when_not_configured() -> None:
    config = DatabaseConfig(host="localhost", port=5432, name="db", user="u", password="")
    with pytest.raises(DatabaseNotConfiguredError):
        check_database_connection(config)


def test_check_uses_env_credentials_and_select_one(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "pg.example.invalid")
    monkeypatch.setenv("DATABASE_PORT", "5433")
    monkeypatch.setenv("DATABASE_NAME", "growup_dev")
    monkeypatch.setenv("DATABASE_USER", "growup_app")
    monkeypatch.setenv("DATABASE_PASSWORD", "env-wired-pw-13579")

    recorder: dict = {}
    psycopg = pytest.importorskip("psycopg")

    def _fake_connect(**kwargs):
        recorder["kwargs"] = kwargs
        return _FakeConnection(recorder)

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    assert check_database_connection(get_database_config()) is True
    assert recorder["kwargs"]["host"] == "pg.example.invalid"
    assert recorder["kwargs"]["port"] == 5433
    assert recorder["kwargs"]["dbname"] == "growup_dev"
    assert recorder["kwargs"]["user"] == "growup_app"
    assert recorder["kwargs"]["password"] == "env-wired-pw-13579"
    assert recorder["query"] == "SELECT 1"


def test_check_wraps_connection_failure_generically(monkeypatch) -> None:
    config = DatabaseConfig(
        host="localhost", port=5432, name="db", user="u", password="pw"
    )
    psycopg = pytest.importorskip("psycopg")

    def _fail(**kwargs):
        raise ConnectionError("dial tcp refused")

    monkeypatch.setattr(psycopg, "connect", _fail)
    with pytest.raises(DatabaseUnavailableError, match="^connection_failed$"):
        check_database_connection(config)
    assert db_module.DatabaseUnavailableError is DatabaseUnavailableError
