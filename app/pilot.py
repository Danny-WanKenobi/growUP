"""Resumable 36-cell pilot ledger (migration 013).

Twelve counties x three categories, four qualified dossiers per cell.
The pilot tracks outcomes recorded by operator-driven discovery work;
it never executes discovery itself and never spends budget. Planning
is a pure read: it orders cells by truthful shortfall and annotates
each candidate action with its estimated cost and a budget verdict.

Stage counters are kept separate per cell: discovered, qualified,
assessed, authorized, incomplete, rejected, michael_ready (legacy),
plus engineering_assessed, financially_assessed, package_prepared,
tracked_actual, tracked_simulated for the connected canonical
pipeline. Each (dossier, transition) records at most once, so replays
and resumed runs cannot inflate counts. Category weights start equal
and only order otherwise-tied cells; they never exclude work.

Stage hooks live in the agents themselves (Gabriel persistence,
Raphael decisions, Onesimus scenarios, Uriel packages, Priscilla
tracking) so the ledger reflects actual processing, not manual
counting. Every hook keys events by the canonical operational
dossier identity; run IDs travel as provenance only.

Commands (operator-invoked, from the repository root)::

    python -m app.pilot status
    python -m app.pilot plan [--max N] [--json]
    python -m app.pilot stop
    python -m app.pilot resume
"""

from __future__ import annotations

import json
import sys
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER, display_name
from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection
from app.research_budget_db import BudgetConfigError, read_budget
from app.workflow import is_demo_id

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

PILOT_CATEGORIES = ("high_rise", "commercial_property", "warehouse_industrial")
TRANSITIONS = (
    "discovered",
    "qualified",
    "assessed",
    "authorized",
    "incomplete",
    "rejected",
    "michael_ready",
    "engineering_assessed",
    "financially_assessed",
    "package_prepared",
    "tracked_actual",
    "tracked_simulated",
)
TARGET_QUALIFIED_PER_CELL = 4
PILOT_CELL_COUNT = 36
PILOT_QUALIFIED_TARGET = 144

# Conservative estimate for one candidate action: a bounded research run.
EST_BRAVE_REQUESTS_PER_ACTION = 3


class PilotError(ValueError):
    """Invalid pilot request (unknown cell, stopped pilot, bad weights)."""


class PilotCell(BaseModel):
    """One county/category ledger cell with separate stage counters."""

    county: str = ""
    category: str = ""
    target_qualified: int = TARGET_QUALIFIED_PER_CELL
    discovered: int = 0
    qualified: int = 0
    assessed: int = 0
    authorized: int = 0
    incomplete: int = 0
    rejected: int = 0
    michael_ready: int = 0
    engineering_assessed: int = 0
    financially_assessed: int = 0
    package_prepared: int = 0
    tracked_actual: int = 0
    tracked_simulated: int = 0


def _wrap_driver(exc: Exception) -> DatabaseUnavailableError:
    return DatabaseUnavailableError("pilot_unavailable")


def _county_index(county: str) -> int:
    return list(COUNTY_PRIORITY_ORDER).index(county)


def normalize_county(county: str) -> str:
    """Accept canonical or display county form; return canonical or refuse."""
    wanted = (county or "").strip().lower()
    if wanted in COUNTY_PRIORITY_ORDER:
        return wanted
    for canonical in COUNTY_PRIORITY_ORDER:
        if display_name(canonical).lower() == wanted:
            return canonical
    raise PilotError(f"unknown pilot county: {county!r}")


def normalize_category(category: str) -> str:
    wanted = (category or "").strip().lower()
    if wanted not in PILOT_CATEGORIES:
        raise PilotError(f"unknown pilot category: {category!r}")
    return wanted


def check_weights(weights: dict[str, Any]) -> dict[str, float]:
    """Validate category weights: exactly the three keys, positive numbers."""
    if not isinstance(weights, dict) or set(weights) != set(PILOT_CATEGORIES):
        raise PilotError("weights must cover exactly the three pilot categories")
    cleaned: dict[str, float] = {}
    for key in PILOT_CATEGORIES:
        try:
            value = float(weights[key])
        except (TypeError, ValueError) as exc:
            raise PilotError(f"weight for {key} is not a number") from exc
        if not value > 0:
            raise PilotError(f"weight for {key} must be positive")
        cleaned[key] = value
    return cleaned


def default_weights() -> dict[str, float]:
    """Equal initial weights: no category gets an automatic advantage."""
    return {key: 1.0 for key in PILOT_CATEGORIES}


def canonical_quota_id(property_id: UUID | str) -> UUID:
    """Canonical dossier UUID for pilot quota accounting.

    Quota credit keys on the shared operational dossier identity
    (``dossier_id_for_property``), never on a run-scoped derivation:
    re-discovering the same property in another run records no second
    credit. Demo and non-UUID identifiers are refused.
    """
    from app.custody import CustodyError, dossier_id_for_property

    try:
        return dossier_id_for_property(property_id)
    except CustodyError as exc:
        raise PilotError(str(exc)) from exc


def cell_from_dossier(dossier: dict[str, Any]) -> tuple[str, str] | None:
    """Extract the pilot cell from a dossier dump, or None if unknown.

    County requires a verified attested county name; category accepts a
    verified or observed canonical category. Anything else (unknown,
    contradictory, non-pilot) returns None: unknown cells earn no
    quota credit but remain researchable elsewhere.
    """
    if not isinstance(dossier, dict):
        return None
    try:
        county_attr = dossier.get("county_attribution") or {}
        county = str(county_attr.get("value", "") or "")
        county_state = str(county_attr.get("evidence_status", "") or "").lower()
        if county_state != "verified":
            return None
        cell_county = normalize_county(county)
        cat_attr = dossier.get("category_attribution") or {}
        category = str(cat_attr.get("verified") or cat_attr.get("observed") or "")
        cell_category = normalize_category(category)
    except (PilotError, AttributeError, TypeError, ValueError):
        return None
    return (cell_county, cell_category)


def canonical_qualified_counts_in_transaction(conn: Any) -> dict[tuple[str, str], int]:
    """Distinct canonical qualified dossiers per cell (quota authority).

    Counts distinct canonical dossier identities from persisted
    ``qualified`` events; run-scoped legacy rows never count. A total
    of 144 concentrated in too few cells does not satisfy the pilot.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT county, category, COUNT(DISTINCT dossier_id) AS total "
                "FROM pilot_events WHERE transition = 'qualified' "
                "AND identity_kind = 'canonical' "
                "GROUP BY county, category"
            )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    counts: dict[tuple[str, str], int] = {}
    for row in rows:
        data = dict(row)
        counts[(str(data["county"]), str(data["category"]))] = int(data["total"])
    return counts


def canonical_qualified_counts(
    config: DatabaseConfig | None = None,
) -> dict[tuple[str, str], int]:
    return _owned(canonical_qualified_counts_in_transaction, config=config)


def pilot_success_evidenced(config: DatabaseConfig | None = None) -> bool:
    """True only when every cell holds four distinct canonical dossiers.

    Success means all 36 county/category cells each carry at least
    four distinct canonical qualified operational dossiers. Any
    database/config failure returns False: when success cannot be
    evidenced, the synthetic diagnostic fallback stays available
    rather than locking out the demo.
    """
    try:
        counts = _owned(canonical_qualified_counts_in_transaction, config=config)
    except (PilotError, BudgetConfigError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        return False
    for county in COUNTY_PRIORITY_ORDER:
        for category in PILOT_CATEGORIES:
            if counts.get((county, category), 0) < TARGET_QUALIFIED_PER_CELL:
                return False
    return True


def ensure_ledger_in_transaction(conn: Any) -> None:
    """Create the pilot state row and all 36 cells (idempotent, no commit)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pilot_state (state_id, status, weights, limits) "
                "VALUES (1, 'stopped', %(weights)s, %(limits)s) "
                "ON CONFLICT (state_id) DO NOTHING",
                {"weights": json.dumps(default_weights()), "limits": json.dumps({})},
            )
            for county in COUNTY_PRIORITY_ORDER:
                for category in PILOT_CATEGORIES:
                    cur.execute(
                        "INSERT INTO pilot_cells (county, category, target_qualified) "
                        "VALUES (%(county)s, %(category)s, %(target)s) "
                        "ON CONFLICT (county, category) DO NOTHING",
                        {
                            "county": county,
                            "category": category,
                            "target": TARGET_QUALIFIED_PER_CELL,
                        },
                    )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc


def get_state_in_transaction(conn: Any) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, weights, limits FROM pilot_state WHERE state_id = 1")
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    if row is None:
        raise PilotError("pilot ledger not initialized (run ensure_ledger first)")
    data = dict(row)
    for key in ("weights", "limits"):
        if isinstance(data.get(key), str):
            data[key] = json.loads(data[key])
    return data


def set_weights_in_transaction(conn: Any, weights: dict[str, Any]) -> dict[str, float]:
    cleaned = check_weights(weights)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pilot_state SET weights = %(weights)s, updated_at = now() "
                "WHERE state_id = 1",
                {"weights": json.dumps(cleaned)},
            )
            if cur.rowcount == 0:
                raise PilotError("pilot ledger not initialized (run ensure_ledger first)")
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return cleaned


def _set_status_in_transaction(conn: Any, status: str) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pilot_state SET status = %(status)s, updated_at = now() "
                "WHERE state_id = 1",
                {"status": status},
            )
            if cur.rowcount == 0:
                raise PilotError("pilot ledger not initialized (run ensure_ledger first)")
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return status


def stop_pilot_in_transaction(conn: Any) -> str:
    """Halt planning; recorded history is preserved for resume."""
    return _set_status_in_transaction(conn, "stopped")


def resume_pilot_in_transaction(conn: Any) -> str:
    """Reopen planning; the ledger continues exactly where it stopped."""
    return _set_status_in_transaction(conn, "running")


def record_transition_in_transaction(
    conn: Any,
    *,
    county: str,
    category: str,
    dossier_id: UUID | str,
    transition: str,
    run_id: UUID | str | None = None,
    record_key: str | None = None,
) -> bool:
    """Record one stage outcome; True when counted, False on replay.

    ``dossier_id`` must be the canonical operational dossier identity
    (``canonical_quota_id``); cross-run rediscovery of the same
    property replays without a second credit. ``run_id``/``record_key``
    are provenance only and never quota identity. Each
    (dossier, transition) counts at most once per pilot, so retries
    and resumed runs cannot inflate counters. Recording history is
    allowed while stopped; only planning is gated.
    """
    cell_county = normalize_county(county)
    cell_category = normalize_category(category)
    if transition not in TRANSITIONS:
        raise PilotError(f"unknown transition: {transition!r}")
    if is_demo_id(dossier_id):
        raise PilotError("demo identifiers are never pilot-counted")
    try:
        dossier = dossier_id if isinstance(dossier_id, UUID) else UUID(str(dossier_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PilotError("dossier_id must be an operational UUID") from exc
    run_uuid: UUID | None = None
    if run_id is not None:
        try:
            run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise PilotError("run_id must be a UUID when supplied") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT county FROM pilot_cells "
                "WHERE county = %(county)s AND category = %(category)s",
                {"county": cell_county, "category": cell_category},
            )
            if cur.fetchone() is None:
                raise PilotError("unknown pilot cell (run ensure_ledger first)")
            cur.execute(
                "INSERT INTO pilot_events (event_id, county, category, dossier_id, "
                "transition, run_id, record_key, identity_kind) "
                "VALUES (%(eid)s, %(county)s, %(category)s, %(dossier)s, "
                "%(transition)s, %(run)s, %(record_key)s, 'canonical') "
                "ON CONFLICT (dossier_id, transition) DO NOTHING RETURNING event_id",
                {
                    "eid": uuid4(),
                    "county": cell_county,
                    "category": cell_category,
                    "dossier": dossier,
                    "transition": transition,
                    "run": run_uuid,
                    "record_key": record_key,
                },
            )
            if cur.fetchone() is None:
                return False
            cur.execute(
                f"UPDATE pilot_cells SET {transition} = {transition} + 1, "
                "updated_at = now() WHERE county = %(county)s "
                "AND category = %(category)s",
                {"county": cell_county, "category": cell_category},
            )
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    return True


def all_cells_in_transaction(conn: Any) -> list[PilotCell]:
    """All ledger cells in deterministic priority order."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT county, category, target_qualified, discovered, qualified, "
                "assessed, authorized, incomplete, rejected, michael_ready, "
                "engineering_assessed, financially_assessed, package_prepared, "
                "tracked_actual, tracked_simulated "
                "FROM pilot_cells"
            )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    cells = [PilotCell.model_validate(dict(row)) for row in rows]
    cells.sort(key=lambda c: (_county_index(c.county), PILOT_CATEGORIES.index(c.category)))
    return cells


def _shortfall(cell: PilotCell) -> int:
    return max(0, cell.target_qualified - cell.qualified)


def plan_next_in_transaction(
    conn: Any, *, max_actions: int = 12, budget: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Order open cells by truthful shortfall; spend nothing, plan only.

    Raises PilotError while stopped. Each candidate action carries its
    estimated Brave cost and whether the monthly budget covers it.
    """
    if max_actions < 1:
        raise PilotError("max_actions must be positive")
    state = get_state_in_transaction(conn)
    if state["status"] != "running":
        raise PilotError("pilot is stopped; resume before planning")
    weights = check_weights(state["weights"])
    cells = all_cells_in_transaction(conn)
    ranked = sorted(
        (cell for cell in cells if _shortfall(cell) > 0),
        key=lambda c: (
            -_shortfall(c),
            -weights[c.category],
            _county_index(c.county),
            PILOT_CATEGORIES.index(c.category),
        ),
    )
    observed = dict(budget) if budget is not None else read_budget()
    remaining = int(observed.get("remaining", 0) or 0)
    actions: list[dict[str, Any]] = []
    cumulative = 0
    for cell in ranked[:max_actions]:
        cumulative += EST_BRAVE_REQUESTS_PER_ACTION
        actions.append(
            {
                "county": cell.county,
                "county_display": display_name(cell.county),
                "category": cell.category,
                "shortfall": _shortfall(cell),
                "est_brave_requests": EST_BRAVE_REQUESTS_PER_ACTION,
                "budget_ok": cumulative <= remaining,
            }
        )
    return {
        "status": state["status"],
        "weights": weights,
        "open_cells": len(ranked),
        "budget": {
            "remaining": remaining,
            "ceiling": observed.get("ceiling"),
            "month": observed.get("month"),
        },
        "actions": actions,
    }


def pilot_report_in_transaction(
    conn: Any, *, budget: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Totals plus per-cell counters and shortfalls (read-only)."""
    state = get_state_in_transaction(conn)
    cells = all_cells_in_transaction(conn)
    totals = {name: 0 for name in TRANSITIONS}
    target_total = 0
    rows: list[dict[str, Any]] = []
    for cell in cells:
        target_total += cell.target_qualified
        for name in TRANSITIONS:
            totals[name] += getattr(cell, name)
        rows.append(
            {
                "county": cell.county,
                "county_display": display_name(cell.county),
                "category": cell.category,
                "target_qualified": cell.target_qualified,
                "shortfall": _shortfall(cell),
                **{name: getattr(cell, name) for name in TRANSITIONS},
            }
        )
    observed = dict(budget) if budget is not None else read_budget()
    return {
        "status": state["status"],
        "weights": state["weights"],
        "cells": len(cells),
        "target_qualified": target_total,
        "qualified": totals["qualified"],
        "shortfall": max(0, target_total - totals["qualified"]),
        "totals": totals,
        "budget": {
            "remaining": observed.get("remaining"),
            "ceiling": observed.get("ceiling"),
            "month": observed.get("month"),
        },
        "rows": rows,
    }


def _owned(conn_fn: Any, *args: Any, config: DatabaseConfig | None = None, **kwargs: Any) -> Any:
    """Run one cursor-level helper on a short owned connection (commit once)."""
    conn = get_connection(config)
    try:
        result = conn_fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except (PilotError, BudgetConfigError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        raise _wrap_driver(exc) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ensure_ledger(config: DatabaseConfig | None = None) -> None:
    return _owned(ensure_ledger_in_transaction, config=config)


def set_weights(
    weights: dict[str, Any], config: DatabaseConfig | None = None
) -> dict[str, float]:
    return _owned(set_weights_in_transaction, weights, config=config)


def stop_pilot(config: DatabaseConfig | None = None) -> str:
    return _owned(stop_pilot_in_transaction, config=config)


def resume_pilot(config: DatabaseConfig | None = None) -> str:
    return _owned(resume_pilot_in_transaction, config=config)


def record_transition(
    *,
    county: str,
    category: str,
    dossier_id: UUID | str,
    transition: str,
    run_id: UUID | str | None = None,
    record_key: str | None = None,
    config: DatabaseConfig | None = None,
) -> bool:
    return _owned(
        record_transition_in_transaction,
        county=county,
        category=category,
        dossier_id=dossier_id,
        transition=transition,
        run_id=run_id,
        record_key=record_key,
        config=config,
    )


def plan_next(
    *, max_actions: int = 12, config: DatabaseConfig | None = None
) -> dict[str, Any]:
    return _owned(plan_next_in_transaction, max_actions=max_actions, config=config)


def pilot_report(config: DatabaseConfig | None = None) -> dict[str, Any]:
    return _owned(pilot_report_in_transaction, config=config)


def _print_status(report: dict[str, Any]) -> None:
    totals = report["totals"]
    print(f"pilot status: {report['status']}")
    print(f"qualified: {report['qualified']}/{report['target_qualified']} "
          f"(shortfall {report['shortfall']}) across {report['cells']} cells")
    print("stages: " + ", ".join(f"{name}={totals[name]}" for name in TRANSITIONS))
    budget = report["budget"]
    print(f"brave budget: remaining={budget['remaining']} "
          f"ceiling={budget['ceiling']} month={budget['month']}")
    open_rows = [row for row in report["rows"] if row["shortfall"] > 0]
    print(f"open cells: {len(open_rows)}")
    for row in open_rows[:12]:
        print(f"  {row['county_display']} / {row['category']}: "
              f"qualified={row['qualified']} shortfall={row['shortfall']}")
    if len(open_rows) > 12:
        print(f"  ... and {len(open_rows) - 12} more")


def _print_plan(plan: dict[str, Any]) -> None:
    print(f"pilot status: {plan['status']} (planning only; nothing executed)")
    print(f"open cells: {plan['open_cells']}")
    for action in plan["actions"]:
        verdict = "budget-ok" if action["budget_ok"] else "OVER BUDGET"
        print(f"  {action['county_display']} / {action['category']}: "
              f"shortfall={action['shortfall']} "
              f"est_brave={action['est_brave_requests']} {verdict}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: status | plan | stop | resume. Never spends."""
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [item for item in args if item != "--json"]
    max_actions = 12
    if "--max" in args:
        try:
            pos = args.index("--max")
            max_actions = int(args[pos + 1])
            del args[pos : pos + 2]
        except (ValueError, IndexError):
            print("error: --max needs a positive integer", file=sys.stderr)
            return 1
    if len(args) != 1 or args[0] not in ("status", "plan", "stop", "resume"):
        print("usage: python -m app.pilot status|plan|stop|resume [--max N] [--json]",
              file=sys.stderr)
        return 1
    command = args[0]
    try:
        if command == "status":
            report = pilot_report()
            if as_json:
                print(json.dumps(report, default=str))
            else:
                _print_status(report)
        elif command == "plan":
            plan = plan_next(max_actions=max_actions)
            if as_json:
                print(json.dumps(plan, default=str))
            else:
                _print_plan(plan)
        elif command == "stop":
            print(f"pilot status: {stop_pilot()} (history preserved)")
        else:
            print(f"pilot status: {resume_pilot()} (ledger resumed)")
    except DatabaseNotConfiguredError:
        print("error: database_not_configured (set DATABASE_* env vars first)",
              file=sys.stderr)
        return 2
    except DatabaseUnavailableError as exc:
        print(f"error: database_unavailable ({exc})", file=sys.stderr)
        return 3
    except (PilotError, BudgetConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
