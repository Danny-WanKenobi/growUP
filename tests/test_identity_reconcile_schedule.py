"""IR-3C C1c: reconciliation scheduling templates (tests-first).

Windows-compatible validation of the repository-managed systemd
service/timer templates and deployment runbook. These tests read files
only; they never install units, enable timers, or touch the host.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = REPO_ROOT / "deployment"
SERVICE = DEPLOYMENT / "growup-identity-reconcile.service"
TIMER = DEPLOYMENT / "growup-identity-reconcile.timer"
README = DEPLOYMENT / "README-DEPLOYMENT.md"
RECOVERY_SERVICE = DEPLOYMENT / "growup-outbox-recovery.service"
RECOVERY_TIMER = DEPLOYMENT / "growup-outbox-recovery.timer"


def _unit(path: Path) -> configparser.ConfigParser:
    assert path.exists(), f"missing systemd template: {path.name}"
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read(path, encoding="utf-8")
    return parser


def _service() -> configparser.ConfigParser:
    return _unit(SERVICE)


def _timer() -> configparser.ConfigParser:
    return _unit(TIMER)


def _exec_argv() -> list[str]:
    return _service()["Service"]["ExecStart"].split()


# --- Template existence and identity -------------------------------------------------


def test_service_and_timer_templates_exist() -> None:
    assert SERVICE.exists()
    assert TIMER.exists()


def test_templates_name_reconciliation_not_recovery() -> None:
    assert "reconcil" in _service()["Unit"]["Description"].lower()
    assert "reconcil" in _timer()["Unit"]["Description"].lower()
    assert "recovery" not in SERVICE.read_text(encoding="utf-8").lower()
    assert "recovery" not in TIMER.read_text(encoding="utf-8").lower()


# --- Service execution contract -----------------------------------------------------------


def test_service_is_oneshot() -> None:
    assert _service()["Service"]["Type"] == "oneshot"


def test_service_user_and_group() -> None:
    service = _service()["Service"]
    assert service["User"] == "growup"
    assert service["Group"] == "growup"


def test_service_paths() -> None:
    service = _service()["Service"]
    assert service["WorkingDirectory"] == "/var/www/growup/current"
    assert service["EnvironmentFile"] == "/var/www/growup/shared/growup.env"


def test_service_executes_reconciler_exactly() -> None:
    argv = _exec_argv()
    assert argv[:3] == ["/usr/bin/flock", "-n", "/var/www/growup/shared/identity-reconcile.lock"]
    assert argv[3:] == [
        "/var/www/growup/current/.venv/bin/python",
        "-m",
        "app.identity_review_reconcile",
    ]


def test_service_imposes_no_event_limit() -> None:
    assert "--limit" not in _exec_argv(), "scheduled sweeps must not be truncated"


def test_service_lock_differs_from_recovery_lock() -> None:
    recovery_argv = _unit(RECOVERY_SERVICE)["Service"]["ExecStart"].split()
    assert _exec_argv()[2] != recovery_argv[2]
    assert _exec_argv()[2].endswith("identity-reconcile.lock")


def test_service_invokes_no_gabriel_or_providers() -> None:
    for path in (SERVICE, TIMER):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "gabriel" not in lowered
        assert "app.agents" not in text
        assert "brave" not in lowered
        assert "recovery" not in lowered


def test_service_has_no_app_lifecycle_binding() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    assert "growup.service" not in text
    assert "PartOf=" not in text
    assert "BindsTo=" not in text


def test_service_startup_dependencies() -> None:
    unit = _service()["Unit"]
    assert "network-online.target" in unit["After"]
    assert "postgresql.service" in unit["After"]


def test_service_timeout_permits_full_sweeps() -> None:
    # Systemd's default start timeout (~90s) could kill a legitimate
    # full-backlog sweep; the template must set explicit headroom.
    assert _service()["Service"]["TimeoutStartSec"] == "30min"


def test_service_hardening() -> None:
    service = _service()["Service"]
    assert service["NoNewPrivileges"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["PrivateDevices"] == "true"
    assert service["ProtectHome"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert "/var/www/growup/shared" in service["ReadWritePaths"]
    families = service["RestrictAddressFamilies"].split()
    assert {"AF_UNIX", "AF_INET", "AF_INET6"} <= set(families)


# --- Timer contract -------------------------------------------------------------------------------


def test_timer_cadence_and_target() -> None:
    timer = _timer()["Timer"]
    assert timer["OnBootSec"] == "2min"
    assert timer["OnUnitInactiveSec"] == "5min"
    assert timer["AccuracySec"] == "30s"
    assert timer["Unit"] == "growup-identity-reconcile.service"
    assert _timer()["Install"]["WantedBy"] == "timers.target"


# --- Closed-workstream protection --------------------------------------------------------------------


def test_existing_recovery_units_untouched() -> None:
    service_text = RECOVERY_SERVICE.read_text(encoding="utf-8")
    timer_text = RECOVERY_TIMER.read_text(encoding="utf-8")
    assert "app.observability.recovery" in service_text
    assert "recovery.lock" in service_text
    assert "OnUnitInactiveSec=30s" in timer_text
    assert "reconcile" not in service_text.lower()
    assert "reconcile" not in timer_text.lower()


def test_deploy_scripts_do_not_manage_reconcile_timer() -> None:
    for name in ("deploy-dev.ps1", "growup-install-release"):
        text = (DEPLOYMENT / name).read_text(encoding="utf-8")
        assert "identity-reconcile" not in text
        assert "systemctl enable" not in text


# --- Runbook -----------------------------------------------------------------------------------------------


def _runbook() -> str:
    assert README.exists()
    return README.read_text(encoding="utf-8")


def test_runbook_distinguishes_recovery_from_reconciliation() -> None:
    text = _runbook()
    assert "growup-identity-reconcile.service" in text
    assert "growup-identity-reconcile.timer" in text
    assert "outbox" in text.lower()
    assert "identity_review" in text or "identity-review" in text


def test_runbook_documents_env_without_secrets() -> None:
    text = _runbook()
    for var in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER"):
        assert var in text
    # Names may be listed; values must never appear. The runbook shows
    # a names-only verification command.
    assert "cut -d= -f1" in text


def test_runbook_documents_manual_install_and_dry_run() -> None:
    text = _runbook()
    assert "cp /var/www/growup/current/deployment/growup-identity-reconcile.service" in text
    assert "systemctl daemon-reload" in text
    assert "sudo -u growup" in text
    assert "-m app.identity_review_reconcile" in text


def test_runbook_documents_activation_and_monitoring() -> None:
    text = _runbook()
    assert "systemctl enable --now growup-identity-reconcile.timer" in text
    assert "list-timers growup-identity-reconcile.timer" in text
    assert "journalctl -u growup-identity-reconcile.service" in text


def test_runbook_documents_exit_codes_and_malformed_events() -> None:
    text = _runbook()
    assert "exit 1" in text.lower() or "exit code 1" in text.lower()
    assert "malformed" in text.lower()


def test_runbook_documents_rollback_and_unit_refresh() -> None:
    text = _runbook()
    assert "systemctl disable --now growup-identity-reconcile.timer" in text
    assert "rm /etc/systemd/system/growup-identity-reconcile.timer" in text
    assert "current" in text and "symlink" in text.lower()


def test_runbook_separates_repo_from_host_steps() -> None:
    text = _runbook()
    assert "repository" in text.lower()
    assert "host" in text.lower() or "privileged" in text.lower()
