"""JSON Lines diagnostic file sink (MVP-004C-A).

One JSON object per line, UTC timestamps, size-rotated files with a
bounded backup count. Thread-safe via the stdlib logging locks. The
sink never raises and never emits observability events itself, so
logging failures cannot recurse: they are counted, stored, and echoed
to stderr instead.

Multi-process note: rotation assumes a single writer process (the
current deployment runs one service worker). Multi-worker setups must
use separate log files per worker.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.observability.config import ObservabilityConfig, get_observability_config

LOG_BASENAME = "agent-events.jsonl"

_SEVERITY_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}

_ROTATED_RE = re.compile(r"^agent-events\.jsonl(?:\.(\d+))?$")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = record.msg if isinstance(record.msg, dict) else {"message": str(record.msg)}
        return json.dumps(payload, default=str, ensure_ascii=True)


class JsonlSink:
    """Bounded, non-raising JSONL writer with observable failure status."""

    def __init__(self, config: ObservabilityConfig | None = None) -> None:
        self._config = config or get_observability_config()
        self._lock = threading.Lock()
        self._written = 0
        self._dropped = 0
        self._errors = 0
        self._last_error: str | None = None
        self._logger: logging.Logger | None = None
        self._path: str | None = None
        self._open()

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._errors += 1
            self._last_error = message[:500]
        print(f"observability file sink error: {message[:500]}", file=sys.stderr)

    def _open(self) -> None:
        try:
            log_dir = Path(self._config.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / LOG_BASENAME
            logger = logging.getLogger(f"growup.observability.{id(self)}")
            logger.setLevel(self._config.log_level)
            logger.propagate = False
            handler = logging.handlers.RotatingFileHandler(
                str(path),
                maxBytes=self._config.log_max_bytes,
                backupCount=self._config.log_backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(_JsonFormatter())
            logger.addHandler(handler)
            # RotatingFileHandler opens the file in its constructor, so an
            # unwritable path already raises here instead of dropping later.
            self._logger = logger
            self._path = str(path)
        except Exception as exc:
            self._record_error(f"{type(exc).__name__}: {exc}")

    @property
    def path(self) -> str | None:
        """Active log file path, or None when the sink is degraded."""
        return self._path

    def write(self, record: dict[str, Any]) -> bool:
        """Append one event record. Returns False (never raises) on failure."""
        logger = self._logger
        if logger is None:
            self._record_error("sink unavailable; event dropped")
            return False
        level = _SEVERITY_LEVELS.get(str(record.get("severity", "INFO")), logging.INFO)
        if level < logger.getEffectiveLevel():
            with self._lock:
                self._dropped += 1
            return True
        try:
            logger.log(level, record)
            with self._lock:
                self._written += 1
            return True
        except Exception as exc:  # never let logging break the caller
            self._record_error(f"{type(exc).__name__}: {exc}")
            return False

    def status(self) -> dict[str, Any]:
        """Observable sink counters (writes, drops, errors, last error)."""
        with self._lock:
            return {
                "path": self._path,
                "written": self._written,
                "dropped_below_level": self._dropped,
                "errors": self._errors,
                "last_error": self._last_error,
            }

    def close(self) -> None:
        """Flush and detach handlers (tests and graceful shutdown)."""
        logger, self._logger = self._logger, None
        if logger is None:
            return
        for handler in list(logger.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
            logger.removeHandler(handler)


_sinks_lock = threading.Lock()
_sinks: dict[str, JsonlSink] = {}


def get_sink(config: ObservabilityConfig | None = None) -> JsonlSink:
    """Return the cached sink for a log directory (one writer per dir)."""
    cfg = config or get_observability_config()
    with _sinks_lock:
        sink = _sinks.get(cfg.log_dir)
        if sink is None:
            sink = JsonlSink(cfg)
            _sinks[cfg.log_dir] = sink
        return sink


def reset_sinks() -> None:
    """Close and drop all cached sinks (tests only)."""
    with _sinks_lock:
        sinks = list(_sinks.values())
        _sinks.clear()
    for sink in sinks:
        sink.close()


def purge_old_log_files(
    log_dir: str | Path, retention_days: int, now: float | None = None
) -> list[str]:
    """Delete rotated agent-event logs older than retention. Never raises.

    Only files exactly matching the sink's own naming scheme are
    candidates; unrelated files are never touched. Returns deleted paths.
    """
    if retention_days < 1:
        return []
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    deleted: list[str] = []
    try:
        entries = list(Path(log_dir).iterdir())
    except OSError as exc:
        print(f"observability log purge error: {exc}", file=sys.stderr)
        return []
    for entry in entries:
        if not _ROTATED_RE.match(entry.name):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                deleted.append(str(entry))
        except OSError as exc:
            print(f"observability log purge error: {entry}: {exc}", file=sys.stderr)
    return deleted
