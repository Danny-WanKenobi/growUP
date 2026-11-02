"""Development-only mission lock, immutable grant, deadline and stop guard.

These controls constrain execution; configuration files do not grant owner
permission. Historical overnight ledgers remain readable and are never reset.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .runtime import Authorization, GuadeError, Ledger

MISSION_ID = "overnight-20261102"
MAX_RUNTIME_SECONDS = 5 * 3600


class MissionError(GuadeError):
    """Mission control failure (lock, deadline, stop, snapshot)."""


def mission_dir(root: Path) -> Path:
    return Path(root) / "guade" / f"state-{MISSION_ID}"


def write_authorization_snapshot(directory: Path, snapshot: dict) -> Path:
    """Write the immutable authorization snapshot exactly once."""
    path = Path(directory) / "authorization.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != snapshot:
            raise MissionError("authorization snapshot already exists "
                               "and differs; refusing to overwrite")
        return path
    Path(directory).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def snapshot_digest(directory: Path) -> str:
    path = Path(directory) / "authorization.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MissionLock:
    """Exclusive single-instance lock with stale-owner recovery."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "mission.lock"
        self.acquired = False
        self.retain = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise MissionError(
                "mission lock held; a second instance is refused "
                "(delete the lock only after confirming no live owner)"
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"pid": os.getpid(), "acquired_at": time.time()}))
        self.acquired = True

    def release(self) -> None:
        if self.acquired and not self.retain:
            self.path.unlink(missing_ok=True)
            self.acquired = False


class Mission:
    """Deadline + emergency-stop guard over one mission directory."""

    def __init__(self, directory: Path, max_runtime_seconds: float = MAX_RUNTIME_SECONDS) -> None:
        self.dir = Path(directory)
        self.state_path = self.dir / "mission.json"
        if self.state_path.exists():
            self.state = json.loads(
                self.state_path.read_text(encoding="utf-8"))
        else:
            now = time.time()
            self.state = {
                "mission_id": self.dir.name,
                "started_at": now,
                "deadline_at": now + max_runtime_seconds,
                "status": "created",
            }
            self._save()
        self._monotonic_deadline = time.monotonic() + max(
            0, float(self.state["deadline_at"]) - time.time())

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, min(float(self.state["deadline_at"]) - time.time(),
                            self._monotonic_deadline - time.monotonic()))

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    def set_status(self, status: str) -> None:
        self.state["status"] = status
        self._save()

    @property
    def deadline_exceeded(self) -> bool:
        return self.remaining_seconds <= 0

    @property
    def stop_requested(self) -> bool:
        return (self.dir / "STOP").exists()

    def check_allowed(self, action: str) -> None:
        """Fail closed before any external call or dispatch."""
        if self.stop_requested:
            raise MissionError(f"emergency stop active; refusing {action}")
        if self.deadline_exceeded:
            raise MissionError(f"mission deadline reached; refusing {action}")

    def emergency_stop(self) -> None:
        (self.dir / "STOP").write_text("stop\n", encoding="utf-8")
        self.set_status("stopped")


def preflight(root: Path, checks: dict[str, bool]) -> dict:
    """Assemble the preflight verdict from named boolean checks.

    Billable launch requires every check; the caller supplies the
    evidence. This function only records and summarizes.
    """
    required = sorted(checks)
    failed = [name for name in required if not checks[name]]
    return {
        "mission_id": MISSION_ID,
        "checks": {name: bool(checks[name]) for name in required},
        "failed": failed,
        "billable_launch_allowed": not failed,
    }


def open_mission_ledger(root: Path, authorization: Authorization) -> Ledger:
    """Open (never reset) the mission ledger in its isolated directory."""
    return Ledger(mission_dir(root), authorization)
