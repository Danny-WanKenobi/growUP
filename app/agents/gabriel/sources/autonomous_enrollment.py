"""Autonomous publisher verification and enrollment for Gabriel.

Gabriel may decide enrollment *on his own*, but only from evidence he
retrieved himself, directly from the publisher's own domain, under the
shared network policy -- never from a catalog claim, a domain suffix
alone, or an LLM judgment. The decision is deterministic: every
criterion below must pass, on observed values, or the outcome is
``needs_more_evidence`` (fail closed). A host-spoof (evidence served
by a different host than the resource claims) is ``rejected``.

An autonomous enrollment is a narrow, fact-level authority grant: it
covers only the recorded facts the observed dataset fields support
(parcel/account identity, situs address, coordinates, recorded
use classification, areas, years -- as observed). It never covers
vacancy, occupancy, availability, ownership, zoning permission,
structural suitability, agricultural feasibility, or profitability.

Enrollments are durable: claim before transmission, evidence and
decision persisted as digest-bound workflow records, replay returns
the retained decision with zero network, and an uncertain attempt
stays consumed. The static curated registry
(``PRODUCTION_REGISTRY``) is never mutated; autonomous enrollments
live in the database and are merged at verification time, with the
curated entries always winning a hostname conflict. Every enrollment
carries a review-due date and expires -- stale evidence must be
re-verified, not trusted forever.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

ENROLLMENT_POLICY_VERSION = "gabriel-autonomous-enrollment-1"
MAX_FRESHNESS_DAYS = 800
REVIEW_DUE_DAYS = 180

_IDENTITY_TOKENS = ("parcel", "account", "apn", "pin", "taxparcel", "folio", "reid")
_LOCATION_TOKENS = ("address", "situs", "latitude", "longitude", "lat", "lon")
_STOPWORDS = {"county", "city", "of", "the", "and", "for", "tx", "texas", "us", "usa"}


class EnrollmentError(ValueError):
    """Autonomous enrollment refused or could not be persisted."""


@dataclass(frozen=True)
class PublisherEvidence:
    """Observed facts retrieved from the publisher's own domain."""

    domain: str
    dataset_id: str
    resource_url: str
    publisher_name: str
    attribution: str
    dataset_name: str
    license_id: str
    license_clear: bool
    freshness_days: int | None
    fields: tuple[str, ...] = ()
    sample_rows: int = 0
    sample_geo_values: tuple[str, ...] = ()
    evidence_digest: str = ""
    retrieved_at: str = ""


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "digest"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _jurisdiction_tokens(jurisdiction: str) -> list[str]:
    parts = [p for p in re.split(r"[^a-z]+", (jurisdiction or "").lower()) if p]
    return [p for p in parts if p not in _STOPWORDS and len(p) > 2]


def _field_tokens(fields: tuple[str, ...]) -> tuple[bool, bool]:
    identity = location = False
    for name in fields:
        parts = [p for p in re.split(r"[^a-z]+", str(name).lower()) if p]
        joined = "".join(parts)
        if any(tok in joined for tok in _IDENTITY_TOKENS):
            identity = True
        if any(tok in parts or tok in joined for tok in _LOCATION_TOKENS):
            location = True
    return identity, location


def verify_socrata_resource(
    resource: dict[str, Any],
    *,
    jurisdiction: str,
    fetch: Callable[..., bytes] | None = None,
    timeout: float = 15.0,
) -> PublisherEvidence:
    """Retrieve publisher-served metadata + a bounded sample.

    Exactly two requests, both to the publisher's own domain under
    ``http_policy``: the Socrata view metadata (publisher identity,
    license, freshness, columns) and a 5-row sample (geography is
    checked against observed values when geographic fields exist).
    Any transport/policy failure raises ``EnrollmentError`` -- the
    attempt is recorded by the caller's durable claim, not retried
    here.
    """
    from app.agents.gabriel.sources.http_policy import HttpPolicyError, fetch_bytes
    from app.agents.gabriel.sources.source_finder import _KNOWN_CLEAR_LICENSES

    domain = str(resource.get("publisher_domain") or "").strip().lower()
    resource_url = str(resource.get("resource_url") or "").strip()
    dataset_id = str(resource.get("dataset_id") or "").strip()
    if not domain or not dataset_id or not resource_url:
        raise EnrollmentError("resource lacks publisher domain, dataset id, or URL")
    if (urlparse(resource_url).hostname or "").lower() != domain:
        raise EnrollmentError("resource URL host does not match claimed publisher domain")

    def _get(url: str, params: dict | None = None) -> bytes:
        if fetch is not None:
            return fetch(url, params or {})
        try:
            return fetch_bytes(url, params=params, timeout=timeout,
                               max_bytes=1_000_000, name="publisher evidence")
        except HttpPolicyError as exc:
            raise EnrollmentError(f"publisher evidence refused: {exc}") from exc

    view_url = f"https://{domain}/api/views/{dataset_id}"
    try:
        view = json.loads(_get(view_url) or b"{}")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher metadata is not valid JSON") from exc
    if not isinstance(view, dict) or str(view.get("id") or "") != dataset_id:
        raise EnrollmentError("publisher metadata does not describe the retained dataset")

    license_raw = view.get("license")
    license_id = ""
    if isinstance(license_raw, dict):
        license_id = str(license_raw.get("name") or license_raw.get("id") or "")
    elif license_raw:
        license_id = str(license_raw)
    columns = view.get("columns") or []
    fields = tuple(
        str(c.get("fieldName") or "") for c in columns
        if isinstance(c, dict) and c.get("fieldName")
    )
    freshness: int | None = None
    updated = view.get("rowsUpdatedAt")
    if isinstance(updated, (int, float)) and updated > 0:
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            float(updated), tz=timezone.utc)
        freshness = max(age.days, 0)

    sample_rows = 0
    geo_values: list[str] = []
    try:
        rows = json.loads(_get(resource_url, {"$limit": 5}) or b"[]")
    except (ValueError, TypeError) as exc:
        raise EnrollmentError("publisher sample is not valid JSON") from exc
    if isinstance(rows, list):
        sample_rows = len([r for r in rows if isinstance(r, dict)])
        geo_keys = ("city", "county", "state", "municipality")
        for row in rows:
            if not isinstance(row, dict):
                continue
            lowered = {str(k).lower(): v for k, v in row.items()}
            for key in geo_keys:
                value = lowered.get(key)
                if value not in (None, ""):
                    geo_values.append(str(value))

    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = {
        "domain": domain, "dataset_id": dataset_id, "resource_url": resource_url,
        "view_url": view_url, "retrieved_at": retrieved,
        "view_name": str(view.get("name") or ""),
        "attribution": str(view.get("attribution") or ""),
        "license_id": license_id, "freshness_days": freshness,
        "fields": list(fields), "sample_rows": sample_rows,
        "sample_geo_values": geo_values[:10],
    }
    return PublisherEvidence(
        domain=domain,
        dataset_id=dataset_id,
        resource_url=resource_url,
        publisher_name=str(resource.get("publisher_name") or ""),
        attribution=str(view.get("attribution") or ""),
        dataset_name=str(view.get("name") or ""),
        license_id=license_id,
        license_clear=license_id.strip().lower() in _KNOWN_CLEAR_LICENSES,
        freshness_days=freshness,
        fields=fields,
        sample_rows=sample_rows,
        sample_geo_values=tuple(geo_values[:10]),
        evidence_digest=hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        retrieved_at=retrieved,
    )


def decide_enrollment(evidence: PublisherEvidence, *, jurisdiction: str) -> dict[str, Any]:
    """Deterministic enrollment decision over observed evidence only.

    Every criterion must pass for ``enrolled``. A host mismatch is
    impossible here (verification refuses it earlier); all other
    shortfalls are ``needs_more_evidence`` with the exact missing
    criterion named -- Gabriel records what he still lacks instead of
    guessing.
    """
    tokens = _jurisdiction_tokens(jurisdiction)
    identity_fields, location_fields = _field_tokens(evidence.fields)
    haystack = " ".join([
        evidence.domain, evidence.attribution, evidence.dataset_name,
        evidence.publisher_name,
    ]).lower()
    domain_jurisdiction = any(tok in evidence.domain for tok in tokens)
    named_jurisdiction = any(tok in haystack for tok in tokens)
    geo_match = True
    if evidence.sample_geo_values:
        geo_match = any(
            tok in value.lower()
            for value in evidence.sample_geo_values for tok in tokens
        )
    criteria = {
        "government_domain": evidence.domain.endswith(".gov"),
        "publisher_identity_matches_jurisdiction": bool(
            domain_jurisdiction or named_jurisdiction),
        "license_known_clear": bool(evidence.license_clear),
        "freshness_known_and_recent": bool(
            evidence.freshness_days is not None
            and evidence.freshness_days <= MAX_FRESHNESS_DAYS),
        "property_fields_observed": bool(identity_fields or location_fields)
        and bool(location_fields),
        "sample_rows_observed": evidence.sample_rows > 0,
        "sample_geography_consistent": geo_match,
    }
    missing = [name for name, ok in criteria.items() if not ok]
    now = datetime.now(timezone.utc)
    decision: dict[str, Any] = {
        "policy_version": ENROLLMENT_POLICY_VERSION,
        "status": "enrolled" if not missing else "needs_more_evidence",
        "domain": evidence.domain,
        "dataset_id": evidence.dataset_id,
        "resource_url": evidence.resource_url,
        "jurisdiction": jurisdiction,
        "publisher_name": evidence.publisher_name or evidence.attribution,
        "criteria": criteria,
        "missing": missing,
        "evidence_digest": evidence.evidence_digest,
        "retrieved_at": evidence.retrieved_at,
        "decided_at": now.isoformat(timespec="seconds"),
        "review_due": (now + timedelta(days=REVIEW_DUE_DAYS)).date().isoformat()
        if not missing else "",
        "scope_note": (
            "Autonomous enrollment (owner-authorized 2026-11-03): narrow "
            "authority for recorded facts supported by the observed "
            "fields only. Never vacancy, occupancy, availability, "
            "ownership, zoning, structural, agricultural, or financial "
            "authority."),
    }
    decision["digest"] = _digest(decision)
    return decision


class EnrollmentStore:
    """Durable enrollment store on the existing workflow tables.

    Mirrors the source-selection claim pattern: an exclusive claim
    (with a conservative two-request reservation) commits before any
    publisher transmission; evidence and decision persist as
    digest-bound ``research_artifact`` records in that run. No DDL.
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
    def _idem(domain: str, dataset_id: str) -> str:
        return f"gabriel-enrollment:{domain}:{dataset_id}"[:512]

    def claim(self, domain: str, dataset_id: str, fingerprint: str) -> dict[str, Any]:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import (
            create_run_in_transaction, get_record_in_transaction,
            insert_record_if_absent_in_transaction, add_usage_in_transaction,
        )
        conn = self._open()
        try:
            with conn:
                run = create_run_in_transaction(
                    conn, idempotency_key=self._idem(domain, dataset_id),
                    limits={"mission_fingerprint": fingerprint,
                            "domain": domain, "dataset_id": dataset_id})
                if (run.limits or {}).get("mission_fingerprint") != fingerprint:
                    raise EnrollmentError(
                        "enrollment key was already used for a different resource; refusing")
                existing = get_record_in_transaction(
                    conn, run.run_id, KIND_RESEARCH_ARTIFACT, "decision", 1)
                if existing is not None:
                    conn.commit()
                    return {"status": "completed", "run_id": str(run.run_id),
                            "decision": dict(existing.payload or {})}
                claim_rec = get_record_in_transaction(
                    conn, run.run_id, KIND_RESEARCH_ARTIFACT, "claim", 1)
                if claim_rec is not None:
                    conn.commit()
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                payload = {"state": "claimed", "domain": domain,
                           "dataset_id": dataset_id,
                           "mission_fingerprint": fingerprint}
                inserted = insert_record_if_absent_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="claim", version=1, payload=payload,
                    digest=digest_payload(payload))
                if inserted is not None:
                    add_usage_in_transaction(
                        conn, run.run_id, "source_requests_reserved", 2)
                conn.commit()
                if inserted is None:
                    return {"status": "uncertain", "run_id": str(run.run_id)}
                return {"status": "winner", "run_id": str(run.run_id)}
        except EnrollmentError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise EnrollmentError(
                "durable enrollment claim could not be committed; refusing without network") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def complete(self, domain: str, dataset_id: str, *, decision: dict[str, Any],
                 evidence_payload: dict[str, Any]) -> None:
        from app.workflow import KIND_RESEARCH_ARTIFACT, digest_payload
        from app.workflow_db import get_run_by_key_in_transaction, put_record_in_transaction
        conn = self._open()
        try:
            with conn:
                run = get_run_by_key_in_transaction(conn, self._idem(domain, dataset_id))
                if run is None:
                    raise EnrollmentError("enrollment run is missing; cannot persist decision")
                if decision.get("digest") != _digest(decision):
                    raise EnrollmentError("enrollment decision digest does not verify")
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="evidence", version=1, payload=evidence_payload,
                    digest=digest_payload(evidence_payload))
                put_record_in_transaction(
                    conn, run.run_id, kind=KIND_RESEARCH_ARTIFACT,
                    record_key="decision", version=1, payload=decision,
                    digest=str(decision.get("digest") or ""))
                conn.commit()
        except EnrollmentError:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            raise EnrollmentError("enrollment decision could not be persisted") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def load_enrollments(self) -> list[dict[str, Any]]:
        """All retained enrollment decisions (digest-verified)."""
        conn = self._open()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT r.payload, r.digest FROM workflow_records r "
                        "JOIN workflow_runs w ON w.run_id = r.run_id "
                        "WHERE w.idempotency_key LIKE 'gabriel-enrollment:%' "
                        "AND r.kind = 'research_artifact' AND r.record_key = 'decision'")
                    rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                payload = row["payload"] if isinstance(row, dict) else row[0]
                digest = row["digest"] if isinstance(row, dict) else row[1]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    continue
                if payload.get("digest") != digest or _digest(payload) != digest:
                    continue
                out.append(payload)
            return out
        finally:
            try:
                conn.close()
            except Exception:
                pass


def run_autonomous_enrollment(
    resource: dict[str, Any],
    *,
    jurisdiction: str,
    store: EnrollmentStore | None = None,
    fetch: Callable[..., bytes] | None = None,
) -> dict[str, Any]:
    """Verify one retained generic resource and decide enrollment.

    Socrata resources only in this version (publisher-served view
    metadata is the evidence); other platforms return
    ``needs_more_evidence`` without network. Replay of a decided
    resource returns the retained decision with zero requests.
    """
    platform = str(resource.get("platform") or "").strip().lower()
    domain = str(resource.get("publisher_domain") or "").strip().lower()
    dataset_id = str(resource.get("dataset_id") or "").strip()
    if platform != "socrata" or not domain or not dataset_id:
        return {"policy_version": ENROLLMENT_POLICY_VERSION,
                "status": "needs_more_evidence",
                "missing": ["publisher-served evidence path not implemented "
                            "for this platform"],
                "domain": domain, "dataset_id": dataset_id}
    fingerprint = _digest({
        "domain": domain, "dataset_id": dataset_id,
        "resource_url": str(resource.get("resource_url") or ""),
        "jurisdiction": jurisdiction,
        "policy_version": ENROLLMENT_POLICY_VERSION,
    })
    store = store if store is not None else EnrollmentStore()
    claim = store.claim(domain, dataset_id, fingerprint)
    if claim.get("status") == "completed":
        return claim["decision"]
    if claim.get("status") != "winner":
        raise EnrollmentError(
            "enrollment for this resource was already claimed and its "
            "outcome is uncertain; it will not be auto-retransmitted")
    evidence = verify_socrata_resource(resource, jurisdiction=jurisdiction, fetch=fetch)
    decision = decide_enrollment(evidence, jurisdiction=jurisdiction)
    decision["run_id"] = claim.get("run_id", "")
    decision["digest"] = _digest(decision)
    store.complete(domain, dataset_id, decision=decision, evidence_payload={
        "domain": evidence.domain, "dataset_id": evidence.dataset_id,
        "resource_url": evidence.resource_url,
        "publisher_name": evidence.publisher_name,
        "attribution": evidence.attribution,
        "dataset_name": evidence.dataset_name,
        "license_id": evidence.license_id,
        "license_clear": evidence.license_clear,
        "freshness_days": evidence.freshness_days,
        "fields": list(evidence.fields),
        "sample_rows": evidence.sample_rows,
        "sample_geo_values": list(evidence.sample_geo_values),
        "evidence_digest": evidence.evidence_digest,
        "retrieved_at": evidence.retrieved_at,
        "fingerprint": fingerprint,
    })
    return decision


_REGISTRY_CACHE: dict[str, Any] = {"at": None, "registry": None}


def effective_registry(*, store: EnrollmentStore | None = None):
    """Curated registry merged with unexpired autonomous enrollments.

    Curated entries always win. Autonomous entries add only hosts the
    curated registry does not know, only while their decision is
    ``enrolled`` and their review-due date has not passed. A database
    failure yields the curated registry alone (fail closed).
    """
    from app.agents.gabriel.source_registry import (
        PRODUCTION_REGISTRY, RegistryEntry, SourceClass, SourceRegistry,
    )
    now = datetime.now(timezone.utc)
    cached_at = _REGISTRY_CACHE.get("at")
    if cached_at is not None and (now - cached_at).total_seconds() < 60:
        return _REGISTRY_CACHE["registry"]
    registry = PRODUCTION_REGISTRY
    try:
        decisions = (store or EnrollmentStore()).load_enrollments()
    except Exception:
        decisions = []
    today = now.date().isoformat()
    entries = dict(PRODUCTION_REGISTRY.entries)
    for decision in decisions:
        if decision.get("status") != "enrolled":
            continue
        if str(decision.get("review_due") or "") < today:
            continue
        domain = str(decision.get("domain") or "").strip().lower()
        if not domain or domain in entries:
            continue
        entries[domain] = RegistryEntry(
            hostname=domain,
            source_class=SourceClass.AUTHORITATIVE,
            outlet_name=str(decision.get("publisher_name") or domain),
            publisher=str(decision.get("publisher_name") or domain),
            record_types=[],
            added_by=f"gabriel autonomous enrollment {ENROLLMENT_POLICY_VERSION}",
            added_on=str(decision.get("decided_at") or "")[:10],
            review_due=str(decision.get("review_due") or ""),
            note=str(decision.get("scope_note") or ""),
        )
    if len(entries) != len(PRODUCTION_REGISTRY.entries):
        registry = SourceRegistry(entries=entries)
    _REGISTRY_CACHE.update({"at": now, "registry": registry})
    return registry


def clear_registry_cache() -> None:
    _REGISTRY_CACHE.update({"at": None, "registry": None})
