"""Uriel: opportunity and outreach campaign packages (canonical role).

Uriel prepares genuine campaign packages from dossier lineage, evidence,
Raphael decisions, Onesimus engineering comparisons, Michael economics,
unknowns, and next actions. He does not send, schedule, or otherwise
execute outreach: no real sender is implemented, and preparation-only
scope holds in both DEMO_MODE values.

Packages are append-only: one outcome row per (dossier, version) with
package_state draft/reviewed/prepared decided at insert. The row is
genuine in both modes (simulated=False always); DEMO_MODE is recorded
informationally and never relabels real preparation as synthetic.
Execution state stays not_attempted until a real sender exists; Priscilla
records simulated or actual delivery as separate conversion events.

Commands (operator-invoked, from the repository root)::

    python -m app.uriel list [--state draft|reviewed|prepared]
    python -m app.uriel show <entry-id>
"""

from __future__ import annotations

import json
import sys
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.modes import demo_mode_enabled
from app.properties_db import get_connection
from app.workflow import is_demo_id

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

URIEL_PACKAGE_VERSION = "uriel-1"
ROLE_SCHEMA = "canonical1-uriel"
PACKAGE_STATES = ("draft", "reviewed", "prepared")


class UrielError(ValueError):
    """Invalid package request (bad identity, custody, state, or duplicate)."""


def _uuid(value: Any, name: str) -> UUID:
    if is_demo_id(value):
        raise UrielError(f"demo identifiers are rejected by Uriel: {name}")
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise UrielError(f"{name} must be an operational UUID") from exc


def _receipt_in_transaction(conn: Any, receipt_id: UUID) -> dict[str, Any]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT receipt_id, dossier_id, version, snapshot_id, kind, "
                "from_agent, to_agent FROM custody_receipts "
                "WHERE receipt_id = %(rid)s",
                {"rid": receipt_id},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if row is None:
        raise UrielError("unknown custody receipt for package")
    return dict(row)


def prepare_package_in_transaction(
    conn: Any,
    *,
    dossier_id: UUID | str,
    version: int,
    receipt_id: UUID | str,
    prepared_by: str,
    dossier: dict[str, Any] | None = None,
    engineering: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    unknowns: list[str] | None = None,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    review: dict[str, Any] | None = None,
    campaign: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare one genuine package; state decided at insert (append-only).

    Custody: the referenced receipt must exist, name this dossier
    version, and hand custody to Uriel. State is prepared when a
    review is supplied and no blockers remain, reviewed when a review
    is supplied with blockers, else draft. Re-preparing the same
    dossier version is rejected (one outcome row per version).
    """
    dossier_uuid = _uuid(dossier_id, "dossier_id")
    receipt_uuid = _uuid(receipt_id, "receipt_id")
    if not isinstance(version, int) or version < 1:
        raise UrielError("version must be a positive integer")
    if not prepared_by or not str(prepared_by).strip():
        raise UrielError("prepared_by principal is required")
    receipt = _receipt_in_transaction(conn, receipt_uuid)
    if str(receipt.get("dossier_id")) != str(dossier_uuid):
        raise UrielError("receipt names a different dossier")
    if int(receipt.get("version", -1)) != version:
        raise UrielError("receipt names a different dossier version")
    if str(receipt.get("to_agent", "")) != "uriel":
        raise UrielError("receipt does not hand custody to Uriel")
    snapshot_uuid = _uuid(receipt.get("snapshot_id"), "snapshot_id")
    from app.pilot import (
        cell_from_dossier,
        record_transition_in_transaction,
    )

    cell = cell_from_dossier(dossier or {})
    state = "draft"
    if review is not None:
        if not str(review.get("reviewed_by", "") or "").strip():
            raise UrielError("review requires a reviewed_by principal")
        state = "prepared" if not (blockers or []) else "reviewed"
    payload = {
        "package_version": URIEL_PACKAGE_VERSION,
        "prepared_by": str(prepared_by),
        "demo_mode": demo_mode_enabled(),
        "lineage": {
            "dossier_id": str(dossier_uuid),
            "version": version,
            "snapshot_id": str(snapshot_uuid),
            "receipt_id": str(receipt_uuid),
            "receipt_kind": str(receipt.get("kind", "")),
            "receipt_from": str(receipt.get("from_agent", "")),
        },
        "cell": {"county": cell[0], "category": cell[1]} if cell else None,
        "engineering": engineering or {},
        "economics": economics or {},
        "unknowns": list(unknowns or []),
        "blockers": list(blockers or []),
        "next_actions": list(next_actions or []),
        "review": review or {},
        "campaign": campaign or {},
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id FROM uriel_packages "
                "WHERE dossier_id = %(dossier)s AND version = %(version)s",
                {"dossier": dossier_uuid, "version": version},
            )
            if cur.fetchone() is not None:
                raise UrielError("package already prepared for this dossier version")
            entry_id = uuid4()
            cur.execute(
                "INSERT INTO uriel_packages (entry_id, dossier_id, version, "
                "snapshot_id, receipt_id, demo_mode, simulated, payload, "
                "role_schema, package_state, execution_state) "
                "VALUES (%(eid)s, %(dossier)s, %(version)s, %(snapshot)s, "
                "%(receipt)s, %(demo)s, FALSE, %(payload)s, %(schema)s, "
                "%(state)s, 'not_attempted') "
                "RETURNING entry_id, created_at",
                {
                    "eid": entry_id,
                    "dossier": dossier_uuid,
                    "version": version,
                    "snapshot": snapshot_uuid,
                    "receipt": receipt_uuid,
                    "demo": payload["demo_mode"],
                    "payload": json.dumps(payload),
                    "schema": ROLE_SCHEMA,
                    "state": state,
                },
            )
            inserted = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        if getattr(exc, "sqlstate", "") == "23505":
            raise UrielError("package already prepared for this dossier version") from exc
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if cell is not None:
        record_transition_in_transaction(
            conn, county=cell[0], category=cell[1], dossier_id=dossier_uuid,
            transition="package_prepared", record_key=f"package-v{version}",
        )
    created = dict(inserted) if inserted is not None else {}
    return {
        "entry_id": str(entry_id),
        "dossier_id": str(dossier_uuid),
        "version": version,
        "package_state": state,
        "simulated": False,
        "payload": payload,
        "created_at": str(created.get("created_at", "")),
    }


def get_package_in_transaction(conn: Any, entry_id: UUID | str) -> dict[str, Any] | None:
    """Load one package row with its JSON payload decoded."""
    entry_uuid = _uuid(entry_id, "entry_id")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_id, dossier_id, version, snapshot_id, receipt_id, "
                "demo_mode, simulated, payload, role_schema, package_state, "
                "execution_state, created_at FROM uriel_packages "
                "WHERE entry_id = %(eid)s",
                {"eid": entry_uuid},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    if row is None:
        return None
    record = dict(row)
    payload = record.get("payload")
    if isinstance(payload, str):
        record["payload"] = json.loads(payload)
    for key in ("entry_id", "dossier_id", "snapshot_id", "receipt_id"):
        record[key] = str(record.get(key))
    record["created_at"] = str(record.get("created_at", ""))
    return record


def list_packages_in_transaction(
    conn: Any, *, package_state: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List packages newest-first, optionally filtered by state."""
    if package_state is not None and package_state not in PACKAGE_STATES:
        raise UrielError(f"unknown package state: {package_state!r}")
    if not isinstance(limit, int) or not 1 <= limit <= 200:
        raise UrielError("limit must be 1..200")
    try:
        with conn.cursor() as cur:
            if package_state is None:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "created_at FROM uriel_packages "
                    "ORDER BY created_at DESC, entry_id DESC LIMIT %(limit)s",
                    {"limit": limit},
                )
            else:
                cur.execute(
                    "SELECT entry_id, dossier_id, version, package_state, "
                    "created_at FROM uriel_packages "
                    "WHERE package_state = %(state)s "
                    "ORDER BY created_at DESC, entry_id DESC LIMIT %(limit)s",
                    {"state": package_state, "limit": limit},
                )
            rows = cur.fetchall() or []
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    return [
        {
            "entry_id": str(row.get("entry_id")),
            "dossier_id": str(row.get("dossier_id")),
            "version": row.get("version"),
            "package_state": row.get("package_state"),
            "created_at": str(row.get("created_at", "")),
        }
        for row in (dict(item) for item in rows)
    ]


# --- Opportunity ranking (deterministic, preparation-only) -----------------
#
# Ranking never sends anything and never upgrades evidence: it orders
# evaluated candidates by documented criteria so an operator can see
# what is outreach-ready, what is evidence-pending, and what is not
# ready. Verified facts outrank every projection; a favorable
# financial verdict under assumptions earns less than a verified
# category fact, and unresolved gaps always cost points.

RANKING_WEIGHTS = {
    "category_verified": 30,
    "raphael_authorized": 25,
    "raphael_needs_evidence": 5,
    "engineering_complete": 10,
    "finance_complete": 10,
    "finance_favorable": 15,
    "contact_known": 5,
    "evidence_gap_each": -2,
    "evidence_gap_floor": -10,
}


def _candidate_key(candidate: dict[str, Any]) -> str:
    account = str(candidate.get("account") or "").strip()
    if account:
        return f"account:{account}"
    return f"property:{candidate.get('property_id')}"


def rank_opportunities(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank evaluated candidates deterministically (no side effects)."""
    seen: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _candidate_key(candidate)
        if key in seen:
            duplicates.append({"key": key, "reason": "duplicate candidate"})
            continue
        seen[key] = candidate

    ranked: list[dict[str, Any]] = []
    for key, candidate in seen.items():
        gaps = [str(gap) for gap in candidate.get("evidence_gaps") or []]
        breakdown = {
            "category_verified": (
                RANKING_WEIGHTS["category_verified"]
                if candidate.get("category_verified") else 0
            ),
            "raphael": (
                RANKING_WEIGHTS["raphael_authorized"]
                if candidate.get("raphael_outcome") == "authorized"
                else RANKING_WEIGHTS["raphael_needs_evidence"]
                if candidate.get("raphael_outcome") == "needs_evidence"
                else 0
            ),
            "engineering": (
                RANKING_WEIGHTS["engineering_complete"]
                if candidate.get("engineering_complete") else 0
            ),
            "finance": (
                RANKING_WEIGHTS["finance_complete"]
                if candidate.get("finance_complete") else 0
            ) + (
                RANKING_WEIGHTS["finance_favorable"]
                if candidate.get("finance_verdict")
                == "favorable_under_assumptions" else 0
            ),
            "contact": (
                RANKING_WEIGHTS["contact_known"]
                if candidate.get("owner_contact") else 0
            ),
            "evidence_gaps": max(
                RANKING_WEIGHTS["evidence_gap_floor"],
                RANKING_WEIGHTS["evidence_gap_each"] * len(gaps),
            ),
        }
        verified = bool(candidate.get("category_verified"))
        authorized = candidate.get("raphael_outcome") == "authorized"
        contact = bool(candidate.get("owner_contact"))
        if verified and authorized and contact:
            readiness = "outreach_ready"
        elif verified and authorized:
            readiness = "evidence_pending"
        else:
            readiness = "not_ready"
        ranked.append({
            "key": key,
            "property_id": candidate.get("property_id"),
            "account": candidate.get("account"),
            "address": candidate.get("address"),
            "score": sum(breakdown.values()),
            "score_breakdown": breakdown,
            "readiness": readiness,
            "missing_data": gaps + (
                [] if contact else ["owner contact not identified"]
            ) + (
                [] if verified else ["category not verified"]
            ) + (
                [] if authorized else ["Raphael authorization outstanding"]
            ),
        })
    tier_order = {"outreach_ready": 0, "evidence_pending": 1, "not_ready": 2}
    ranked.sort(key=lambda item: (
        tier_order[item["readiness"]], -item["score"], item["key"]))
    return {
        "ranked": ranked,
        "duplicates": duplicates,
        "criteria": dict(RANKING_WEIGHTS),
        "note": (
            "Ranking is preparation only: it sends nothing, and no "
            "score upgrades an unverified fact."
        ),
    }


def draft_outreach(entry: dict[str, Any]) -> dict[str, Any]:
    """Draft (never send) outreach for one outreach-ready entry.

    The text may cite only the verified category fact and the
    address; it must not claim vacancy, availability, suitability,
    or owner circumstances the evidence does not establish.
    """
    if entry.get("readiness") != "outreach_ready":
        raise UrielError("outreach draft requires an outreach-ready entry")
    address = str(entry.get("address") or "").strip()
    if not address:
        raise UrielError("outreach draft requires a property address")
    return {
        "status": "draft_not_sent",
        "to": entry.get("owner_contact"),
        "subject": f"Interest in {address}",
        "body": (
            f"Hello — we are evaluating underused commercial space in "
            f"Dallas for a preliminary indoor-growing concept. Public "
            f"appraisal records for {address} record a commercial "
            f"classification that our verification process has "
            f"confirmed at the recorded-fact level. If you are open "
            f"to a brief conversation about the property's future, "
            f"we would welcome it. This note is exploratory; it "
            f"assumes nothing about the property's availability or "
            f"your plans for it."
        ),
        "facts_used": ["recorded commercial classification (verified)"],
        "facts_not_claimed": [
            "vacancy", "availability", "current use", "suitability",
        ],
    }


def _owned(fn: Any, *args: Any, config: DatabaseConfig | None = None, **kwargs: Any) -> Any:
    conn = get_connection(config)
    try:
        result = fn(conn, *args, **kwargs)
        conn.commit()
        return result
    except (UrielError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("uriel_unavailable") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _print_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("no packages")
        return
    for row in rows:
        print(
            f"{row['entry_id']} dossier={row['dossier_id']} "
            f"v{row['version']} state={row['package_state']} "
            f"created={row['created_at']}"
        )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("list", "show"):
        print("usage: python -m app.uriel list [--state S] | show <entry-id>", file=sys.stderr)
        return 2
    try:
        if args[0] == "list":
            state = None
            if args[1:3] == ["--state"] or (len(args) == 3 and args[1] == "--state"):
                state = args[2]
            elif len(args) > 1:
                print("usage: python -m app.uriel list [--state S]", file=sys.stderr)
                return 2
            _print_list(_owned(list_packages_in_transaction, package_state=state))
            return 0
        if len(args) != 2:
            print("usage: python -m app.uriel show <entry-id>", file=sys.stderr)
            return 2
        record = _owned(get_package_in_transaction, args[1])
        if record is None:
            print("package not found", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2, sort_keys=True, default=str))
        return 0
    except (UrielError, DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        print(f"uriel: {exc}", file=sys.stderr)
        return 1



def prepare_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    """Evidence-linked plain-text campaign drafts from the inherited dossier only."""
    from app.workflow import digest_payload
    if payload.get('digest') != digest_payload({k:v for k,v in payload.items() if k!='digest'}):
        raise UrielError('campaign input failed dossier digest verification')
    dossier = payload.get('dossier') or {}
    reports = dossier.get('reports') or {}
    refs = {}
    for agent in ('gabriel','raphael','onesimus','michael'):
        report = reports.get(agent)
        # Older canonical Gabriel dossiers carry evidence directly, not a report.
        if agent == 'gabriel' and report is None:
            refs[agent] = {'dossier_digest':payload['digest'],'sources':list(dossier.get('sources') or [])}
            continue
        if not isinstance(report,dict) or report.get('digest') != digest_payload({k:v for k,v in report.items() if k!='digest'}):
            raise UrielError(f'campaign requires intact {agent} report')
        refs[agent] = {'report_digest':report['digest'],'sources':list(report.get('evidence_refs') or [])}
    if reports['raphael'].get('outcome') != 'authorized':
        raise UrielError('campaign requires preliminary Raphael authorization')
    from app.linked_reassessments import stale_dependencies
    stale=stale_dependencies(reports)
    if any(a in stale for a in ('raphael','onesimus','michael')):
        raise UrielError('campaign inputs contain stale inspection, engineering or financial reports')
    assessments = reports['michael'].get('assessments') or {}
    claims = []
    for name, assessment in sorted(assessments.items()):
        if not assessment.get('complete'):
            continue
        for quantity in ('capex_usd','revenue_usd_per_year','opex_usd_per_year','operating_net_usd_per_year','monthly_operating_cashflow_usd'):
            value = (assessment.get('outputs') or {}).get(quantity)
            if value is not None:
                claims.append({'scenario':name,'quantity':quantity,'value':str(value),
                    'evidence':refs['michael'],'scope':'Conditional deterministic estimate; not actual performance or guaranteed returns.'})
    room_finance=reports['michael'].get('room_finance') or {}
    if room_finance.get('complete'):
        for quantity,value in sorted((room_finance.get('totals') or {}).items()):
            claims.append({'scenario':'room_portfolio','quantity':quantity,'value':str(value),'evidence':refs['michael'],
                'scope':'Conditional sum of separately allocated room plans; an alternative to single-room scenarios, never added to them.'})
    supplied = (dossier.get('specialist_inputs') or {}).get('uriel') or {}
    contact = supplied.get('owner_contact') or {}
    from app.contact_evidence import assess_contact
    contact_assessment = assess_contact(contact,property_id=payload['property_id'])
    policy = dossier.get('outreach_policy') or {}
    if not isinstance(policy,dict) or ('suppressed' in policy and not isinstance(policy['suppressed'],bool)):
        raise UrielError('invalid outreach suppression policy')
    address = str(dossier.get('street_address') or 'Address unresolved')
    financial_text = '\n'.join(f"{c['scenario']}: {c['quantity']} = {c['value']} [Michael {refs['michael']['report_digest']}]" for c in claims)
    limitations = 'Preliminary concept only. Availability, vacancy, structural/electrical capacity, permitted use and executable conversion are not established by this campaign. Figures depend on retained assumptions and exclude unmodeled costs. Alternative plans are not additive.'
    from app.personalities import presentation
    introduction = presentation('uriel',f'Grow Up: preliminary indoor-growing opportunity for {address}.\nWe would welcome a discussion of this passive Kratky concept.\n{financial_text}\n{limitations}','email',introduce=True)['text']
    campaign = {'version':'uriel-campaign-2','property_id':str(payload['property_id']),
        'source_dossier_digest':payload['digest'],'status':'suppressed' if policy.get('suppressed') else 'draft_not_sent',
        'source_reports':refs,'claims':claims,'to':contact_assessment['email'] if contact_assessment['eligible_for_draft'] else None,
        'contact_assessment':contact_assessment,
        'contact_evidence_refs':list(contact.get('evidence_refs') or []),
        'drafts':{'introduction_email':{'subject':f'Preliminary growing concept: {address}','body':introduction},
            'follow_up_email':{'subject':f'Possible follow-up: {address}','body':introduction+'\nDraft follow-up only; send history must be checked before any contact.'},
            'letter':introduction,'flyer':introduction,'business_card':{'organization':'Grow Up','description':'Preliminary property-to-agriculture feasibility','contact':None}},
        'execution':{'enabled':False,'sent':False,'provider_calls':0,'human_channels':['email'],'postal_delivery_enabled':False},
        'missing':([] if contact_assessment['eligible_for_draft'] else ['owner_contact_evidence'])+([] if claims else ['complete_financial_assessment']),
        'limitations':[limitations,'Draft creation does not establish channel permission or recipient identity.']}
    campaign['claim_review']={'financial_claim_count':len(claims),'factual_source_authentication':'not established by draft creation',
        'conditional_scenario_names':sorted({c['scenario'] for c in claims}),
        'must_not_claim':['Guaranteed returns or savings','Verified vacancy, ownership, structural safety or permitted agricultural use','A draft was sent, delivered or accepted'],
        'evidence_gaps':list(dict.fromkeys(gap for assessment in assessments.values() if isinstance(assessment,dict) for gap in assessment.get('evidence_requests',[]))),
        'suppression_preserved':bool(policy.get('suppressed')),'outbound_delivery_authorized':False}
    from app.uriel_campaign import collateral
    campaign['collateral'] = collateral(introduction)
    campaign['data_provenance'] = 'synthetic' if dossier.get('synthetic') else 'inherited_evidence'
    campaign['campaign_plan'] = {'day_offsets':[0,7,14,21], 'window':'one month',
        'human_contact_channel':'email','physical_mail_enabled':False,'collateral_distribution':'email_attachment_only',
        'contact':'simeon@growup.securecodeusa.com', 'costs':'increasing collateral tiers; dollar costs unknown',
        'status':'draft_not_scheduled'}
    campaign['campaign_id'] = 'campaign:'+digest_payload(campaign)
    return campaign


if __name__ == "__main__":
    raise SystemExit(main())
