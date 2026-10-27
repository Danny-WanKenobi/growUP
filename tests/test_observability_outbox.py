"""SQLite outbox tests (MVP-004C-A Phase 3A): tmp databases only.

No live PostgreSQL, no Brave, no emitter integration (Phase 3B owns
delivery). Lock-contention tests use short busy timeouts; permission
bit assertions are POSIX-only with a Windows-safe chmod-call test.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.observability import (
    AgentEvent,
    AgentId,
    Outbox,
    OutboxConfigError,
    OutboxConflictError,
    OutboxUnavailableError,
    RetentionClass,
)

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


def _event(**overrides):
    base = {
        "agent": AgentId.GABRIEL,
        "run_id": "run-1",
        "event_type": "gabriel.ranking.excluded",
        "summary": "candidate excluded",
        "retention_class": RetentionClass.PERMANENT,
    }
    base.update(overrides)
    return AgentEvent.model_validate(base)


def _box(tmp_path, name="events-outbox.sqlite3", **kwargs):
    box = Outbox(tmp_path / name, **kwargs)
    box.initialize()
    return box


# 1. Schema initialization.


def test_schema_initialization(tmp_path) -> None:
    path = tmp_path / "sub" / "events-outbox.sqlite3"
    box = Outbox(path)
    assert not path.exists()  # construction performs no I/O
    assert box.initialize() == path
    assert box.initialize() == path  # idempotent
    conn = sqlite3.connect(str(path))
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "outbox_events" in tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox_events)")}
        assert columns >= {
            "event_id", "payload", "event_timestamp", "enqueued_at",
            "status", "attempts", "last_attempt_at", "last_error", "next_retry_at",
        }
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        conn.close()


# 2. Successful durable acceptance.


def test_successful_durable_acceptance(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event()
    assert box.accept(event, now=_T0) == event.event_id
    stored = box.get(event.event_id)
    assert stored is not None
    assert stored.event_id == event.event_id
    assert stored.timestamp == event.timestamp
    assert stored.retention_class is RetentionClass.PERMANENT
    assert box.get("missing-event-id-0000") is None


# 3. Persistence across close/reopen.


def test_persistence_after_reopen(tmp_path) -> None:
    path = tmp_path / "events-outbox.sqlite3"
    first = Outbox(path)
    first.initialize()
    event = _event()
    first.accept(event, now=_T0)
    del first
    second = Outbox(path)  # fresh handle, same file
    assert second.count_pending() == 1
    assert second.get(event.event_id) is not None
    assert [p.event.event_id for p in second.pending()] == [event.event_id]


# 4-5. Duplicate and conflicting IDs.


def test_duplicate_identical_event_is_idempotent(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event()
    box.accept(event, now=_T0)
    assert box.accept(event, now=_T0 + timedelta(hours=1)) == event.event_id
    assert box.count_pending() == 1


def test_conflicting_duplicate_rejected_without_replacement(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event(timestamp=_T0)
    box.accept(event, now=_T0)
    clash = _event(event_id=event.event_id, timestamp=_T0, summary="different summary")
    with pytest.raises(OutboxConflictError):
        box.accept(clash, now=_T0)
    assert box.get(event.event_id).summary == "candidate excluded"
    assert box.count_pending() == 1


# 6-8. Pending ordering, counts, oldest.


def test_pending_ordering_counts_and_oldest(tmp_path) -> None:
    box = _box(tmp_path)
    first = _event()
    second = _event()
    box.accept(second, now=_T0 + timedelta(minutes=5))
    box.accept(first, now=_T0)
    assert box.count_pending() == 2
    assert [p.event.event_id for p in box.pending()] == [first.event_id, second.event_id]
    assert box.oldest_pending_at() == _T0
    assert box.pending(limit=1)[0].event.event_id == first.event_id
    box.mark_delivered(first.event_id)
    assert box.count_pending() == 1
    assert [p.event.event_id for p in box.pending()] == [second.event_id]


def test_oldest_pending_empty_is_none(tmp_path) -> None:
    box = _box(tmp_path)
    assert box.oldest_pending_at() is None
    assert box.pending() == []
    assert box.count_pending() == 0


# 9. Marking delivered.


def test_mark_delivered_transitions_once(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event()
    box.accept(event, now=_T0)
    assert box.mark_delivered(event.event_id) is True
    assert box.mark_delivered(event.event_id) is False  # idempotent
    assert box.mark_delivered("missing-event-id-0000") is False
    assert box.count_pending() == 0
    assert box.pending() == []
    assert box.get(event.event_id) is not None  # delivered rows retained


# 10. Failed attempts and retry scheduling.


def test_failed_attempts_and_retry_scheduling(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event()
    box.accept(event, now=_T0)
    retry = _T0 + timedelta(minutes=30)
    assert box.record_attempt(event.event_id, "pg_unreachable", retry, now=_T0) is True
    assert box.pending(now=_T0 + timedelta(minutes=29)) == []  # not yet due
    due = box.pending(now=retry)
    assert [p.event.event_id for p in due] == [event.event_id]
    assert due[0].attempts == 1
    assert due[0].last_error == "pg_unreachable"
    assert due[0].next_retry_at == retry.replace(microsecond=0)
    assert box.count_pending() == 1  # backlog counts regardless of retry time
    box.record_attempt(event.event_id, "pg_unreachable\nwith newline" + "x" * 100, retry, now=_T0)
    (pending,) = box.pending(now=retry)
    assert "\n" not in (pending.last_error or "")
    assert len(pending.last_error or "") <= 64
    assert box.record_attempt("missing-event-id-0000", "x", retry) is False
    box.mark_delivered(event.event_id)
    assert box.record_attempt(event.event_id, "x", retry) is False


# 11. Lock contention.


def test_lock_contention_maps_to_unavailable(tmp_path) -> None:
    box = _box(tmp_path, busy_timeout_seconds=0.1)
    holder = sqlite3.connect(str(tmp_path / "events-outbox.sqlite3"), timeout=5.0)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO outbox_events (event_id, payload, event_timestamp, enqueued_at)"
            " VALUES ('holder-event-0001', '{}', '2026-10-27T12:00:00+00:00',"
            " '2026-10-27T12:00:00+00:00')"
        )
        with pytest.raises(OutboxUnavailableError):
            box.accept(_event(), now=_T0)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert box.count_pending() == 0  # failed accept persisted nothing


# 12. Transaction rollback.


def test_failed_write_leaves_no_partial_state(tmp_path) -> None:
    box = _box(tmp_path)
    kept = _event()
    box.accept(kept, now=_T0)
    clash = _event(event_id=kept.event_id, summary="clash")
    with pytest.raises(OutboxConflictError):
        box.accept(clash, now=_T0)
    assert box.count_pending() == 1
    assert box.get(kept.event_id).summary == "candidate excluded"
    with pytest.raises(ValueError):
        box.accept("not-an-event")  # type: ignore[arg-type]
    assert box.count_pending() == 1


# 13. Invalid database paths.


def test_invalid_paths_rejected() -> None:
    with pytest.raises(OutboxConfigError):
        Outbox(None)  # type: ignore[arg-type]
    with pytest.raises(OutboxConfigError):
        Outbox("   ")
    with pytest.raises(OutboxConfigError):
        Outbox("/tmp/x.sqlite3", busy_timeout_seconds=0)


def test_directory_path_and_unwritable_parent(tmp_path) -> None:
    with pytest.raises(OutboxConfigError):
        Outbox(tmp_path).initialize()  # path is a directory, not a file
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OutboxUnavailableError):
        Outbox(blocker / "events-outbox.sqlite3").initialize()


# 14. Corrupt database handling.


def test_corrupt_database_maps_to_unavailable(tmp_path) -> None:
    path = tmp_path / "events-outbox.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 100)
    box = Outbox(path)
    with pytest.raises(OutboxUnavailableError):
        box.initialize()
    with pytest.raises(OutboxUnavailableError):
        box.accept(_event())


def test_corrupt_payload_maps_to_unavailable(tmp_path) -> None:
    box = _box(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "events-outbox.sqlite3"))
    try:
        with conn:
            conn.execute(
                "INSERT INTO outbox_events (event_id, payload, event_timestamp, enqueued_at)"
                " VALUES ('corrupt-event-0001', 'not-json{{{',"
                " '2026-10-27T12:00:00+00:00', '2026-10-27T12:00:00+00:00')"
            )
    finally:
        conn.close()
    with pytest.raises(OutboxUnavailableError):
        box.get("corrupt-event-0001")
    with pytest.raises(OutboxUnavailableError):
        box.pending()


# 15. Rejection of nonpermanent events.


def test_nonpermanent_events_rejected(tmp_path) -> None:
    box = _box(tmp_path)
    with pytest.raises(ValueError):
        box.accept(_event(retention_class=RetentionClass.ACTIVITY))
    with pytest.raises(ValueError):
        box.accept(_event(retention_class=RetentionClass.DIAGNOSTIC))
    assert box.count_pending() == 0


# 16. Redaction of sensitive event details.


def test_stored_payload_is_redacted_and_sanitized(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event(
        summary="token Bearer abc.def.ghi here",
        details={"api_key": "live-key-1", "nested": {"password": "p", "ok": 1},
                 "evil": "a\x00b"},
        evidence_refs=[{"kind": "note", "ref": "sk-abcdefghijklmnop"}],
    )
    box.accept(event, now=_T0)
    conn = sqlite3.connect(str(tmp_path / "events-outbox.sqlite3"))
    try:
        (raw,) = conn.execute(
            "SELECT payload FROM outbox_events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
    finally:
        conn.close()
    for secret in ("live-key-1", "abc.def.ghi", "sk-abcdefghijklmnop", "\x00"):
        assert secret not in raw
    assert "***REDACTED***" in raw


# 17. Restrictive permissions.


def test_chmod_targets_recorded(tmp_path, monkeypatch) -> None:
    import app.observability.outbox as outbox_module

    calls: list = []
    real_chmod = os.chmod

    def _recording_chmod(path, mode):
        calls.append((str(path), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _recording_chmod)
    target = tmp_path / "secure" / "events-outbox.sqlite3"
    box = Outbox(target)
    box.initialize()
    box.accept(_event(), now=_T0)
    modes = {path: mode for path, mode in calls}
    assert modes.get(str(tmp_path / "secure")) == 0o700
    assert modes.get(str(target)) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_permission_bits_posix(tmp_path) -> None:
    import stat as stat_module

    target = tmp_path / "secure" / "events-outbox.sqlite3"
    box = Outbox(target)
    box.initialize()
    box.accept(_event(), now=_T0)
    for path, expected in ((tmp_path / "secure", 0o700), (target, 0o600)):
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
        assert mode == expected, (path, oct(mode))
        assert mode & 0o077 == 0


def test_preexisting_parent_permissions_untouched(tmp_path) -> None:
    parent = tmp_path / "admin-owned"
    parent.mkdir()
    box = Outbox(parent / "events-outbox.sqlite3")
    box.initialize()
    assert box.get(box.accept(_event())) is not None


# Correction 1 — production path safety.


def test_production_requires_absolute_configured_path(tmp_path) -> None:
    from app.observability import validate_production_path

    with pytest.raises(OutboxConfigError):
        validate_production_path(None)
    with pytest.raises(OutboxConfigError):
        validate_production_path("   ")
    with pytest.raises(OutboxConfigError):
        validate_production_path("outbox/events-outbox.sqlite3")  # dev default rejected
    with pytest.raises(OutboxConfigError):
        Outbox("outbox/events-outbox.sqlite3", production=True)
    with pytest.raises(OutboxConfigError):
        Outbox(None, production=True)
    allowed = validate_production_path(tmp_path / "events-outbox.sqlite3")
    assert allowed.is_absolute()
    box = Outbox(tmp_path / "events-outbox.sqlite3", production=True)
    box.initialize()
    assert box.accept(_event()) is not None


def test_production_rejects_release_tree_paths() -> None:
    from app.observability import validate_production_path

    for bad in (
        "/var/www/growup/releases/2026-10-27/outbox.sqlite3",
        "/var/www/growup/releases",
        "/var/www/growup/current/outbox.sqlite3",
        "/var/www/growup/current",
    ):
        with pytest.raises(OutboxConfigError):
            validate_production_path(bad)
        with pytest.raises(OutboxConfigError):
            Outbox(bad, production=True)
    if os.name == "posix":
        # The documented production location passes (POSIX absolute paths only).
        good = validate_production_path(
            "/var/www/growup/shared/observability/events-outbox.sqlite3"
        )
        assert str(good).endswith("events-outbox.sqlite3")


def test_production_rejects_symlink_into_release_tree(tmp_path, monkeypatch) -> None:
    import app.observability.outbox as outbox_module
    from app.observability import validate_production_path

    real_tree = tmp_path / "releases-real"
    real_tree.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_tree, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(
        outbox_module, "PRODUCTION_FORBIDDEN_PREFIXES", (real_tree,)
    )
    with pytest.raises(OutboxConfigError):
        validate_production_path(alias / "events-outbox.sqlite3")
    outside = tmp_path / "safe" / "events-outbox.sqlite3"
    assert validate_production_path(outside) == outside


def test_forbidden_prefix_matching_is_path_based(tmp_path, monkeypatch) -> None:
    import app.observability.outbox as outbox_module
    from app.observability import validate_production_path

    tree = tmp_path / "tree"
    (tree / "releases").mkdir(parents=True)
    monkeypatch.setattr(
        outbox_module, "PRODUCTION_FORBIDDEN_PREFIXES", (tree / "releases", tree / "current")
    )
    with pytest.raises(OutboxConfigError):
        validate_production_path(tree / "releases" / "r1" / "o.sqlite3")
    with pytest.raises(OutboxConfigError):
        validate_production_path(tree / "current" / "o.sqlite3")
    assert validate_production_path(tree / "shared" / "o.sqlite3")


def test_nonproduction_paths_unchanged(tmp_path) -> None:
    # Unit tests and development keep convenient relative/tmp paths.
    box = Outbox(tmp_path / "events-outbox.sqlite3")
    box.initialize()
    assert box.accept(_event()) is not None
    from pathlib import Path as _Path

    relative = Outbox("outbox/events-outbox.sqlite3")  # construction only: no I/O
    assert relative.path == _Path("outbox/events-outbox.sqlite3")


# Correction 2 — canonical event idempotency.


def test_key_ordering_does_not_cause_conflict(tmp_path) -> None:
    box = _box(tmp_path)
    first = _event(
        event_id="order-stable-event-0001",
        timestamp=_T0,
        details={"b": 1, "a": {"z": [3, 2], "y": "x"}, "c": None},
    )
    box.accept(first, now=_T0)
    reordered = _event(
        event_id="order-stable-event-0001",
        timestamp=_T0,
        details={"c": None, "a": {"y": "x", "z": [3, 2]}, "b": 1},
    )
    assert box.accept(reordered, now=_T0) == "order-stable-event-0001"
    assert box.count_pending() == 1


def test_materially_different_details_conflict(tmp_path) -> None:
    box = _box(tmp_path)
    box.accept(
        _event(event_id="conflict-event-0001", timestamp=_T0, details={"count": 1}), now=_T0
    )
    with pytest.raises(OutboxConflictError):
        box.accept(
            _event(event_id="conflict-event-0001", timestamp=_T0, details={"count": 2}), now=_T0
        )
    with pytest.raises(OutboxConflictError):
        box.accept(
            _event(event_id="conflict-event-0001", timestamp=_T0, severity="ERROR"), now=_T0
        )
    assert box.count_pending() == 1


def test_event_ids_never_rewritten(tmp_path) -> None:
    box = _box(tmp_path)
    event = _event(event_id="stable-id-event-0001")
    assert box.accept(event, now=_T0) == "stable-id-event-0001"
    assert box.get("stable-id-event-0001").event_id == "stable-id-event-0001"
    (pending,) = box.pending()
    assert pending.event.event_id == "stable-id-event-0001"
