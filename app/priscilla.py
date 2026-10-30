"""Priscilla: delivery/engagement/response/conversion tracking (canonical role).

Priscilla records conversion events against Uriel packages and reports
metrics with simulated and actual evidence strictly separated: simulated
events never enter actual totals, and no combined total is offered.
Simulated demonstration events are permitted only with DEMO_MODE=yes and
must carry an unmistakable simulated label; they may reference real
package IDs without implying real recipients responded. Actual events
require explicit evidence references. Switching DEMO_MODE never replays,
converts, or sends anything: rows are append-only and immutable.

No real outreach sender exists in this assignment, so actual delivery
events can only arrive with externally supplied evidence.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any
from uuid import UUID, uuid4

from app.db import DatabaseUnavailableError
from app.modes import demo_mode_enabled
from app.uriel import UrielError, get_package_in_transaction
from app.workflow import is_demo_id

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

PRISCILLA_VERSION = "priscilla-1"
KINDS = ("delivery", "engagement", "response", "conversion")

SIM_CONFIG_VERSION = "priscilla-sim-1"
SIM_RESPONSE_RATE = Decimal("0.10")
SIM_CUSTOMER_RATE = Decimal("0.36")
SIM_ROUNDING = "floor-responses;half-up-customers;capped"
SIM_SELECTION_METHOD = "seeded-sha256-prefix"


class PriscillaError(ValueError):
    """Invalid tracking request (bad identity, kind, label, or evidence)."""


def _uuid(value: Any, name: str) -> UUID:
    if is_demo_id(value):
        raise PriscillaError(f"demo identifiers are rejected by Priscilla: {name}")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PriscillaError(f"{name} must be an operational UUID") from exc


def record_event_in_transaction(
    conn: Any,
    *,
    package_entry: UUID | str,
    kind: str,
    label: str,
    simulated: bool,
    evidence_refs: list[str] | None = None,
    detail: dict[str, Any] | None = None,
    simulation_id: UUID | str | None = None,
) -> dict[str, Any]:
    """Append one conversion event; simulated and actual never mix.

    Simulated events require DEMO_MODE=yes and a label naming them
    simulated. Actual events require at least one evidence reference
    and a label that does not claim simulation. Unknown cells record
    no pilot transition. Simulation-linked events carry the run ID in
    both a dedicated column and the payload, and their insert is
    idempotent per (simulation, package, kind).
    """
    entry_uuid = _uuid(package_entry, "package_entry")
    sim_uuid = _uuid(simulation_id, "simulation_id") if simulation_id is not None else None
    if sim_uuid is not None and not simulated:
        raise PriscillaError("simulation-linked events must be simulated")
    if kind not in KINDS:
        raise PriscillaError(f"unknown conversion kind: {kind!r}")
    text = str(label or "")
    if not text.strip():
        raise PriscillaError("event label is required")
    refs = [str(ref) for ref in (evidence_refs or []) if str(ref).strip()]
    if simulated:
        if not demo_mode_enabled():
            raise PriscillaError("simulated events require DEMO_MODE=yes")
        if "simulated" not in text.lower():
            raise PriscillaError("simulated events must be labeled simulated")
    else:
        if not refs:
            raise PriscillaError("actual events require evidence references")
        if "simulated" in text.lower():
            raise PriscillaError("actual events must not be labeled simulated")
    try:
        package = get_package_in_transaction(conn, entry_uuid)
    except UrielError as exc:
        raise PriscillaError(str(exc)) from exc
    if package is None:
        raise PriscillaError("unknown Uriel package for conversion event")
    merged_detail = dict(detail or {})
    if sim_uuid is not None:
        merged_detail["simulation_id"] = str(sim_uuid)
    payload = {
        "priscilla_version": PRISCILLA_VERSION,
        "evidence_refs": refs,
        "detail": merged_detail,
    }
    replayed = False
    try:
        with conn.cursor() as cur:
            event_id = uuid4()
            if sim_uuid is None:
                cur.execute(
                    "INSERT INTO conversion_events (event_id, package_entry, kind, "
                    "simulated, label, payload) VALUES (%(eid)s, %(entry)s, "
                    "%(kind)s, %(simulated)s, %(label)s, %(payload)s) "
                    "RETURNING event_id, created_at",
                    {
                        "eid": event_id,
                        "entry": entry_uuid,
                        "kind": kind,
                        "simulated": simulated,
                        "label": text,
                        "payload": json.dumps(payload),
                    },
                )
                inserted = cur.fetchone()
            else:
                cur.execute(
                    "INSERT INTO conversion_events (event_id, package_entry, kind, "
                    "simulated, label, payload, simulation_id) "
                    "VALUES (%(eid)s, %(entry)s, %(kind)s, %(simulated)s, "
                    "%(label)s, %(payload)s, %(simulation)s) "
                    "ON CONFLICT (simulation_id, package_entry, kind) "
                    "WHERE simulation_id IS NOT NULL DO NOTHING "
                    "RETURNING event_id, created_at",
                    {
                        "eid": event_id,
                        "entry": entry_uuid,
                        "kind": kind,
                        "simulated": simulated,
                        "label": text,
                        "payload": json.dumps(payload),
                        "simulation": sim_uuid,
                    },
                )
                inserted = cur.fetchone()
                if inserted is None:
                    cur.execute(
                        "SELECT event_id, created_at FROM conversion_events "
                        "WHERE simulation_id = %(simulation)s "
                        "AND package_entry = %(entry)s AND kind = %(kind)s",
                        {"simulation": sim_uuid, "entry": entry_uuid, "kind": kind},
                    )
                    inserted = cur.fetchone()
                    replayed = True
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    from app.pilot import record_transition_in_transaction

    cell = (package.get("payload") or {}).get("cell") or {}
    if cell.get("county") and cell.get("category"):
        try:
            record_transition_in_transaction(
                conn, county=str(cell["county"]), category=str(cell["category"]),
                dossier_id=_uuid(package.get("dossier_id"), "package dossier_id"),
                transition="tracked_simulated" if simulated else "tracked_actual",
                record_key=f"conversion-{kind}",
            )
        except (PriscillaError, ValueError):
            # A malformed stored cell must not fail event recording; the
            # event is durable and the ledger simply gains no count.
            pass
    created = dict(inserted) if inserted is not None else {}
    if replayed and created.get("event_id"):
        event_id = created["event_id"] if isinstance(created["event_id"], UUID) else UUID(str(created["event_id"]))
    return {
        "event_id": str(event_id),
        "package_entry": str(entry_uuid),
        "kind": kind,
        "simulated": simulated,
        "label": text,
        "payload": payload,
        "created_at": str(created.get("created_at", "")),
        "simulation_id": str(sim_uuid) if sim_uuid is not None else None,
        "replayed": replayed,
    }


def metrics_in_transaction(
    conn: Any, *, package_entry: UUID | str | None = None
) -> dict[str, Any]:
    """Conversion metrics with simulated and actual strictly separated.

    No combined total is reported: callers that need both must add them
    explicitly and label the mixture themselves.
    """
    entry_uuid = _uuid(package_entry, "package_entry") if package_entry is not None else None
    try:
        with conn.cursor() as cur:
            if entry_uuid is None:
                cur.execute(
                    "SELECT simulated, kind, COUNT(*) AS total "
                    "FROM conversion_events GROUP BY simulated, kind"
                )
            else:
                cur.execute(
                    "SELECT simulated, kind, COUNT(*) AS total "
                    "FROM conversion_events WHERE package_entry = %(entry)s "
                    "GROUP BY simulated, kind",
                    {"entry": entry_uuid},
                )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    simulated = {kind: 0 for kind in KINDS}
    actual = {kind: 0 for kind in KINDS}
    for row in rows:
        data = dict(row)
        bucket = simulated if data.get("simulated") else actual
        if data.get("kind") in bucket:
            bucket[str(data["kind"])] = int(data.get("total", 0) or 0)
    return {
        "priscilla_version": PRISCILLA_VERSION,
        "package_entry": str(entry_uuid) if entry_uuid else None,
        "simulated": {**simulated, "total": sum(simulated.values())},
        "actual": {**actual, "total": sum(actual.values())},
    }


def sim_expected_counts(cohort_size: int) -> dict[str, Any]:
    """Deterministic funnel counts for a cohort of size N (pure).

    responses = floor(N * 0.10); customers = round_half_up(responses *
    0.36), capped at responses. N=144 yields 14 responses and 5
    customers. Percentages are None when their denominator is zero.
    """
    if not isinstance(cohort_size, int) or cohort_size < 0:
        raise PriscillaError("cohort_size must be a non-negative integer")
    responses = int(
        (Decimal(cohort_size) * SIM_RESPONSE_RATE).to_integral_value(rounding=ROUND_FLOOR)
    )
    customers = int(
        (Decimal(responses) * SIM_CUSTOMER_RATE).to_integral_value(rounding=ROUND_HALF_UP)
    )
    customers = min(customers, responses)
    return {
        "cohort": cohort_size,
        "deliveries": cohort_size,
        "responses": responses,
        "customers": customers,
        "response_pct": round(responses / cohort_size * 100, 2) if cohort_size else None,
        "customer_pct": round(customers / responses * 100, 2) if responses else None,
    }


def rank_entries(entry_ids: list[UUID | str], seed: str) -> list[str]:
    """Deterministic seeded ranking; input order never matters."""
    return sorted(
        (str(entry) for entry in entry_ids),
        key=lambda entry: hashlib.sha256(f"{seed}:{entry}".encode("utf-8")).hexdigest(),
    )


def _decode_json(value: Any, name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise PriscillaError(f"stored {name} is not valid JSON") from exc
    return value if value is not None else ([] if name != "counts" else {})


def _cohort_rows_in_transaction(
    conn: Any, package_entries: list[UUID] | None
) -> list[dict[str, Any]]:
    try:
        with conn.cursor() as cur:
            if package_entries is None:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "simulated, execution_state, payload, created_at "
                    "FROM uriel_packages ORDER BY entry_id"
                )
            else:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "simulated, execution_state, payload, created_at "
                    "FROM uriel_packages WHERE entry_id = ANY(%(ids)s) "
                    "ORDER BY entry_id",
                    {"ids": package_entries},
                )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    records = []
    for row in rows:
        record = dict(row)
        record["payload"] = _decode_json(record.get("payload"), "package payload")
        records.append(record)
    return records


def _freeze_cohort(
    conn: Any, *, cohort: str, package_entries: list[UUID | str] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split candidate packages into eligible rows and exclusions.

    Eligible means a genuine reviewed package with no blockers: state
    'prepared', not synthetic, execution never attempted. Each dossier
    contributes its latest version only. Explicitly requested entries
    fail closed when missing or ineligible; cohort-wide scans record
    exclusions with reasons instead.
    """
    explicit = package_entries is not None
    wanted: list[UUID] | None = None
    if explicit:
        seen: set[str] = set()
        wanted = []
        for raw in package_entries or []:
            entry = _uuid(raw, "package_entry")
            if str(entry) not in seen:
                seen.add(str(entry))
                wanted.append(entry)
    rows = _cohort_rows_in_transaction(conn, wanted)
    if explicit:
        found = {str(row["entry_id"]) for row in rows}
        missing = [str(entry) for entry in (wanted or []) if str(entry) not in found]
        if missing:
            raise PriscillaError(f"unknown Uriel package(s): {sorted(missing)}")
    eligible: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []

    def _exclude(row: dict[str, Any], reason: str) -> None:
        exclusion = {
            "entry_id": str(row["entry_id"]),
            "dossier_id": str(row.get("dossier_id")),
            "version": row.get("version"),
            "reason": reason,
        }
        if explicit:
            raise PriscillaError(
                f"package {exclusion['entry_id']} is ineligible for simulation: {reason}"
            )
        exclusions.append(exclusion)

    for row in rows:
        state = str(row.get("package_state", ""))
        if row.get("simulated"):
            _exclude(row, "synthetic_package")
        elif state == "draft":
            _exclude(row, "draft_package")
        elif state != "prepared":
            _exclude(row, "ineligible_state")
        elif list((row.get("payload") or {}).get("blockers") or []):
            _exclude(row, "blocked_package")
        elif str(row.get("execution_state", "")) != "not_attempted":
            _exclude(row, "already_executed")
        else:
            eligible.append(row)
    by_dossier: dict[str, list[dict[str, Any]]] = {}
    for row in eligible:
        by_dossier.setdefault(str(row["dossier_id"]), []).append(row)
    frozen: list[dict[str, Any]] = []
    for dossier_id in sorted(by_dossier):
        versions = sorted(by_dossier[dossier_id], key=lambda row: int(row.get("version", 0)))
        for older in versions[:-1]:
            _exclude(older, "superseded_version")
        frozen.append(versions[-1])
    frozen.sort(key=lambda row: str(row["entry_id"]))
    exclusions.sort(key=lambda item: (str(item["entry_id"]), str(item["reason"])))
    return frozen, exclusions


def preview_simulation_in_transaction(
    conn: Any,
    *,
    cohort: str = "",
    package_entries: list[UUID | str] | None = None,
    seed: str = "",
) -> dict[str, Any]:
    """Dry-run a cohort simulation: eligibility, assumptions, counts.

    Read-only: no simulation row and no events are written.
    """
    frozen, exclusions = _freeze_cohort(
        conn, cohort=str(cohort or ""), package_entries=package_entries
    )
    ranked = rank_entries([row["entry_id"] for row in frozen], str(seed or ""))
    counts = sim_expected_counts(len(frozen))
    responders = ranked[: counts["responses"]]
    customers = responders[: counts["customers"]]
    return {
        "cohort": str(cohort or ""),
        "assumptions": {
            "config_version": SIM_CONFIG_VERSION,
            "response_rate": str(SIM_RESPONSE_RATE),
            "customer_rate": str(SIM_CUSTOMER_RATE),
            "rounding": SIM_ROUNDING,
            "selection_method": SIM_SELECTION_METHOD,
            "seed": str(seed or ""),
        },
        "eligible": [
            {
                "package_entry": str(row["entry_id"]),
                "dossier_id": str(row["dossier_id"]),
                "version": row.get("version"),
            }
            for row in frozen
        ],
        "responders": responders,
        "customers": customers,
        "exclusions": exclusions,
        "expected": counts,
    }


_SIM_COLUMNS = (
    "sim_id, idempotency_key, cohort, package_ids, dossier_refs, "
    "config_version, response_rate, customer_rate, rounding, "
    "selection_method, seed, actor, status, counts, exclusions, "
    "created_at, completed_at"
)


def _sim_from_row(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["sim_id"] = str(record.get("sim_id"))
    record["package_ids"] = [str(item) for item in (record.get("package_ids") or [])]
    record["dossier_refs"] = _decode_json(record.get("dossier_refs"), "dossier_refs")
    record["counts"] = _decode_json(record.get("counts"), "counts")
    record["exclusions"] = _decode_json(record.get("exclusions"), "exclusions")
    record["created_at"] = str(record.get("created_at", ""))
    record["completed_at"] = str(record.get("completed_at", ""))
    return record


def get_simulation_in_transaction(
    conn: Any, sim_id: UUID | str
) -> dict[str, Any] | None:
    """Load one persisted simulation run by ID."""
    sim_uuid = _uuid(sim_id, "sim_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SIM_COLUMNS} FROM priscilla_simulations "
                "WHERE sim_id = %(sim)s",
                {"sim": sim_uuid},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    return _sim_from_row(row) if row is not None else None


def _sim_by_key_in_transaction(conn: Any, key: str) -> dict[str, Any] | None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SIM_COLUMNS} FROM priscilla_simulations "
                "WHERE idempotency_key = %(key)s",
                {"key": key},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    return _sim_from_row(row) if row is not None else None


def _replay_material(sim: dict[str, Any]) -> dict[str, Any]:
    """Outcome-determining inputs of a stored simulation run."""
    return {
        "cohort": str(sim.get("cohort", "")),
        "package_ids": sorted(str(i) for i in sim.get("package_ids", [])),
        "seed": str(sim.get("seed", "")),
        "config_version": str(sim.get("config_version", "")),
        "response_rate": str(sim.get("response_rate", "")),
        "customer_rate": str(sim.get("customer_rate", "")),
        "rounding": str(sim.get("rounding", "")),
        "selection_method": str(sim.get("selection_method", "")),
    }


def _validate_replay(existing: dict[str, Any], material: dict[str, Any]) -> None:
    """Reject an idempotency key reused with different material inputs."""
    stored = _replay_material(existing)
    if stored["cohort"] != material["cohort"]:
        raise PriscillaError("idempotency key already used for a different cohort")
    if stored["package_ids"] != material["package_ids"]:
        raise PriscillaError(
            "idempotency key already used for a different package selection"
        )
    if stored["seed"] != material["seed"]:
        raise PriscillaError("idempotency key already used with a different seed")
    for field in (
        "config_version", "response_rate", "customer_rate",
        "rounding", "selection_method",
    ):
        if stored[field] != material[field]:
            raise PriscillaError(
                "idempotency key already used with a different simulation "
                "configuration"
            )


def run_simulation_in_transaction(
    conn: Any,
    *,
    idempotency_key: str,
    cohort: str = "",
    package_entries: list[UUID | str] | None = None,
    actor: str = "",
    seed: str = "",
) -> dict[str, Any]:
    """Execute one deterministic cohort simulation (DEMO_MODE=yes only).

    Freezes the eligible cohort, persists the run, and writes
    simulated delivery/response/conversion events. Idempotency binds
    to the cohort label, frozen package selection, seed, and
    configuration: retries with identical inputs replay the stored
    run without duplicating events, while conflicting reuse is
    rejected so a changed cohort or config needs a distinct run.
    Simulated rows never touch dossier facts, financial results,
    actual metrics, or package execution state.

    Concurrency is PostgreSQL-safe: ``ON CONFLICT DO NOTHING``
    serializes rival inserts on the unique index without aborting
    the loser's transaction, and the loser re-reads the winner's
    committed row (or wins outright if the winner aborts) before
    the same material-input validation runs.
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise PriscillaError("idempotency_key is required")
    if not demo_mode_enabled():
        raise PriscillaError("simulated events require DEMO_MODE=yes")
    cohort_label = str(cohort or "")
    frozen, exclusions = _freeze_cohort(
        conn, cohort=cohort_label, package_entries=package_entries
    )
    material = {
        "cohort": cohort_label,
        "package_ids": sorted(str(row["entry_id"]) for row in frozen),
        "seed": str(seed or ""),
        "config_version": SIM_CONFIG_VERSION,
        "response_rate": str(SIM_RESPONSE_RATE),
        "customer_rate": str(SIM_CUSTOMER_RATE),
        "rounding": SIM_ROUNDING,
        "selection_method": SIM_SELECTION_METHOD,
    }
    seed_text = str(seed or "")
    ranked = rank_entries([row["entry_id"] for row in frozen], seed_text)
    counts = sim_expected_counts(len(frozen))
    responders = set(ranked[: counts["responses"]])
    customers = set(ranked[: counts["customers"]])
    sim_id = uuid4()
    dossier_refs = [
        {
            "package_entry": str(row["entry_id"]),
            "dossier_id": str(row["dossier_id"]),
            "version": row.get("version"),
        }
        for row in frozen
    ]
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO priscilla_simulations (sim_id, idempotency_key, "
                "cohort, package_ids, dossier_refs, config_version, "
                "response_rate, customer_rate, rounding, selection_method, "
                "seed, actor, status, counts, exclusions) "
                "VALUES (%(sim)s, %(key)s, %(cohort)s, %(packages)s, "
                "%(refs)s, %(config)s, %(response_rate)s, %(customer_rate)s, "
                "%(rounding)s, %(method)s, %(seed)s, %(actor)s, 'completed', "
                "%(counts)s, %(exclusions)s) "
                "ON CONFLICT (idempotency_key) DO NOTHING "
                "RETURNING sim_id, created_at, completed_at",
                {
                    "sim": sim_id,
                    "key": key,
                    "cohort": str(cohort or ""),
                    "packages": [UUID(str(row["entry_id"])) for row in frozen],
                    "refs": json.dumps(dossier_refs),
                    "config": SIM_CONFIG_VERSION,
                    "response_rate": str(SIM_RESPONSE_RATE),
                    "customer_rate": str(SIM_CUSTOMER_RATE),
                    "rounding": SIM_ROUNDING,
                    "method": SIM_SELECTION_METHOD,
                    "seed": seed_text,
                    "actor": str(actor or ""),
                    "counts": json.dumps(counts),
                    "exclusions": json.dumps(exclusions),
                },
            )
            inserted = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    if inserted is None:
        existing = _sim_by_key_in_transaction(conn, key)
        if existing is None:
            raise PriscillaError("simulation conflict could not be resolved")
        _validate_replay(existing, material)
        return {**existing, "replayed": True}
    tag = cohort_label or str(sim_id)[:8]
    for entry in ranked:
        record_event_in_transaction(
            conn, package_entry=entry, kind="delivery",
            label=f"simulated outreach delivery [{tag}]", simulated=True,
            detail={"cohort": str(cohort or "")}, simulation_id=sim_id,
        )
        if entry in responders:
            record_event_in_transaction(
                conn, package_entry=entry, kind="response",
                label=f"simulated interested response [{tag}]", simulated=True,
                detail={"cohort": str(cohort or "")}, simulation_id=sim_id,
            )
        if entry in customers:
            record_event_in_transaction(
                conn, package_entry=entry, kind="conversion",
                label=f"simulated customer conversion [{tag}]", simulated=True,
                detail={"cohort": str(cohort or "")}, simulation_id=sim_id,
            )
    created = dict(inserted) if inserted is not None else {}
    stored = _sim_by_key_in_transaction(conn, key)
    if stored is None:  # pragma: no cover - insert just succeeded
        raise DatabaseUnavailableError("priscilla_unavailable")
    stored["created_at"] = str(created.get("created_at", stored.get("created_at", "")))
    stored["completed_at"] = str(created.get("completed_at", stored.get("completed_at", "")))
    return {**stored, "replayed": False}


def funnel_in_transaction(
    conn: Any, *, simulation_id: UUID | str | None = None
) -> dict[str, Any]:
    """Unique-lead funnel with simulated and actual strictly separated.

    A lead counts once per stage no matter how many events it has.
    Scoped to one simulation run when given (cohort N, realized
    simulated leads, cohort-scoped actual leads, exclusions);
    otherwise global across all tracked events. No combined total.
    """
    sim_uuid = _uuid(simulation_id, "simulation_id") if simulation_id is not None else None
    run: dict[str, Any] | None = None
    cohort_ids: list[str] = []
    if sim_uuid is not None:
        run = get_simulation_in_transaction(conn, sim_uuid)
        if run is None:
            raise PriscillaError("unknown simulation run")
        cohort_ids = [str(item) for item in (run.get("package_ids") or [])]
    try:
        with conn.cursor() as cur:
            if sim_uuid is not None:
                cur.execute(
                    "SELECT package_entry, kind, simulated FROM conversion_events "
                    "WHERE simulation_id = %(sim)s",
                    {"sim": sim_uuid},
                )
                sim_rows = [dict(row) for row in (cur.fetchall() or [])]
                if cohort_ids:
                    cur.execute(
                        "SELECT package_entry, kind FROM conversion_events "
                        "WHERE package_entry = ANY(%(ids)s) AND NOT simulated "
                        "AND simulation_id IS NULL",
                        {"ids": cohort_ids},
                    )
                    actual_rows = [dict(row) for row in (cur.fetchall() or [])]
                else:
                    actual_rows = []
            else:
                cur.execute("SELECT package_entry, kind, simulated FROM conversion_events")
                sim_rows = []
                actual_rows = []
                for row in (cur.fetchall() or []):
                    data = dict(row)
                    (sim_rows if data.get("simulated") else actual_rows).append(data)
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("priscilla_unavailable") from exc
    simulated_leads: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for row in sim_rows:
        if row.get("kind") in simulated_leads:
            simulated_leads[str(row["kind"])].add(str(row["package_entry"]))
    actual_leads: dict[str, set[str]] = {kind: set() for kind in KINDS}
    for row in actual_rows:
        if row.get("kind") in actual_leads:
            actual_leads[str(row["kind"])].add(str(row["package_entry"]))
    outreach = simulated_leads["delivery"]
    responses = simulated_leads["response"]
    conversions = simulated_leads["conversion"]
    cohort_set = set(cohort_ids) if sim_uuid is not None else None
    consistent = responses <= outreach and conversions <= responses
    if cohort_set is not None:
        consistent = consistent and outreach <= cohort_set
    report: dict[str, Any] = {
        "priscilla_version": PRISCILLA_VERSION,
        "scope": {"simulation_id": str(sim_uuid)} if sim_uuid else {"simulation_id": None},
        "cohort_unique": len(cohort_set) if cohort_set is not None else None,
        "simulated_unique": {
            "outreach": len(outreach),
            "responses": len(responses),
            "customers": len(conversions),
        },
        "actual_unique": {kind: len(actual_leads[kind]) for kind in KINDS},
        "subset_consistent": consistent,
    }
    if run is not None:
        report["scope"]["cohort"] = run.get("cohort", "")
        report["expected"] = run.get("counts", {})
        report["exclusions"] = run.get("exclusions", [])
    return report
