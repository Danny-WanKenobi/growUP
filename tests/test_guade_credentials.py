"""GUADE credential-loading tests (offline; no real secret is used).

The loader must find the Baseten key in a fresh process even when the
parent process environment is stale (Windows user env installed after
the parent started) or when the key lives only in the restricted
per-user env file (Ubuntu). Values must never leak into status output.
"""
from __future__ import annotations

import pytest

from guade import credentials


def test_process_env_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "reg-key")
    monkeypatch.setattr(credentials, "_candidate_env_files", lambda env: [])
    resolved = credentials.resolve_baseten_api_key(
        environ={"BASETEN_API_KEY": "env-key"})
    assert resolved.value == "env-key"
    assert resolved.source == "process_env"


def test_windows_user_env_recovers_stale_parent(tmp_path, monkeypatch):
    # Parent started before the user env var was installed: env is
    # empty, but the persistent user store still yields the key.
    monkeypatch.setattr(credentials, "_from_windows_registry",
                        lambda: "registry-key")
    monkeypatch.setattr(credentials, "_candidate_env_files", lambda env: [])
    resolved = credentials.resolve_baseten_api_key(environ={})
    assert resolved.value == "registry-key"
    assert resolved.source == "windows_user_env"


def test_env_file_fallback_and_meta_alias(tmp_path, monkeypatch):
    env_file = tmp_path / "guade.env"
    env_file.write_text("META_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "")
    monkeypatch.setattr(credentials, "_candidate_env_files",
                        lambda env: [env_file])
    monkeypatch.setattr(credentials, "_env_file_problem", lambda path: None)
    resolved = credentials.resolve_baseten_api_key(environ={})
    assert resolved.value == "file-key"
    assert resolved.source.startswith("env_file")


def test_meta_alias_refused_for_non_baseten_endpoint(tmp_path, monkeypatch):
    # META_API_KEY paired with a non-Baseten META_API_URL is a
    # different credential; GUADE must not spend it against Baseten.
    env_file = tmp_path / "guade.env"
    env_file.write_text(
        "META_API_URL=https://example.invalid/v1\nMETA_API_KEY=other-key\n",
        encoding="utf-8")
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "")
    monkeypatch.setattr(credentials, "_candidate_env_files",
                        lambda env: [env_file])
    monkeypatch.setattr(credentials, "_env_file_problem", lambda path: None)
    assert credentials.resolve_baseten_api_key(environ={}).value == ""


def test_meta_alias_accepted_for_baseten_endpoint(tmp_path, monkeypatch):
    env_file = tmp_path / "guade.env"
    env_file.write_text(
        "META_API_URL=https://inference.baseten.co/v1\n"
        "META_API_KEY=baseten-key\n", encoding="utf-8")
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "")
    monkeypatch.setattr(credentials, "_candidate_env_files",
                        lambda env: [env_file])
    monkeypatch.setattr(credentials, "_env_file_problem", lambda path: None)
    assert credentials.resolve_baseten_api_key(environ={}).value \
        == "baseten-key"


def test_absent_everywhere_is_actionable(monkeypatch):
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "")
    monkeypatch.setattr(credentials, "_candidate_env_files", lambda env: [])
    resolved = credentials.resolve_baseten_api_key(environ={})
    assert resolved.value == ""
    assert resolved.source == "absent"
    status = credentials.credential_status(environ={})
    assert status["present"] is False
    assert "BASETEN_API_KEY" in credentials.missing_key_guidance()


def test_status_never_contains_the_value(monkeypatch):
    monkeypatch.setattr(credentials, "_from_windows_registry", lambda: "")
    monkeypatch.setattr(credentials, "_candidate_env_files", lambda env: [])
    status = credentials.credential_status(
        environ={"BASETEN_API_KEY": "super-secret-value"})
    assert status["present"] is True
    assert "super-secret-value" not in repr(status)
    resolved = credentials.resolve_baseten_api_key(
        environ={"BASETEN_API_KEY": "super-secret-value"})
    assert "super-secret-value" not in repr(resolved)


def test_coach_uses_loader_when_env_absent(tmp_path, monkeypatch):
    from guade.runtime import Authorization, Coach, Ledger

    monkeypatch.setattr(
        credentials, "resolve_baseten_api_key",
        lambda environ=None: credentials.ResolvedKey("loaded-key", "env_file"))
    monkeypatch.setattr("guade.runtime.resolve_baseten_api_key",
                        credentials.resolve_baseten_api_key)
    ledger = Ledger(tmp_path / "state", Authorization())
    coach = Coach(ledger)
    assert coach.available()
    assert coach.api_key == "loaded-key"
