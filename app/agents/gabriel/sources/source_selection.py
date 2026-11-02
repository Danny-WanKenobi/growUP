"""Typed, deterministic source selection for Gabriel.

Selection never grants registry authority. A candidate is executable
only when it qualifies, carries a registered adapter, has clear
licensing and known freshness, and its claimed publisher is
consistent across name/domain/provenance (or registry-enrolled).
Unknown attributes stay unknown and route to ``needs_review``.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY, SourceClass
from app.agents.gabriel.sources.qualification import SourceCandidate, rank_sources

POLICY_VERSION = "gabriel-source-selection-1"
SELECTED = "selected"
NEEDS_REVIEW = "needs_review"
NO_SUPPORTED_ADAPTER = "no_supported_adapter"
REFUSED = "refused"


class SourceSelectionError(ValueError):
    """Selection is missing, unapproved, or does not match collection."""


class UncertainFindingError(SourceSelectionError):
    """A claimed finding attempt may have transmitted; never auto-retry it."""


@dataclass(frozen=True)
class DiscoveryBudget:
    max_requests: int = 0
    max_paid_requests: int = 0
    max_results: int = 20
    timeout_seconds: float = 10.0
    max_response_bytes: int = 262_144
    max_redirects: int = 0

    def validate(self) -> None:
        for name in ("max_requests", "max_paid_requests", "max_results", "max_response_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SourceSelectionError(f"{name} must be an integer count")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise SourceSelectionError("timeout_seconds must be a number")
        if not (0 <= self.max_requests <= 10):
            raise SourceSelectionError("max_requests must be 0..10")
        if not (0 <= self.max_paid_requests <= self.max_requests):
            raise SourceSelectionError("max_paid_requests must be <= max_requests")
        if not (1 <= self.max_results <= 50):
            raise SourceSelectionError("max_results must be 1..50")
        if not (0 < self.timeout_seconds <= 30):
            raise SourceSelectionError("timeout_seconds must be in (0,30]")
        if not (1024 <= self.max_response_bytes <= 1_048_576):
            raise SourceSelectionError("max_response_bytes out of bounds")
        if self.max_redirects != 0:
            raise SourceSelectionError("redirects are not followed")


@dataclass(frozen=True)
class SourceMission:
    jurisdiction: str
    record_types: tuple[str, ...] = ("parcels", "appraisal")
    country: str = "US"
    approved: bool = False
    budget: DiscoveryBudget = field(default_factory=DiscoveryBudget)

    def validate(self) -> None:
        if not str(self.jurisdiction or "").strip():
            raise SourceSelectionError("jurisdiction is required")
        if not self.record_types or len(self.record_types) > 8:
            raise SourceSelectionError("record_types must be 1..8 items")
        self.budget.validate()


@dataclass(frozen=True)
class FoundCandidate:
    candidate: SourceCandidate
    tier: int
    provider: str
    adapter: str | None = None
    evidence_ref: str = ""


def _host(value: str) -> str:
    raw = (value or "").strip().lower()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.hostname or "").rstrip(".")


def _publisher_consistent(c: SourceCandidate) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    domain = _host(c.publisher_domain)
    if not domain:
        reasons.append("publisher domain unknown")
        return False, reasons
    if c.provenance_url and _host(c.provenance_url) != domain:
        reasons.append("provenance host does not match claimed publisher domain (possible spoof)")
        return False, reasons
    entry = PRODUCTION_REGISTRY.lookup(domain)
    if entry is not None and entry.source_class is SourceClass.AUTHORITATIVE:
        if entry.publisher and c.publisher_name and entry.publisher.lower() != c.publisher_name.lower():
            reasons.append("publisher name does not match registry enrollment")
            return False, reasons
        return True, reasons
    # A .gov spelling or self-claim is evidence to review, not proof.
    # Non-registry candidates may still be *selected* for collection
    # only when every other executable gate is met; they gain no
    # authority candidacy beyond what qualification already states.
    return True, reasons


def _registry_enrolled(c: SourceCandidate) -> bool:
    entry = PRODUCTION_REGISTRY.lookup(_host(c.publisher_domain))
    return entry is not None and entry.source_class is SourceClass.AUTHORITATIVE


@dataclass(frozen=True)
class AdapterBinding:
    """Server-owned binding: which publisher/endpoint an adapter serves."""

    publishers: frozenset = frozenset()
    endpoints: frozenset = frozenset()
    record_types: frozenset = frozenset()


ADAPTER_BINDINGS: dict[str, AdapterBinding] = {
    "dcad_bulk": AdapterBinding(
        publishers=frozenset({"www.dallascad.org", "dallascad.org"}),
        endpoints=frozenset({"www.dallascad.org", "dallascad.org"}),
        record_types=frozenset({"parcels", "appraisal"})),
    "overpass": AdapterBinding(
        publishers=frozenset({"www.openstreetmap.org", "openstreetmap.org", "overpass-api.de"}),
        endpoints=frozenset({"overpass-api.de"})),
    "osm_api": AdapterBinding(
        publishers=frozenset({"www.openstreetmap.org", "openstreetmap.org", "api.openstreetmap.org"}),
        endpoints=frozenset({"api.openstreetmap.org"})),
}


def register_adapter_binding(adapter: str, *, publishers=(), endpoints=(), record_types=()) -> None:
    """Register a server-owned adapter binding (real adapters / test fakes)."""
    ADAPTER_BINDINGS[str(adapter)] = AdapterBinding(
        publishers=frozenset(_host(str(p)) for p in publishers if _host(str(p))),
        endpoints=frozenset(_host(str(e)) for e in endpoints if _host(str(e))),
        record_types=frozenset(str(r).lower() for r in record_types))


# Server-retained decisions: enforce_selection only accepts a decision
# this process constructed (select_sources / bootstrap_selection) or
# one verified against the durable workflow store (see below). The
# process dict is only a cache; it is never proof of durability.
_RETAINED: dict[str, dict[str, Any]] = {}

# Explicit server-owned bootstrap descriptors. Bootstrap is not a
# general bypass: only these existing sources, with an explicit
# server-supplied jurisdiction/scope, may use it. ``dcad_bulk`` is
# bound to Dallas County; the OSM sources are supplemental geographic
# adapters usable with any explicit jurisdiction (or the explicit
# supplemental scope constant constructed by server code).
BOOTSTRAP_SUPPLEMENTAL_SCOPE = "supplemental-geographic"
BOOTSTRAP_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "overpass": {"kind": "supplemental_geographic"},
    "osm_api": {"kind": "supplemental_geographic"},
    "dcad_bulk": {"kind": "official_bulk",
                  "jurisdictions": frozenset({"dallas county"})},
}

_DEFAULT_DURABLE_STORE: Any = None


def set_durable_store(store: Any) -> None:
    """Install the process-default durable decision store (or None)."""
    global _DEFAULT_DURABLE_STORE
    _DEFAULT_DURABLE_STORE = store


def get_durable_store() -> Any:
    return _DEFAULT_DURABLE_STORE


def clear_retained() -> None:
    """Drop the process cache only (durable records are unaffected)."""
    _RETAINED.clear()


def mission_payload(mission: SourceMission) -> dict[str, Any]:
    return {"jurisdiction": mission.jurisdiction,
            "record_types": list(mission.record_types),
            "country": mission.country,
            "budget": {"max_requests": mission.budget.max_requests,
                       "max_paid_requests": mission.budget.max_paid_requests,
                       "max_results": mission.budget.max_results,
                       "timeout_seconds": mission.budget.timeout_seconds,
                       "max_response_bytes": mission.budget.max_response_bytes,
                       "max_redirects": mission.budget.max_redirects}}


def mission_fingerprint(mission: SourceMission) -> str:
    """Stable digest of mission + limits; same-key conflicts compare it."""
    body = mission_payload(mission)
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _retain(payload: dict[str, Any]) -> dict[str, Any]:
    payload["digest"] = _digest(payload)
    _RETAINED[payload["digest"]] = copy.deepcopy(payload)
    return payload


def _rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


class WorkflowSelectionStore:
    """Durable finding/decision store on existing workflow tables.

    Uses only existing supported record kinds (``research_artifact``)
    inside a per-assignment workflow run whose idempotency key is
    ``gabriel-source-finding:<assignment_key>``. The exclusive claim
    is committed in its own transaction *before* any provider
    transmission; evidence and the final decision are then persisted
    as versioned records in that same run. Tests inject stores with
    the same three methods (``claim``/``complete``/``load_decision``)
    backed by transactional fakes; no real DB is needed to test the
    orchestration.
    """

    _KIND = "research_artifact"

    def __init__(self, connect: Callable[[], Any] | None = None) -> None:
        self._connect = connect

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        from app.properties_db import get_connection
        return get_connection()

    @staticmethod
    def _idem(assignment_key: str) -> str:
        key = str(assignment_key or "").strip()
        if not key or len(key) > 128:
            raise SourceSelectionError(
                "a stable assignment_key (1..128 chars) is required for durable finding")
        return f"gabriel-source-finding:{key}"

    @staticmethod
    def _verified_decision(conn: Any, run: Any, record: Any) -> dict[str, Any]:
        from app.workflow import digest_payload
        from app.workflow_db import get_record_in_transaction
        claim = get_record_in_transaction(conn, run.run_id, "research_artifact", "claim", 1)
        evidence = get_record_in_transaction(conn, run.run_id, "research_artifact", "evidence", 1)
        decision = copy.deepcopy(record.payload)
        if (claim is None or evidence is None
                or claim.digest != digest_payload(claim.payload)
                or evidence.digest != digest_payload(evidence.payload)
                or record.digest != _digest(decision)
                or decision.get("digest") != record.digest
                or decision.get("run_id") != str(run.run_id)
                or evidence.payload.get("run_id") != str(run.run_id)
                or decision.get("evidence_digest") != evidence.digest
                or decision.get("mission_fingerprint") != (run.limits or {}).get("mission_fingerprint")
                or evidence.payload.get("mission_fingerprint") != decision.get("mission_fingerprint")
                or claim.payload.get("mission_fingerprint") != decision.get("mission_fingerprint")):
            raise SourceSelectionError("stored source-selection claim/evidence/decision binding failed")
        return decision

    def claim(self, assignment_key: str, fingerprint: str,
              mission_data: dict[str, Any]) -> dict[str, Any]:
        """Atomically claim one assignment; commit before transmission."""
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import (
            create_run_in_transaction, get_record_in_transaction,
            insert_record_if_absent_in_transaction, add_usage_in_transaction,
        )
        idem = self._idem(assignment_key)
        conn = self._open()
        try:
            with conn:
                run = create_run_in_transaction(
                    conn, idempotency_key=idem,
                    limits={"mission_fingerprint": fingerprint,
                            "mission": mission_data,
                            "assignment_key": str(assignment_key)})
                stored_fp = (run.limits or {}).get("mission_fingerprint")
                if stored_fp != fingerprint:
                    raise SourceSelectionError(
                        "assignment_key was already used with a different mission/limits; refusing")
                claim_rec = get_record_in_transaction(
                    conn, run.run_id, KIND_RESEARCH_ARTIFACT, "claim", 1)
                if claim_rec is not None:
                    sel = get_record_in_transaction(
                        conn, run.run_id, KIND_RESEARCH_ARTIFACT, "selection", 1)
                    conn.commit()
                    if sel is not None:
                        return {"status": "completed", "run_id": str(run.run_id),
                                "decision": self._verified_decision(conn, run, sel)}
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                payload = {"state": "claimed", "assignment_key": str(assignment_key),
                           "mission_fingerprint": fingerprint, "mission": mission_data}
                inserted = insert_record_if_absent_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="claim", version=1, payload=payload,
                    digest=digest_payload(payload))
                if inserted is not None:
                    # Reserve the entire finite envelope conservatively. A crash
                    # cannot reopen unused or uncertain capacity on this key.
                    add_usage_in_transaction(conn, run.run_id, "source_requests_reserved",
                                             int(mission_data["budget"]["max_requests"]))
                conn.commit()
                if inserted is None:
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                return {"status": "winner", "run_id": str(run.run_id)}
        except SourceSelectionError:
            _rollback_quietly(conn)
            raise
        except Exception as exc:
            _rollback_quietly(conn)
            raise SourceSelectionError(
                "durable source-finding claim could not be committed; refusing without network") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def complete(self, assignment_key: str, *, decision: dict[str, Any],
                 evidence: dict[str, Any]) -> None:
        """Persist bounded evidence + decision in the claimed run."""
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import (
            get_run_by_key_in_transaction, put_record_in_transaction,
        )
        conn = self._open()
        try:
            with conn:
                run = get_run_by_key_in_transaction(conn, self._idem(assignment_key))
                if run is None:
                    raise SourceSelectionError(
                        "durable source-finding run is missing; cannot persist decision")
                if (decision.get("run_id") != str(run.run_id)
                        or decision.get("mission_fingerprint") != (run.limits or {}).get("mission_fingerprint")
                        or decision.get("evidence_digest") != digest_payload(evidence)
                        or decision.get("digest") != _digest(decision)):
                    raise SourceSelectionError("decision does not bind the claimed run and evidence")
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="evidence", version=1, payload=evidence,
                    digest=digest_payload(evidence))
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="selection", version=1, payload=decision,
                    digest=str(decision.get("digest") or ""))
                conn.commit()
        except SourceSelectionError:
            _rollback_quietly(conn)
            raise
        except Exception as exc:
            _rollback_quietly(conn)
            raise SourceSelectionError(
                "durable source-selection decision could not be persisted") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def load_decision(self, digest: str) -> dict[str, Any] | None:
        """Load a stored decision by digest; verify run identity."""
        from app.workflow import KIND_RESEARCH_ARTIFACT
        from app.workflow_db import get_record_by_digest_in_transaction, get_run_in_transaction
        conn = self._open()
        try:
            with conn:
                rec = get_record_by_digest_in_transaction(
                    conn, KIND_RESEARCH_ARTIFACT, str(digest))
                if rec is None or rec.record_key != "selection":
                    return None
                run = get_run_in_transaction(conn, rec.run_id)
                return self._verified_decision(conn, run, rec) if run is not None else None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _validate_bootstrap(source: str, scope: str, *, server_source: Any = None) -> str:
    """Return the bootstrap descriptor kind or refuse (fail closed)."""
    descriptor = BOOTSTRAP_DESCRIPTORS.get(source)
    if descriptor is None:
        resolved = str(getattr(server_source, "name", "") or "").strip() \
            if server_source is not None else ""
        if resolved and resolved == source:
            return "server_resolved_source"
        raise SourceSelectionError(
            f"bootstrap source {source!r} is not an approved server-owned source")
    allowed = descriptor.get("jurisdictions")
    if allowed and scope.strip().lower() not in allowed:
        raise SourceSelectionError(
            f"bootstrap source {source!r} is only approved for "
            f"{sorted(allowed)}; scope {scope!r} does not match")
    return str(descriptor["kind"])


def bootstrap_selection(*, source: str, jurisdiction: str | None = None,
                        record_types: tuple[str, ...] = ("parcels", "appraisal"),
                        server_source: Any = None) -> dict[str, Any]:
    """Server-constructed explicit bootstrap decision for existing collectors.

    Constructed internally (never from client payloads) only for the
    explicitly server-owned sources in ``BOOTSTRAP_DESCRIPTORS`` (or a
    source object the server itself resolved via its registry, passed
    as ``server_source``), and only with an explicit jurisdiction /
    scope supplied by server code. An omitted jurisdiction, an unknown
    source, or a scope outside the descriptor (e.g. ``dcad_bulk``
    outside Dallas County) refuses. Limitations: bootstrap asserts no
    verified licensing/freshness, grants no registry trust, and its
    retention here plus the pre-collection event write are an audit
    trail, not proof of authorization durability (durable decisions
    come from the workflow store path).
    """
    src = str(source or "").strip()
    if not src:
        raise SourceSelectionError("bootstrap source is required")
    scope = str(jurisdiction or "").strip()
    if not scope:
        raise SourceSelectionError(
            "bootstrap requires an explicit server-owned jurisdiction/scope; "
            "an omitted jurisdiction is never a bootstrap")
    kind = _validate_bootstrap(src, scope, server_source=server_source)
    payload = {"policy_version": POLICY_VERSION, "bootstrap": True,
               "bootstrap_descriptor": kind,
               "mission": {"jurisdiction": scope, "record_types": list(record_types), "country": "US"},
               "ranked": [], "selected_candidate": None,
               "selected_adapter": src, "status": SELECTED,
               "blockers": [], "scope_note": "server-constructed bootstrap; no verified metadata asserted"}
    return _retain(payload)


def select_sources(mission: SourceMission, found: list[FoundCandidate],
                   *, supported_adapters: Any = None) -> dict[str, Any]:
    """Rank found candidates and select one executable adapter, if any."""
    mission.validate()
    if supported_adapters is None:
        from app.agents.gabriel.sources.base import SOURCES
        supported = set(SOURCES)
    else:
        supported = set(supported_adapters)
    ranked_pairs = rank_sources([f.candidate for f in found],
                                needed_record_types=tuple(x.lower() for x in mission.record_types))
    by_identity = {id(f.candidate): f for f in found}
    ranked: list[dict[str, Any]] = []
    selected: FoundCandidate | None = None
    selected_adapter: str | None = None
    blockers: list[str] = []
    saw_needs_review = False
    for candidate, result in ranked_pairs:
        item = by_identity[id(candidate)]
        consistent, trust_reasons = _publisher_consistent(candidate)
        reasons = list(result.reasons) + trust_reasons
        executable_blockers: list[str] = []
        if result.status != "qualified":
            executable_blockers.append(f"qualification is {result.status}")
            if result.status == "needs_review":
                saw_needs_review = True
        if not candidate.license_clear:
            executable_blockers.append("licensing/terms not verified")
        if candidate.update_age_days is None:
            executable_blockers.append("freshness unknown")
        if not consistent:
            executable_blockers.append("publisher identity inconsistent")
            saw_needs_review = True
        if not candidate.jurisdiction:
            executable_blockers.append("candidate jurisdiction not observed")
        elif candidate.jurisdiction.strip().lower() != mission.jurisdiction.strip().lower():
            executable_blockers.append("candidate jurisdiction does not cover the mission jurisdiction")
        if candidate.record_types and not ({x.lower() for x in candidate.record_types} & {x.lower() for x in mission.record_types}):
            executable_blockers.append("candidate record types do not cover the mission")
        if not item.adapter:
            executable_blockers.append("no adapter declared for candidate")
        elif item.adapter not in supported:
            executable_blockers.append(f"adapter {item.adapter!r} is not registered/supported")
        else:
            binding = ADAPTER_BINDINGS.get(item.adapter)
            if binding is None:
                executable_blockers.append("adapter has no server-owned publisher binding")
            if binding is not None and binding.publishers and _host(candidate.publisher_domain) not in binding.publishers:
                executable_blockers.append(
                    f"adapter {item.adapter!r} is bound to a different publisher; a catalog declaration cannot assign it")
            if binding is not None and binding.record_types and candidate.record_types and not (
                    {x.lower() for x in candidate.record_types} & set(binding.record_types)):
                executable_blockers.append(f"adapter {item.adapter!r} does not serve the candidate record types")
        ranked.append({
            "name": candidate.name, "publisher_name": candidate.publisher_name,
            "publisher_domain": candidate.publisher_domain,
            "jurisdiction": candidate.jurisdiction, "status": result.status,
            "p1_eligible": result.p1_eligible, "score": result.score,
            "reasons": reasons, "authority_candidates": list(result.authority_candidates),
            "adapter": item.adapter, "tier": item.tier, "provider": item.provider,
            "evidence_ref": item.evidence_ref,
            "registry_enrolled": _registry_enrolled(candidate),
            "executable_blockers": executable_blockers,
        })
        if selected is None and not executable_blockers:
            selected, selected_adapter = item, item.adapter
    if selected is not None:
        status = SELECTED
    elif saw_needs_review or any(r["status"] == "needs_review" for r in ranked):
        status = NEEDS_REVIEW
        blockers.append("no qualified candidate with verified licensing, known freshness, consistent publisher, and a supported adapter")
    elif ranked:
        status = NO_SUPPORTED_ADAPTER
        blockers.append("no supported adapter for the qualified candidates")
    else:
        status = NO_SUPPORTED_ADAPTER
        blockers.append("no candidate sources were found")
    payload = {"policy_version": POLICY_VERSION, "bootstrap": False, "scope_note": None,
               "mission": {"jurisdiction": mission.jurisdiction,
                           "record_types": list(mission.record_types), "country": mission.country},
               "ranked": ranked, "selected_candidate": selected.candidate.name if selected else None,
               "selected_adapter": selected_adapter, "status": status, "blockers": blockers}
    return _retain(payload)


def build_selection_event(decision: dict[str, Any], *, run_id: str):
    """Build the permanent workflow/event-store record for a decision.

    The event is a per-run audit occurrence, so it takes the canonical
    unique event ID (``AgentEvent`` default). Decision identity lives
    in ``details.digest`` and in the durable workflow store; deriving
    the event ID from the content digest made every independent run
    with the same decision collide on the ``agent_events`` primary
    key under real PostgreSQL.
    """
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
    digest = str(decision.get("digest", ""))
    return AgentEvent(
        agent=AgentId.GABRIEL, run_id=run_id,
        event_type="gabriel.source_selection",
        summary=f"source selection: {decision.get('status')} ({decision.get('selected_adapter') or 'none'})",
        severity=Severity.INFO if decision.get("status") == SELECTED else Severity.WARNING,
        retention_class=RetentionClass.PERMANENT,
        details={"policy_version": POLICY_VERSION, "digest": digest,
                 "mission": decision.get("mission"), "status": decision.get("status"),
                 "selected_candidate": decision.get("selected_candidate"),
                 "selected_adapter": decision.get("selected_adapter"),
                 "ranked": decision.get("ranked"), "blockers": decision.get("blockers")},
        evidence_refs=[{"kind": "source_finding", "ref": r["evidence_ref"]}
                       for r in decision.get("ranked", []) if r.get("evidence_ref")][:20],
    )


def persist_selection(decision: dict[str, Any], *, run_id: str,
                      deliver: Callable[..., Any] | None = None) -> Any:
    """Persist via existing event storage; replay of same digest is idempotent."""
    if deliver is None:
        from app.observability.store import deliver_event as deliver
    return deliver(build_selection_event(decision, run_id=run_id))


def enforce_selection(*, source: str, jurisdiction: str | None,
                      selection: dict[str, Any] | None,
                      durable_store: Any = None) -> dict[str, Any]:
    """Fail closed unless collection matches a server-retained decision.

    Every collection passes through a decision: a ``selected`` decision
    constructed by :func:`select_sources` or the server-constructed
    :func:`bootstrap_selection`. A caller-computed digest is never
    sufficient: the payload must recompute to its digest and exactly
    match the retained server copy, bound to jurisdiction, source
    identity/endpoint, policy revision, and adapter.
    """
    if jurisdiction is None or not str(jurisdiction).strip():
        raise SourceSelectionError("jurisdiction is required; selection cannot be omitted")
    if not isinstance(selection, dict):
        raise SourceSelectionError("source selection is required before collection")
    if selection.get("policy_version") != POLICY_VERSION:
        raise SourceSelectionError("source selection policy version mismatch")
    if selection.get("status") != SELECTED:
        raise SourceSelectionError(f"source selection is not executable: {selection.get('status') or 'missing'}")
    if selection.get("selected_adapter") != source:
        raise SourceSelectionError("requested source does not match the selected adapter")
    digest = selection.get("digest")
    if not digest or _digest(selection) != digest:
        raise SourceSelectionError("source selection digest does not verify")
    retained = _RETAINED.get(str(digest))
    if retained is None:
        # Process cache miss (e.g. after restart): verify against the
        # durable workflow store, never against caller-recomputed
        # hashes alone. The stored payload must match exactly and
        # carry its retained run identity.
        store = durable_store if durable_store is not None else (_DEFAULT_DURABLE_STORE or WorkflowSelectionStore())
        stored = None
        if store is not None:
            try:
                stored = store.load_decision(str(digest))
            except Exception as exc:
                raise SourceSelectionError(
                    "durable source selection lookup failed; refusing") from exc
        if stored is not None and stored == selection and stored.get("run_id"):
            retained = stored
            _RETAINED[str(digest)] = copy.deepcopy(stored)
    if retained is None or retained != selection:
        raise SourceSelectionError("source selection was not constructed and retained by the server")
    if selection.get("bootstrap"):
        if str(source) in BOOTSTRAP_DESCRIPTORS:
            _validate_bootstrap(
                str(source),
                str((selection.get("mission") or {}).get("jurisdiction") or ""))
        elif selection.get("bootstrap_descriptor") != "server_resolved_source":
            raise SourceSelectionError(
                "bootstrap decision does not carry a server-owned descriptor")
    mission = selection.get("mission") or {}
    if not str(mission.get("jurisdiction") or "").strip():
        raise SourceSelectionError("source selection mission jurisdiction is required")
    if str(mission["jurisdiction"]).strip().lower() != str(jurisdiction).strip().lower():
        raise SourceSelectionError("source selection jurisdiction does not match the request")
    if not selection.get("bootstrap"):
        binding = ADAPTER_BINDINGS.get(str(source))
        chosen = next((r for r in selection.get("ranked") or []
                       if r.get("name") == selection.get("selected_candidate")), None)
        if chosen is None:
            raise SourceSelectionError("selected candidate is missing from the retained decision")
        if binding is not None and binding.publishers and _host(str(chosen.get("publisher_domain") or "")) not in binding.publishers:
            raise SourceSelectionError("selected candidate publisher does not match the collector binding")
        if binding is not None and binding.endpoints:
            try:
                from app.agents.gabriel.sources.base import SOURCES
                collector = SOURCES.get(str(source))
                docs_host = _host(str(getattr(collector, "docs_url", "") or "")) if collector else ""
                if docs_host and docs_host not in (binding.publishers | binding.endpoints):
                    raise SourceSelectionError("collector endpoint does not match the adapter binding")
            except SourceSelectionError:
                raise
            except Exception:
                pass
    return selection
