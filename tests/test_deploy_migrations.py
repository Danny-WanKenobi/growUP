"""OPS-2: migration-before-activation deployment order (tests-first).

Windows-compatible static validation of the repository-managed
deployment controller source ``deployment/growup-install-release``.
These tests read files only; they never execute the controller,
touch systemd, or require bash (unavailable on this host).
Behavioral execution is verified on Ubuntu after the reviewed
controller is installed (see README-DEPLOYMENT.md).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = REPO_ROOT / "deployment"
CONTROLLER = DEPLOYMENT / "growup-install-release"
README = DEPLOYMENT / "README-DEPLOYMENT.md"
DEPLOY_PS1 = DEPLOYMENT / "deploy-dev.ps1"


def _controller() -> str:
    assert CONTROLLER.exists(), "missing deployment controller source"
    return CONTROLLER.read_text(encoding="utf-8")


def _migrate_block() -> str:
    """Controller text from the migration invocation through its failure exit."""
    text = _controller()
    migrate_at = text.index("app.migrate")
    start = text.rindex("if ! systemd-run", 0, migrate_at)
    # Failure message immediately follows the migrate invocation.
    end = text.index("release not activated")
    return text[start:end]


def _stage_index(token: str) -> int:
    return _controller().index(token)


# --- Deployment sequence -----------------------------------------------------


def test_migration_runs_after_install_before_activation() -> None:
    assert _stage_index('"$INSTALLER"') < _stage_index("app.migrate")
    assert _stage_index("app.migrate") < _stage_index('mv -Tf "$PENDING" "$ROOT/current"')


def test_migration_runs_before_service_restart_and_health() -> None:
    assert _stage_index("app.migrate") < _stage_index('systemctl restart "$SERVICE"')
    assert _stage_index("app.migrate") < _stage_index("/health")


def test_migration_failure_prevents_activation() -> None:
    text = _controller()
    migrate_at = text.index("app.migrate")
    fail_exit = text.index("release not activated")
    activation_at = text.index('mv -Tf "$PENDING" "$ROOT/current"')
    assert migrate_at < fail_exit < activation_at
    segment = text[migrate_at:activation_at]
    assert "exit 1" in segment
    # The only symlink op before activation builds the dotfile pending link.
    assert "mv -Tf" not in segment
    assert "systemctl restart" not in segment


def test_failed_migration_leaves_previous_release_untouched() -> None:
    # Activation reads PREVIOUS only after the migration gate passes.
    text = _controller()
    assert text.index("release not activated") < text.index("PREVIOUS=$(readlink")


# --- Migration execution contract --------------------------------------------


def test_migration_uses_new_release_python() -> None:
    text = _controller()
    assert 'NEW_RELEASE="$ROOT/releases/$RELEASE"' in text
    assert 'NEW_PYTHON="$NEW_RELEASE/.venv/bin/python"' in text
    assert '"$NEW_PYTHON"' in _migrate_block()


def test_migration_never_runs_from_current_symlink() -> None:
    assert "current/.venv" not in _migrate_block()
    assert "current" not in _migrate_block()


def test_migration_runs_as_service_account_not_root() -> None:
    block = _migrate_block()
    assert "User=growup" in block
    assert "Group=growup" in block
    assert "sudo" not in block
    assert "runuser" not in block


def test_migration_working_directory_is_new_release() -> None:
    assert 'WorkingDirectory="$NEW_RELEASE"' in _migrate_block()


def test_migration_exit_status_propagates() -> None:
    # --wait --pipe makes systemd-run report the migration exit status,
    # which the `if !` guard turns into a pre-activation abort.
    block = _migrate_block()
    assert "--wait" in block
    assert "--pipe" in block
    assert block.startswith("if ! systemd-run")


def test_prepared_release_validated_before_migration() -> None:
    text = _controller()
    check = text.index("Prepared release incomplete")
    assert check < text.index("app.migrate")
    segment = text[check - 200 : check]
    assert '-x "$NEW_PYTHON"' in segment
    assert '"$NEW_RELEASE/app/migrate.py"' in segment


def test_migration_uses_fixed_trusted_environment() -> None:
    text = _controller()
    assert 'ENVFILE="$ROOT/shared/growup.env"' in text
    block = _migrate_block()
    # systemd parses the env file as KEY=VALUE; it is never shell-sourced.
    assert 'EnvironmentFile="$ENVFILE"' in block
    assert '"$NEW_PYTHON" -m app.migrate' in block
    stage = text[text.index("Prepared release incomplete") : text.index("release not activated")]
    assert "/bin/bash -c" not in stage
    assert "set -a" not in stage
    assert '". $1"' not in stage and '. "$1"' not in stage


def test_migration_stage_prints_no_secrets() -> None:
    text = _controller()
    migrate_at = text.index('NEW_RELEASE="$ROOT/releases/$RELEASE"')
    activation_at = text.index('mv -Tf "$PENDING" "$ROOT/current"')
    segment = text[migrate_at:activation_at]
    assert "cat $ENVFILE" not in segment
    assert "cat \"$ENVFILE\"" not in segment
    assert "printenv" not in segment
    assert "DATABASE_" not in segment


# --- Isolated behavioral simulation ------------------------------------------
# The stage order and the migration failure guard below are parsed from the
# actual controller text (not hardcoded): any reorder or guard removal
# changes the parsed model and fails the simulation. No shell, database,
# or Ubuntu host is involved.


def _parsed_stage_order() -> list[str]:
    text = _controller()
    markers = [
        ("validate", '[[ "$RELEASE" =~'),
        ("checksum", "SHA256 mismatch"),
        ("install", '"$INSTALLER" "$RELEASE"'),
        ("migrate", "systemd-run"),
        ("activate", 'mv -Tf "$PENDING" "$ROOT/current"'),
        ("restart", 'if ! systemctl restart "$SERVICE"; then rollback; exit 1; fi'),
        ("health", "/health"),
    ]
    return [name for name, _ in sorted(markers, key=lambda m: text.index(m[1]))]


def _migration_guard() -> str:
    """Body of the `if ! ...; then ... fi` guard wrapping the migration."""
    text = _controller()
    start = text.index("if ! systemd-run")
    end = text.index("\nfi", start)
    return text[start:end]


def _simulate(migrate_rc: int) -> list[str]:
    """Walk the parsed stages; a guarded nonzero migration halts the run."""
    guard = _migration_guard()
    guarded_abort = "exit 1" in guard and "mv -Tf" not in guard
    reached: list[str] = []
    for stage in _parsed_stage_order():
        if stage == "migrate" and migrate_rc != 0:
            assert guarded_abort, "migration failure is not guarded by a pre-activation exit"
            return reached
        reached.append(stage)
    return reached


def test_simulated_stage_order_matches_approved_sequence() -> None:
    assert _parsed_stage_order() == [
        "validate",
        "checksum",
        "install",
        "migrate",
        "activate",
        "restart",
        "health",
    ]


def test_simulated_migration_failure_halts_before_activation() -> None:
    reached = _simulate(migrate_rc=1)
    assert reached == ["validate", "checksum", "install"]
    assert "activate" not in reached
    assert "restart" not in reached
    assert "health" not in reached


def test_simulated_migration_success_reaches_activation() -> None:
    assert _simulate(migrate_rc=0) == [
        "validate",
        "checksum",
        "install",
        "migrate",
        "activate",
        "restart",
        "health",
    ]


# --- Preserved security controls ---------------------------------------------


def test_deployment_lock_preserved() -> None:
    assert "flock -n 9" in _controller()


def test_release_validation_preserved() -> None:
    text = _controller()
    assert r'[[ "$RELEASE" =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{7}$ ]]' in text
    assert "SHA256 mismatch" in text
    assert "Staged archive checksum mismatch" in text
    assert "Trusted installer missing" in text


def test_privilege_separation_preserved() -> None:
    text = _controller()
    assert "install -d -o root -g growup -m 750" in text
    assert "install -o root -g growup -m 640" in text
    assert 'runuser -u growup -- /bin/bash "$INSTALLER"' in text


def test_health_check_rollback_preserved() -> None:
    text = _controller()
    assert "rollback()" in text
    assert "reverting active release" in text
    assert 'if ! systemctl restart "$SERVICE"; then rollback; exit 1; fi' in text
    assert "HEALTHY=0" in text
    assert 'if [[ "$HEALTHY" != 1 ]]; then rollback; exit 1; fi' in text


def test_deploy_script_still_calls_controller() -> None:
    assert "/usr/local/sbin/growup-install-release" in DEPLOY_PS1.read_text(encoding="utf-8")


# --- Documentation -----------------------------------------------------------


def test_runbook_documents_migration_ordering() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "migration" in readme.lower()
    assert "before activation" in readme.lower()


def test_runbook_documents_compatibility_rule() -> None:
    readme = README.read_text(encoding="utf-8").lower()
    assert "backward-compatible" in readme or "backward compatible" in readme
    assert "no" in readme and "down-migration" in readme.replace("_", "-")


def test_runbook_documents_controller_install() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "/usr/local/sbin/growup-install-release" in readme
    assert "bash -n" in readme
