"""IR-3C C1b: research identity-review reconciler (tests-first).

Local tests use transaction-faithful fakes and real production verdict
builders; no database, no providers, no Gabriel activation. Live tests
(``GROWUP_PG_INTEGRATION=1``) exercise the real PostgreSQL scan,
per-event transactions, and concurrency on isolated synthetic rows.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.identity_review_reconcile as reconcile
from app.agents.gabriel.dossier import DossierSet, PropertyDossier
from app.agents.gabriel.extractor import (
    ExtractedProperty,
    ResearchIntelligence,
    ResultIntelligence,
    group_by_property,
)
from app.agents.gabriel.provenance import build_dossier_verdicts, build_research_verdicts
from app.agents.gabriel.ranking import PropertyRanking, QCStatus, RankedCandidate
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity_reviews_db import IdentityReviewError

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


# --- Real-builder fixtures ----------------------------------------------------


def _disputed_intel() -> ResearchIntelligence:
    def _item(url: str, title: str, name: str) -> ResultIntelligence:
        return ResultIntelligence(
            assignment_id="a1",
            query="downtown Dallas office",
            result_url=url,
            domain="example.com",
            title=title,
            properties=[
                ExtractedProperty(
                    name=name,
                    street_address="500 Commerce St",
                    city="Dallas",
                    state="TX",
                )
            ],
        )

    return ResearchIntelligence(
        results=[
            _item("https://example.com/a", "Tower A", "Commerce Tower A"),
            _item("https://example.com/b", "Tower B", "Commerce Tower B"),
        ]
    )


def _real_disputed_verdict(run_id: str = "run-c1b-1"):
    intel = _disputed_intel()
    groups = group_by_property(intel.results)
    assert len(groups) == 1
    candidate = RankedCandidate(
        key=groups[0].key,
        qc_status=QCStatus.RESEARCH_REQUIRED,
        identity_disputes={"name": ["Commerce Tower A", "Commerce Tower B"]},
    )
    ranking = PropertyRanking(candidates=[candidate])
    verdicts = build_research_verdicts(
        run_id=run_id, correlation_id="corr-c1b-1", ranking=ranking, intel=intel
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _real_dossier_verdict(run_id: str = "run-c1b-1"):
    intel = _disputed_intel()
    groups = group_by_property(intel.results)
    assert len(groups) == 1
    dossier = PropertyDossier(
        key=groups[0].key,
        qc_status=QCStatus.RESEARCH_REQUIRED,
        identity_disputes={"name": ["Commerce Tower A", "Commerce Tower B"]},
    )
    dset = DossierSet(dossiers=[dossier])
    verdicts = build_dossier_verdicts(
        run_id=run_id,
        correlation_id="corr-c1b-1",
        dossier_set=dset,
        items=intel.results,
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _row_from_verdict(event, **overrides):
    row = {
        "event_id": event.event_id,
        "occurred_at": event.timestamp,
        "agent": event.agent.value,
        "run_id": event.run_id,
        "correlation_id": event.correlation_id,
        "property_id": None,
        "event_type": event.event_type,
        "severity": "INFO",
        "summary": event.summary,
        "details": dict(event.details),
        "evidence_refs": [],
        "parent_event_id": None,
        "retention_class": "permanent",
    }
    row.update(overrides)
    return row


# --- Transaction-faithful fakes (local, no I/O) ----------------------------------


class _FakeDB:
    """In-memory agent_events + occurrences honoring the scan contract."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.occurrences: set[str] = set()
        self.fetch_cursors: list[tuple] = []
        self.statements: list[str] = []

    def scan(self, params):
        cts, ceid = params["cursor_ts"], params["cursor_eid"]
        only = params.get("only_ids")
        self.fetch_cursors.append((cts, ceid))
        out = []
        for row in self.rows:
            if only is not None and row["event_id"] not in only:
                continue
            if row["event_type"] not in (
                "gabriel.research.candidate_decision",
                "gabriel.dossier.verdict",
            ):
                continue
            if row["retention_class"] != "permanent":
                continue
            # Mirror PostgreSQL `details->'identity_disputes'`: a
            # non-object envelope yields NULL and is never selected.
            envelope = row["details"]
            disputes = envelope.get("identity_disputes", {}) if isinstance(envelope, dict) else {}
            if disputes == {}:
                continue
            if row["event_id"] in self.occurrences:
                continue
            if not ((row["occurred_at"], row["event_id"]) > (cts, ceid)):
                continue
            out.append(row)
        out.sort(key=lambda r: (r["occurred_at"], r["event_id"]))
        return out[: params["batch"]]


class _FakeCursor:
    def __init__(self, db: _FakeDB):
        self._db = db
        self._rows: list[dict] = []
        self._probe: list[dict] = []

    def execute(self, sql, params=None):  # type: ignore[no-untyped-def]
        self._db.statements.append(sql)
        params = params or {}
        if sql.startswith("SELECT EXISTS"):
            probe = dict(params)
            probe["batch"] = 1
            self._probe = [{"more": bool(self._db.scan(probe))}]
        elif "FROM agent_events" in sql:
            self._rows = self._db.scan(params)
        elif "FROM identity_review_occurrences" in sql:
            eid = params.get("event_id")
            self._probe = [{"one": 1}] if eid in self._db.occurrences else []
        else:  # pragma: no cover - reconciler issues only these SELECTs
            raise AssertionError(f"unexpected SQL in fake: {sql[:80]!r}")

    def fetchall(self):  # type: ignore[no-untyped-def]
        return list(self._rows)

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self._probe[0] if self._probe else None

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        return False


class _FakeConn:
    def __init__(self, db: _FakeDB):
        self._db = db
        self.committed = False
        self.rolled_back = False

    def cursor(self):  # type: ignore[no-untyped-def]
        return _FakeCursor(self._db)

    def commit(self):  # type: ignore[no-untyped-def]
        self.committed = True

    def rollback(self):  # type: ignore[no-untyped-def]
        self.rolled_back = True

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        return False


def _fake_factory(db: _FakeDB, conns: list):  # type: ignore[no-untyped-def]
    def _connect(config=None):
        conn = _FakeConn(db)
        conns.append(conn)
        return conn

    return _connect


def _install(db, monkeypatch, writer=None):  # type: ignore[no-untyped-def]
    """Wire fakes: scan DB + (default) recording writer."""
    conns: list[_FakeConn] = []
    monkeypatch.setattr(reconcile, "get_connection", _fake_factory(db, conns))
    calls: list[str] = []

    def _record(conn, event):
        calls.append(event.event_id)
        db.occurrences.add(event.event_id)
        return f"review-{event.event_id}"

    monkeypatch.setattr(
        reconcile, "record_review_occurrence", writer if writer is not None else _record
    )
    return conns, calls


# --- Local: selection and pagination -----------------------------------------------


def test_scan_sql_selects_dispute_claiming_permanent_verdicts() -> None:
    sql = reconcile._scan_sql(only_ids=False)
    assert "FROM agent_events" in sql
    assert "gabriel.research.candidate_decision" in sql
    assert "gabriel.dossier.verdict" in sql
    assert "permanent" in sql
    assert "identity_disputes" in sql
    assert "'{}'" in sql, "empty-dispute objects must be excluded in SQL"
    assert "jsonb_typeof" not in sql, "malformed shapes must scan, not vanish"
    assert "identity_review_occurrences" in sql
    assert "NOT EXISTS" in sql
    assert "ORDER BY" in sql and "occurred_at ASC" in sql and "event_id ASC" in sql
    assert "(e.occurred_at, e.event_id) > " in sql


def test_research_verdict_selected_and_reconciled(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    conns, calls = _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 1
    assert report.reconciled == 1
    assert report.failed == [] and report.total_failed == 0
    assert calls == [event.event_id]
    assert len(conns) == 3, "scan, verdict, then terminal empty-page scan"
    assert conns[1].committed and not conns[1].rolled_back


def test_dossier_verdict_selected_and_reconciled(monkeypatch) -> None:
    event = _real_dossier_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert (report.examined, report.reconciled) == (1, 1)


def test_clean_verdicts_excluded_from_scan(monkeypatch) -> None:
    event = _real_disputed_verdict()
    details = dict(event.details)
    details["identity_disputes"] = {}
    db = _FakeDB([_row_from_verdict(event, details=details)])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 0, "empty disputes must not be selected"


def test_malformed_nonempty_disputes_selected_and_reported(monkeypatch) -> None:
    good = _real_disputed_verdict(run_id="run-good")
    bad = _real_disputed_verdict(run_id="run-bad")
    details = dict(bad.details)
    details["identity_disputes"] = "not-a-mapping"
    db = _FakeDB([_row_from_verdict(bad, details=details), _row_from_verdict(good)])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 2
    assert report.reconciled == 1
    assert report.total_failed == 1
    assert report.failed[0].event_id == bad.event_id
    assert report.failed[0].reason, "failure must carry a reason"


def test_keyset_pagination_spans_batches(monkeypatch) -> None:
    events = [_real_disputed_verdict(run_id=f"run-{i}") for i in range(5)]
    rows = [_row_from_verdict(e) for e in events]
    db = _FakeDB(rows)
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=2)
    assert report.examined == 5
    assert report.reconciled == 5
    assert len(db.fetch_cursors) == 4, "2+2+1 rows then an empty page"
    ordered = sorted(rows, key=lambda r: (r["occurred_at"], r["event_id"]))
    assert db.fetch_cursors[1] == (ordered[1]["occurred_at"], ordered[1]["event_id"])


def test_cursor_advances_past_failed_events(monkeypatch) -> None:
    bad = _real_disputed_verdict(run_id="run-bad")
    details = dict(bad.details)
    details["identity_disputes"] = {"name": "scalar-not-list"}
    bad_row = _row_from_verdict(bad, details=details)
    good = _real_disputed_verdict(run_id="run-good")
    good_row = _row_from_verdict(good)
    first, second = sorted([bad_row, good_row], key=lambda r: (r["occurred_at"], r["event_id"]))
    db = _FakeDB([first, second])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=1)
    assert report.examined == 2
    assert report.reconciled == 1 and report.total_failed == 1
    assert db.fetch_cursors[1] == (first["occurred_at"], first["event_id"])


def test_full_sweep_completes_despite_failed_prefix(monkeypatch) -> None:
    rows = []
    for i in range(3):
        bad = _real_disputed_verdict(run_id=f"run-bad-{i}")
        details = dict(bad.details)
        details["identity_disputes"] = ["not", "a", "mapping"]
        rows.append(_row_from_verdict(bad, details=details))
    rows.append(_row_from_verdict(_real_disputed_verdict(run_id="run-good")))
    db = _FakeDB(rows)
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=2)
    assert report.examined == 4
    assert report.reconciled == 1
    assert report.total_failed == 3
    assert report.more_may_remain is False


def test_successes_excluded_on_subsequent_sweeps(monkeypatch) -> None:
    events = [_real_disputed_verdict(run_id=f"run-{i}") for i in range(2)]
    db = _FakeDB([_row_from_verdict(e) for e in events])
    _install(db, monkeypatch)
    first = reconcile.reconcile_once(batch_size=10)
    assert first.reconciled == 2
    second = reconcile.reconcile_once(batch_size=10)
    assert second.examined == 0
    assert second.reconciled == 0


# --- Local: reconstruction and writer contract ----------------------------------------


def test_event_reconstruction_fidelity() -> None:
    event = _real_disputed_verdict(run_id="run-fidelity")
    rebuilt = reconcile._row_to_event(_row_from_verdict(event))
    assert rebuilt.event_id == event.event_id
    assert rebuilt.event_type == event.event_type
    assert rebuilt.run_id == event.run_id
    assert rebuilt.details == event.details
    assert rebuilt.retention_class == event.retention_class
    assert rebuilt.property_id is None
    assert rebuilt.timestamp == event.timestamp


def test_corrupt_row_fails_with_event_id(monkeypatch) -> None:
    event = _real_disputed_verdict()
    row = _row_from_verdict(event, evidence_refs=[{"missing": "kind-and-ref"}])
    db = _FakeDB([row])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 1 and report.reconciled == 0
    assert report.total_failed == 1
    assert report.failed[0].event_id == event.event_id


def test_non_object_details_envelope_not_selected(monkeypatch) -> None:
    event = _real_disputed_verdict()
    row = _row_from_verdict(event, details=["not", "a", "dict"])
    db = _FakeDB([row])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 0, "PG `->` on a non-object yields NULL: not selected"


def test_writer_none_for_dispute_claiming_event_is_failure(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])

    def _decline(conn, evt):  # type: ignore[no-untyped-def]
        return None

    _install(db, monkeypatch, writer=_decline)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.examined == 1 and report.reconciled == 0
    assert report.total_failed == 1
    assert report.failed[0].event_id == event.event_id
    assert "declined" in report.failed[0].reason


def test_review_context_error_is_reported_not_raised(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    _install(db, monkeypatch)

    def _broken(evt):  # type: ignore[no-untyped-def]
        raise IdentityReviewError("synthetic malformed verdict")

    monkeypatch.setattr(reconcile, "review_context_from_event", _broken)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.total_failed == 1
    assert "synthetic malformed verdict" in report.failed[0].reason


# --- Local: transactions, races, lost acks -----------------------------------------------


def test_database_error_rolls_back_and_reports(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    conns, _ = _install(db, monkeypatch)

    def _boom(conn, evt):  # type: ignore[no-untyped-def]
        raise DatabaseUnavailableError("synthetic outage")

    monkeypatch.setattr(reconcile, "record_review_occurrence", _boom)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.total_failed == 1
    assert report.reconciled == 0
    assert conns[1].rolled_back and not conns[1].committed


def test_lost_commit_ack_becomes_already_reconciled(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    conns: list[_FakeConn] = []

    class _AckLosingConn(_FakeConn):
        def commit(self):  # type: ignore[no-untyped-def]
            db.occurrences.add(event.event_id)  # commit landed; ack lost
            raise DatabaseUnavailableError("synthetic lost ack")

    def _connect(config=None):
        conn = _AckLosingConn(db)
        conns.append(conn)
        return conn

    monkeypatch.setattr(reconcile, "get_connection", _connect)
    monkeypatch.setattr(
        reconcile, "record_review_occurrence", lambda conn, evt: f"review-{evt.event_id}"
    )
    report = reconcile.reconcile_once(batch_size=10)
    assert report.reconciled == 0
    assert report.already_reconciled == 1, "probe must recognize the landed commit"
    assert report.total_failed == 0


def test_uniqueness_race_becomes_already_reconciled(monkeypatch) -> None:
    event = _real_disputed_verdict()
    db = _FakeDB([_row_from_verdict(event)])
    _install(db, monkeypatch)

    def _race(conn, evt):  # type: ignore[no-untyped-def]
        db.occurrences.add(evt.event_id)  # rival reconciler won first
        raise DatabaseUnavailableError("synthetic unique violation")

    monkeypatch.setattr(reconcile, "record_review_occurrence", _race)
    report = reconcile.reconcile_once(batch_size=10)
    assert report.already_reconciled == 1
    assert report.total_failed == 0


# --- Local: report, CLI, safety -------------------------------------------------------------


def test_report_counts_and_truncation(monkeypatch) -> None:
    rows = []
    for i in range(25):
        bad = _real_disputed_verdict(run_id=f"run-bad-{i}")
        details = dict(bad.details)
        details["identity_disputes"] = 42
        rows.append(_row_from_verdict(bad, details=details))
    rows.append(_row_from_verdict(_real_disputed_verdict(run_id="run-good")))
    db = _FakeDB(rows)
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=50)
    assert report.examined == 26
    assert report.reconciled == 1
    assert report.total_failed == 25
    assert len(report.failed) == reconcile.MAX_FAILURE_SAMPLE
    assert report.omitted_failures == 25 - reconcile.MAX_FAILURE_SAMPLE
    line = report.summary_line()
    assert "examined=26" in line and "reconciled=1" in line


def test_cli_exit_codes(monkeypatch, capsys) -> None:
    ok = reconcile.ReconcileReport(
        examined=1, reconciled=1, already_reconciled=0, skipped_ineligible=0,
        failed=[], omitted_failures=0, more_may_remain=False, status="ok",
    )
    monkeypatch.setattr(reconcile, "reconcile_once", lambda **kw: ok)
    assert reconcile.main([]) == 0
    partial = reconcile.ReconcileReport(
        examined=1, reconciled=0, already_reconciled=0, skipped_ineligible=0,
        failed=[reconcile.ReconcileFailure("e1", "boom")],
        omitted_failures=0, more_may_remain=False, status="partial",
    )
    monkeypatch.setattr(reconcile, "reconcile_once", lambda **kw: partial)
    assert reconcile.main([]) == 1
    out = capsys.readouterr().out
    assert "e1" in out and "boom" in out

    def _not_configured(**kw):  # type: ignore[no-untyped-def]
        raise DatabaseNotConfiguredError("nope")

    monkeypatch.setattr(reconcile, "reconcile_once", _not_configured)
    assert reconcile.main([]) == 2

    def _outage(**kw):  # type: ignore[no-untyped-def]
        raise DatabaseUnavailableError("down")

    monkeypatch.setattr(reconcile, "reconcile_once", _outage)
    assert reconcile.main([]) == 3


def test_operator_limit_truncates_and_reports_more(monkeypatch) -> None:
    events = [_real_disputed_verdict(run_id=f"run-{i}") for i in range(3)]
    db = _FakeDB([_row_from_verdict(e) for e in events])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10, limit=2)
    assert report.examined == 2
    assert report.reconciled == 2
    assert report.more_may_remain is True


def test_limit_covering_backlog_reports_no_more(monkeypatch) -> None:
    events = [_real_disputed_verdict(run_id=f"run-{i}") for i in range(2)]
    db = _FakeDB([_row_from_verdict(e) for e in events])
    _install(db, monkeypatch)
    report = reconcile.reconcile_once(batch_size=10, limit=5)
    assert report.examined == 2
    assert report.more_may_remain is False


def test_scan_outage_propagates_for_cli_mapping(monkeypatch) -> None:
    def _down(config=None):
        raise DatabaseUnavailableError("synthetic scan outage")

    monkeypatch.setattr(reconcile, "get_connection", _down)
    with pytest.raises(DatabaseUnavailableError):
        reconcile.reconcile_once(batch_size=10)

    def _unconfigured(config=None):
        raise DatabaseNotConfiguredError("synthetic missing config")

    monkeypatch.setattr(reconcile, "get_connection", _unconfigured)
    with pytest.raises(DatabaseNotConfiguredError):
        reconcile.reconcile_once(batch_size=10)


def test_cli_rejects_nonpositive_batch_and_limit() -> None:
    assert reconcile.main(["--batch-size", "0"]) == 2
    assert reconcile.main(["--limit", "0"]) == 2


def test_cli_reads_env_defaults(monkeypatch) -> None:
    seen: dict = {}

    def _capture(**kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return reconcile.ReconcileReport(status="ok")

    monkeypatch.setattr(reconcile, "reconcile_once", _capture)
    monkeypatch.setenv("GROWUP_RECONCILE_BATCH", "11")
    monkeypatch.setenv("GROWUP_RECONCILE_LIMIT", "13")
    assert reconcile.main([]) == 0
    assert seen["batch_size"] == 11
    assert seen["limit"] == 13
    monkeypatch.setenv("GROWUP_RECONCILE_BATCH", "bogus")
    assert reconcile.main([]) == 2


def test_cli_passes_batch_size_and_limit(monkeypatch) -> None:
    seen: dict = {}

    def _capture(**kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return reconcile.ReconcileReport(
            examined=0, reconciled=0, already_reconciled=0, skipped_ineligible=0,
            failed=[], omitted_failures=0, more_may_remain=False, status="ok",
        )

    monkeypatch.setattr(reconcile, "reconcile_once", _capture)
    assert reconcile.main(["--batch-size", "7", "--limit", "9"]) == 0
    assert seen["batch_size"] == 7
    assert seen["limit"] == 9


def test_reconciler_never_writes_agent_events(monkeypatch) -> None:
    events = [_real_disputed_verdict(run_id=f"run-{i}") for i in range(2)]
    db = _FakeDB([_row_from_verdict(e) for e in events])
    _install(db, monkeypatch)
    reconcile.reconcile_once(batch_size=10)
    for statement in db.statements:
        upper = statement.upper()
        assert "AGENT_EVENTS" not in upper or upper.lstrip().startswith("SELECT"), (
            f"agent_events must be read-only: {statement[:60]!r}"
        )


def test_reconciler_uses_no_sqlite() -> None:
    import pathlib

    source = pathlib.Path(reconcile.__file__).read_text(encoding="utf-8").lower()
    assert "sqlite" not in source
    assert "outbox" not in source


def test_reconciler_imports_no_gabriel_pipeline() -> None:
    import pathlib

    source = pathlib.Path(reconcile.__file__).read_text(encoding="utf-8")
    assert "app.agents" not in source
    assert "Brave" not in source and "brave" not in source


# --- Live PostgreSQL (gated) ---------------------------------------------------------------------


def _live_config() -> None:
    from app.properties_db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor():
                pass
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unavailable for C1b live tests: {exc}")


def _live_require_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.identity_reviews') AS reviews, "
                "to_regclass('public.identity_review_occurrences') AS occ"
            )
            row = cur.fetchone()
    if row is None or row["reviews"] is None or row["occ"] is None:
        pytest.skip("identity_reviews tables not present (migration 009 not applied)")


def _live_insert_verdict(event) -> None:  # type: ignore[no-untyped-def]
    from app.observability.store import insert_event

    insert_event(event)


def _live_cleanup(event_ids: list[str]) -> None:
    from app.properties_db import get_connection

    eids = [str(e) for e in event_ids if e]
    if not eids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT review_id AS rid FROM identity_review_occurrences "
                "WHERE event_id = ANY(%(ids)s)",
                {"ids": eids},
            )
            rids = [str(r["rid"]) for r in cur.fetchall()]
            cur.execute(
                "DELETE FROM identity_review_occurrences WHERE event_id = ANY(%(ids)s)",
                {"ids": eids},
            )
            if rids:
                cur.execute(
                    "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
                    {"ids": rids},
                )
            cur.execute(
                "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)", {"ids": eids}
            )
        conn.commit()


def _live_review_for_event(event_id: str):  # type: ignore[no-untyped-def]
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.* FROM identity_reviews r "
                "JOIN identity_review_occurrences o USING (review_id) "
                "WHERE o.event_id = %(eid)s",
                {"eid": event_id},
            )
            return cur.fetchone()


@needs_live_pg
def test_live_research_dispute_reconciled() -> None:
    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    try:
        _live_insert_verdict(event)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert report.examined == 1
        assert report.reconciled == 1
        row = _live_review_for_event(event.event_id)
        assert row is not None
        assert row["source"] == "research"
        assert row["candidate_key"] == event.details["candidate_ref"]["group_key"]
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_dossier_dispute_reconciled() -> None:
    _live_config()
    _live_require_tables()
    event = _real_dossier_verdict(run_id=f"run-{uuid4().hex}")
    try:
        _live_insert_verdict(event)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert report.reconciled == 1
        assert _live_review_for_event(event.event_id) is not None
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_research_and_dossier_join_one_case() -> None:
    _live_config()
    _live_require_tables()
    research = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    dossier = _real_dossier_verdict(run_id=f"run-{uuid4().hex}")
    eids = [research.event_id, dossier.event_id]
    try:
        _live_insert_verdict(research)
        _live_insert_verdict(dossier)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=eids)
        assert report.reconciled == 2
        first = _live_review_for_event(research.event_id)
        second = _live_review_for_event(dossier.event_id)
        assert first["review_id"] == second["review_id"]
        assert first["occurrence_count"] == 2
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_repeated_runs_are_idempotent() -> None:
    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    try:
        _live_insert_verdict(event)
        first = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert first.reconciled == 1
        second = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert second.examined == 0
        assert second.reconciled == 0
        assert _live_review_for_event(event.event_id)["occurrence_count"] == 1
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_concurrent_same_event_reconciles_once(monkeypatch) -> None:
    import threading

    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    barrier = threading.Barrier(2)
    gate = threading.Barrier(2)
    real_record = reconcile.record_review_occurrence

    def _gated_record(conn, evt):  # type: ignore[no-untyped-def]
        # Both workers must select the event before either commits;
        # without this rendezvous the loser could scan after the win.
        gate.wait(timeout=30)
        return real_record(conn, evt)

    monkeypatch.setattr(reconcile, "record_review_occurrence", _gated_record)
    reports: list = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=30)
            reports.append(
                reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        _live_insert_verdict(event)
        workers = [threading.Thread(target=_worker) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert not errors
        assert len(reports) == 2
        assert sum(r.reconciled for r in reports) == 1
        assert sum(r.already_reconciled for r in reports) == 1
        assert _live_review_for_event(event.event_id)["occurrence_count"] == 1
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_concurrent_same_case_counts_every_occurrence() -> None:
    import threading

    _live_config()
    _live_require_tables()
    first = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    second = _real_dossier_verdict(run_id=f"run-{uuid4().hex}")
    eids = [first.event_id, second.event_id]
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _worker(event):  # type: ignore[no-untyped-def]
        try:
            barrier.wait(timeout=30)
            reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        _live_insert_verdict(first)
        _live_insert_verdict(second)
        workers = [threading.Thread(target=_worker, args=(e,)) for e in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert not errors
        row = _live_review_for_event(first.event_id)
        assert row["review_id"] == _live_review_for_event(second.event_id)["review_id"]
        assert row["occurrence_count"] == 2
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_malformed_event_isolated() -> None:
    _live_config()
    _live_require_tables()
    good = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    bad = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    details = dict(bad.details)
    details["identity_disputes"] = "not-a-mapping"
    bad = bad.model_copy(update={"details": details})
    eids = [good.event_id, bad.event_id]
    try:
        _live_insert_verdict(good)
        _live_insert_verdict(bad)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=eids)
        assert report.examined == 2
        assert report.reconciled == 1
        assert report.total_failed == 1
        assert report.failed[0].event_id == bad.event_id
        assert _live_review_for_event(good.event_id) is not None
        assert _live_review_for_event(bad.event_id) is None
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_valid_events_behind_malformed_prefix() -> None:
    _live_config()
    _live_require_tables()
    eids: list[str] = []
    try:
        for i in range(5):
            bad = _real_disputed_verdict(run_id=f"run-{uuid4().hex}-bad-{i}")
            details = dict(bad.details)
            details["identity_disputes"] = ["not", "a", "mapping"]
            bad = bad.model_copy(update={"details": details})
            _live_insert_verdict(bad)
            eids.append(bad.event_id)
        goods = [
            _real_disputed_verdict(run_id=f"run-{uuid4().hex}-good-{i}") for i in range(2)
        ]
        for good in goods:
            _live_insert_verdict(good)
            eids.append(good.event_id)
        report = reconcile.reconcile_once(batch_size=2, only_event_ids=eids)
        assert report.examined == 7
        assert report.reconciled == 2
        assert report.total_failed == 5
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_failures_retry_on_second_invocation() -> None:
    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    details = dict(event.details)
    details["identity_disputes"] = {"name": "scalar-not-list"}
    event = event.model_copy(update={"details": details})
    try:
        _live_insert_verdict(event)
        first = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert first.total_failed == 1
        second = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert second.examined == 1, "failures must be retried, never skipped"
        assert second.total_failed == 1
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_timestamp_ties_reconciled_exactly_once() -> None:
    _live_config()
    _live_require_tables()
    stamp = datetime.now(timezone.utc)
    events = []
    for i in range(3):
        event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}-tie-{i}")
        events.append(event.model_copy(update={"timestamp": stamp}))
    eids = [e.event_id for e in events]
    try:
        for event in events:
            _live_insert_verdict(event)
        report = reconcile.reconcile_once(batch_size=1, only_event_ids=eids)
        assert report.examined == 3
        assert report.reconciled == 3
        for event in events:
            assert _live_review_for_event(event.event_id) is not None
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_mid_sweep_arrival_recovered_next_run(monkeypatch) -> None:
    _live_config()
    _live_require_tables()
    first = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    late = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    older = first.timestamp - timedelta(seconds=1)
    late = late.model_copy(update={"timestamp": older})
    assert late.timestamp < first.timestamp
    eids = [first.event_id, late.event_id]
    real_fetch = reconcile._fetch_batch
    state = {"inserted": False}

    def _fetch_then_arrive(cursor_ts, cursor_eid, batch_size, only_ids, config=None):  # type: ignore[no-untyped-def]
        rows = real_fetch(cursor_ts, cursor_eid, batch_size, only_ids, config)
        if not state["inserted"]:
            state["inserted"] = True
            _live_insert_verdict(late)
        return rows

    monkeypatch.setattr(reconcile, "_fetch_batch", _fetch_then_arrive)
    try:
        _live_insert_verdict(first)
        run1 = reconcile.reconcile_once(batch_size=1, only_event_ids=eids)
        assert run1.reconciled == 1
        assert _live_review_for_event(first.event_id) is not None
        assert _live_review_for_event(late.event_id) is None
        run2 = reconcile.reconcile_once(batch_size=10, only_event_ids=eids)
        assert run2.reconciled == 1
        assert _live_review_for_event(late.event_id) is not None
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_per_event_transaction_isolation(monkeypatch) -> None:
    _live_config()
    _live_require_tables()
    good = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    poisoned = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    eids = [good.event_id, poisoned.event_id]
    real_record = reconcile.record_review_occurrence

    def _poisoned_record(conn, event):  # type: ignore[no-untyped-def]
        if event.event_id == poisoned.event_id:
            raise DatabaseUnavailableError("synthetic per-event outage")
        return real_record(conn, event)

    monkeypatch.setattr(reconcile, "record_review_occurrence", _poisoned_record)
    try:
        _live_insert_verdict(good)
        _live_insert_verdict(poisoned)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=eids)
        assert report.examined == 2
        assert report.reconciled == 1
        assert report.total_failed == 1
        assert _live_review_for_event(good.event_id) is not None
        assert _live_review_for_event(poisoned.event_id) is None
    finally:
        _live_cleanup(eids)


@needs_live_pg
def test_live_original_events_immutable() -> None:
    from app.properties_db import get_connection

    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")

    def _snapshot():  # type: ignore[no-untyped-def]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM agent_events WHERE event_id = %(eid)s",
                            {"eid": event.event_id})
                return cur.fetchone()

    try:
        _live_insert_verdict(event)
        before = _snapshot()
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert report.reconciled == 1
        assert _snapshot() == before
    finally:
        _live_cleanup([event.event_id])


@needs_live_pg
def test_live_fixture_cleanup_removes_synthetic_rows() -> None:
    from app.properties_db import get_connection

    _live_config()
    _live_require_tables()
    event = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    try:
        _live_insert_verdict(event)
        report = reconcile.reconcile_once(batch_size=10, only_event_ids=[event.event_id])
        assert report.reconciled == 1
        _live_cleanup([event.event_id])
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM agent_events WHERE event_id = %(eid)s",
                            {"eid": event.event_id})
                assert cur.fetchone()["n"] == 0
                cur.execute("SELECT COUNT(*) AS n FROM identity_review_occurrences "
                            "WHERE event_id = %(eid)s", {"eid": event.event_id})
                assert cur.fetchone()["n"] == 0
    finally:
        _live_cleanup([event.event_id])
