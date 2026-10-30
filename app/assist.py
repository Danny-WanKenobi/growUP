"""Wondrue assistance capabilities (briefs, execution, artifact attach).

Each capability retrieves stored context deterministically, sends one
bounded model request through the shared inference boundary, validates
the structured output, and attaches successful results as attributed
artifacts. Model output never authorizes dossiers, never alters
deterministic results, and never sends or publishes anything.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
from typing import Any
from uuid import UUID

from app.model_adapter import (
    DRAFT_KINDS,
    DRAFT_SCHEMA_VERSION,
    ECONOMICS_SCHEMA_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    EXPLAIN_SCHEMA_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    ModelError,
    validate_draft,
    validate_economics_explanation,
    validate_evidence_analysis,
    validate_explanation,
    validate_extraction,
)
from app.modes import demo_mode_enabled
from app.workflow import KIND_WONDRUE_ASSISTANCE, digest_payload
from app.workflow_db import (
    get_record_in_transaction,
    get_run_in_transaction,
    list_records_in_transaction,
    put_record_in_transaction,
)
from app.wondrue import WondrueError

MAX_EVIDENCE_ITEMS = 40
MAX_SNIPPET_CHARS = 600
MAX_BRIEF_CHARS = 20000
MAX_PAGES = 12
MAX_IMAGES = 6
MAX_PAGE_TEXT_CHARS = 12000
MAX_IMAGE_B64_CHARS = 7000000
MAX_IMAGE_BYTES = 5242880
MAX_RETAINED_BYTES = 8000000

IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})
PAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp", "text/plain"})


def _image_format(raw: bytes) -> str | None:
    """Detect a supported image format from magic bytes (never trust MIME)."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _run_uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WondrueError("run_id must be an operational UUID") from exc


def _dossier_identity(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Derive stored dossier identity and lineage version for one record."""
    from app.custody import CustodyError, dossier_id_for_property

    dossier_id: UUID | None = None
    raw_prop = str(payload.get("property_id", "") or "")
    if raw_prop:
        try:
            dossier_id = dossier_id_for_property(UUID(raw_prop))
        except (ValueError, AttributeError, TypeError, CustodyError):
            dossier_id = None
    try:
        version: int | None = int(payload.get("version", 1))
    except (TypeError, ValueError):
        version = None
    return {"key": key, "dossier_id": dossier_id, "version": version}


def resolve_run_dossier(
    conn: Any, run_id: UUID, dossier_key: str | None = None
) -> dict[str, Any] | None:
    """Deterministically resolve a run's dossier selection.

    An explicitly supplied key wins when it names a stored dossier
    version. Otherwise the stored inventory decides: a single
    property-associated dossier is unambiguous, a lone unassociated
    record is selected with a null identity, and multiple remaining
    candidates require explicit selection. Returns None only when the
    run genuinely has no dossier record.
    """
    records = list_records_in_transaction(conn, run_id, "dossier_version")
    by_key = {str(record.record_key): record for record in records}
    if dossier_key:
        key = str(dossier_key)
        record = by_key.get(key)
        if record is None:
            available = sorted(by_key) or ["none stored"]
            raise WondrueError(
                f"unknown dossier key {key!r}; available: {', '.join(available)}"
            )
        return _dossier_identity(key, _payload(record))
    if not records:
        return None
    associated = [
        record for record in records
        if str((_payload(record).get("property_id", "") or ""))
    ]
    if len(associated) == 1:
        key = str(associated[0].record_key)
        return _dossier_identity(key, _payload(associated[0]))
    if len(associated) > 1 or len(records) > 1:
        keys = sorted(
            str(record.record_key) for record in (associated or records)
        )
        raise WondrueError(
            "run has multiple dossiers; supply dossier_key "
            f"(one of: {', '.join(keys)})"
        )
    key = str(records[0].record_key)
    return _dossier_identity(key, _payload(records[0]))


def list_stored_dossier_identities(conn: Any, run_id: UUID) -> dict[str, dict[str, Any]]:
    """Map stored dossier identity to its key/version for one run."""
    identities: dict[str, dict[str, Any]] = {}
    for record in list_records_in_transaction(conn, run_id, "dossier_version"):
        identity = _dossier_identity(str(record.record_key), _payload(record))
        if identity["dossier_id"] is not None:
            identities[str(identity["dossier_id"])] = identity
    return identities


def _payload(record: Any) -> dict[str, Any]:
    data = getattr(record, "payload", None)
    return dict(data) if isinstance(data, dict) else {}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " [truncated for prompt bound]"


def attach_assistance(
    conn: Any,
    run_id: UUID,
    capability: str,
    request_id: str,
    artifact: dict[str, Any],
    model_result: Any = None,
) -> dict[str, Any]:
    """Attach one attributed assistance artifact (new rows only).

    Assistance never mutates authoritative dossier contents or
    historical snapshots, and never becomes a dossier revision.
    """
    from app.mvp import record_model_execution

    key = f"{capability}-{request_id[:12]}"
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_WONDRUE_ASSISTANCE, record_key=key,
        version=1, payload=artifact, digest=digest_payload(artifact),
    )
    if model_result is not None:
        record_model_execution(conn, run_id, f"inference-{request_id[:12]}", model_result)
    return {"kind": KIND_WONDRUE_ASSISTANCE, "record_key": stored.record_key}


def build_explain_brief(
    conn: Any, run_id: UUID, selection: dict[str, Any] | None
) -> dict[str, Any]:
    """Deterministic brief over stored results plus the stop reason."""
    run = get_run_in_transaction(conn, run_id)
    if run is None:
        raise WondrueError("unknown workflow run")
    dossier: dict[str, Any] = {}
    if selection is not None:
        dossier = _payload(
            get_record_in_transaction(
                conn, run_id, "dossier_version", selection["key"]
            )
        )
    decisions = [
        _payload(item)
        for item in list_records_in_transaction(conn, run_id, "raphael_decision")
    ]
    scenarios = [
        _payload(item)
        for item in list_records_in_transaction(conn, run_id, "scenario")
    ]
    reviews = [
        _payload(item)
        for item in list_records_in_transaction(conn, run_id, "identity_review")
    ]
    inner = dossier.get("dossier", {}) if isinstance(dossier, dict) else {}
    decision = decisions[-1] if decisions else {}
    outcome = str(decision.get("outcome", "") or "")
    basis = [str(item) for item in (decision.get("basis", []) or [])]
    facts = {
        "run_stage": getattr(run, "stage", ""),
        "run_status": getattr(run, "status", ""),
        "property_id": str(dossier.get("property_id", "") or ""),
        "category": str((inner.get("category_attribution") or {}).get("observed", "") or ""),
        "county": str((inner.get("county_attribution") or {}).get("value", "") or ""),
        "decision_outcome": outcome,
        "decision_basis": basis[:5],
        "scenarios": [
            {
                "name": str(item.get("name", "")),
                "complete": bool(item.get("complete")),
                "financial_verdict": str(
                    ((item.get("financial_assessment") or {}).get("assessment") or {}
                     ).get("verdict", "")
                ),
            }
            for item in scenarios
        ],
    }
    estimates: list[str] = []
    assumptions: list[str] = []
    unknowns: list[str] = []
    for item in scenarios:
        finance = (item.get("financial_assessment") or {}).get("assessment") or {}
        for label in ("capex_usd", "opex_usd_per_year", "revenue_usd_per_year",
                      "operating_net_usd_per_year"):
            value = (finance.get("outputs") or {}).get(label)
            if value is not None:
                estimates.append(f"{item.get('name')}.{label} = {value} (estimated)")
        assumptions.extend(str(a) for a in (finance.get("assumptions", []) or []))
        unknowns.extend(f"missing: {m}" for m in (item.get("missing", []) or []))
        unknowns.extend(str(e) for e in (finance.get("evidence_requests", []) or []))
    unknowns.extend(
        f"identity review {item.get('reason', '')}: "
        f"{len(item.get('candidates', []) or [])} candidate(s)"
        for item in reviews
    )
    stop = _stop_reason(run, decision, scenarios, reviews)
    assumptions = sorted(set(assumptions))
    return {
        "facts": facts,
        "dossier": _selection_brief(selection),
        "estimates": estimates[:20],
        "assumptions": assumptions[:20],
        "unknowns": unknowns[:30],
        "omitted": {
            "estimates": max(0, len(estimates) - 20),
            "assumptions": max(0, len(assumptions) - 20),
            "unknowns": max(0, len(unknowns) - 30),
        },
        "stop": stop,
    }


def _selection_brief(selection: dict[str, Any] | None) -> dict[str, Any]:
    """JSON-safe dossier selection shared by every brief."""
    if selection is None:
        return {"key": None, "dossier_id": None, "version": None}
    identity = selection.get("dossier_id")
    return {
        "key": selection.get("key"),
        "dossier_id": str(identity) if identity else None,
        "version": selection.get("version"),
    }


def _stop_reason(
    run: Any,
    decision: dict[str, Any],
    scenarios: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> str:
    status = str(getattr(run, "status", "") or "")
    stage = str(getattr(run, "stage", "") or "")
    outcome = str(decision.get("outcome", "") or "")
    if not decision:
        return f"stopped before any Raphael decision (stage {stage}, status {status})"
    if outcome != "authorized":
        basis = decision.get("basis", []) or ["see decision basis"]
        return f"Raphael decision {outcome or 'missing'}: {basis[0]}"
    incomplete = [item for item in scenarios if not item.get("complete")]
    if incomplete:
        names = ", ".join(str(item.get("name", "?")) for item in incomplete)
        return f"authorized; engineering incomplete for: {names}"
    pending = [
        item for item in scenarios
        if not (item.get("financial_assessment") or {}).get("assessed")
    ]
    if pending:
        return "authorized; Michael financial assessment pending"
    unfavorable = [
        str(item.get("name", "?")) for item in scenarios
        if ((item.get("financial_assessment") or {}).get("assessment") or {}
            ).get("verdict") == "unfavorable"
    ]
    if unfavorable:
        return f"completed; unfavorable economics for: {', '.join(unfavorable)}"
    if reviews:
        return "completed with identity-review notes; see unknowns"
    return f"completed (stage {stage}, status {status})"


def _dispatch(
    *,
    agent: str,
    capability: str,
    purpose: str,
    brief: dict[str, Any],
    schema_version: str,
    validator: Any,
    ctx: dict[str, Any],
    dossier_id: Any = None,
    dossier_version: int | None = None,
    run_id: Any = None,
    package_entry: Any = None,
    images: list[dict[str, str]] | None = None,
    optional: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    from app.inference import complete_capability

    try:
        return complete_capability(
            agent=agent,
            capability=capability,
            purpose=purpose,
            payload={"brief": brief},
            principal=str(ctx.get("principal", "") or ""),
            dossier_id=dossier_id,
            dossier_version=dossier_version,
            run_id=run_id,
            package_entry=package_entry,
            max_tokens=int(ctx.get("max_tokens", 1024) or 1024),
            use_live=bool(ctx.get("use_live", False)),
            mode=str(ctx.get("mode", "read-only")),
            approved=True,
            optional=optional,
            demo=bool(ctx.get("demo", False)),
            approval_via_token=bool(ctx.get("approval_via_token", False)),
            idempotency_key=idempotency_key,
            images=images,
            schema_version=schema_version,
            validator=validator,
        )
    except ModelError as exc:
        raise WondrueError(str(exc)) from exc


def _envelope(
    completion: dict[str, Any],
    *,
    brief: dict[str, Any],
    data_key: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if completion.get("outcome") == "deferred":
        return {
            "deferred": True,
            "deferred_reason": completion.get("deferred_reason", ""),
            "budget": completion.get("budget"),
        }
    model_result = completion.get("model_result")
    envelope = {
        "brief": brief,
        data_key: completion.get("result", {}),
        "model": {
            "mode": getattr(model_result, "mode", "none"),
            "provider": getattr(model_result, "provider", ""),
            "model": getattr(model_result, "model", ""),
        },
        "live": bool(completion.get("live", False)),
        "outcome": completion.get("outcome"),
        "request_id": completion.get("request_id"),
        "estimated_cost_usd": completion.get("estimated_cost_usd"),
        "budget": completion.get("budget"),
        "demo_mode": demo_mode_enabled(),
    }
    if completion.get("replay_of"):
        envelope["replay_of"] = completion["replay_of"]
    envelope.update(extra or {})
    return envelope


def assist_explain(conn: Any, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Explain stored pipeline results, including why a property stopped."""
    if conn is None:
        raise WondrueError("real target requires a database connection")
    run_id = _run_uuid(args.get("run_id"))
    requested = str(args.get("dossier_key", "") or args.get("key", "") or "") or None
    selection = resolve_run_dossier(conn, run_id, requested)
    brief = build_explain_brief(conn, run_id, selection)
    question = str(args.get("question", "") or "")[:1000]
    if question:
        brief["question"] = question
    if len(json.dumps(brief, default=str)) > MAX_BRIEF_CHARS:
        raise WondrueError("stored context exceeds explanation bound; narrow the run")
    completion = _dispatch(
        agent="wondrue", capability="assist_explain", purpose="assist_explain",
        brief=brief, schema_version=EXPLAIN_SCHEMA_VERSION,
        validator=validate_explanation, ctx=ctx, run_id=run_id,
        dossier_id=(selection or {}).get("dossier_id"),
        dossier_version=(selection or {}).get("version"),
        idempotency_key=str(args.get("idempotency_key", "") or "") or None,
    )
    envelope = _envelope(completion, brief=brief, data_key="commentary")
    envelope["commentary_note"] = (
        "model-composed explanation; the stored brief is authoritative"
    )
    if completion.get("live") and completion.get("model_result") is not None:
        envelope["artifact"] = attach_assistance(
            conn, run_id, "assist_explain", str(completion["request_id"]),
            {"brief": brief, "commentary": completion["result"],
             "live": True, "request_id": completion["request_id"]},
            completion["model_result"],
        )
    return envelope


def build_evidence_brief(
    conn: Any, run_id: UUID, selection: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Evidence items with stable ids plus citable text per id."""
    artifacts = list_records_in_transaction(conn, run_id, "research_artifact")
    items: list[dict[str, Any]] = []
    citable: dict[str, str] = {}
    omitted = 0
    for record in artifacts:
        payload = _payload(record)
        key = str(getattr(record, "record_key", "") or payload.get("assignment_id", ""))
        results = payload.get("results", []) or []
        for index, entry in enumerate(results):
            if not isinstance(entry, dict):
                continue
            if len(items) >= MAX_EVIDENCE_ITEMS:
                omitted += 1
                continue
            item_id = f"{key}#{index}"
            title = str(entry.get("title", "") or "")
            snippet = _truncate(str(entry.get("snippet", "") or ""), MAX_SNIPPET_CHARS)
            items.append(
                {
                    "evidence_id": item_id,
                    "url": str(entry.get("url", "") or ""),
                    "domain": str(entry.get("domain", "") or ""),
                    "title": title,
                    "snippet": snippet,
                }
            )
            citable[item_id] = f"{title} {snippet}"
    brief = {
        "evidence": items,
        "omitted_items": omitted,
        "dossier": _selection_brief(selection),
        "note": (
            "Findings are suggestions only: no verification, source "
            "authentication, independence, occupancy, zoning, distress, "
            "or authorization claims."
        ),
    }
    return brief, citable


def analyze_evidence(conn: Any, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Suggest conflicts, gaps, and research questions over dossier evidence."""
    if conn is None:
        raise WondrueError("real target requires a database connection")
    run_id = _run_uuid(args.get("run_id"))
    requested = str(args.get("dossier_key", "") or "") or None
    selection = resolve_run_dossier(conn, run_id, requested)
    brief, citable = build_evidence_brief(conn, run_id, selection)
    if not citable:
        raise WondrueError("no stored evidence available for analysis")
    completion = _dispatch(
        agent="raphael", capability="analyze_evidence", purpose="analyze_evidence",
        brief=brief, schema_version=EVIDENCE_SCHEMA_VERSION,
        validator=functools.partial(validate_evidence_analysis, evidence=citable),
        ctx=ctx, run_id=run_id,
        dossier_id=(selection or {}).get("dossier_id"),
        dossier_version=(selection or {}).get("version"),
        idempotency_key=str(args.get("idempotency_key", "") or "") or None,
    )
    envelope = _envelope(completion, brief=brief, data_key="suggestions")
    envelope["suggestions_note"] = (
        "model suggestions only; not verified findings, and no "
        "authorization or new research follows from them"
    )
    if completion.get("live") and completion.get("model_result") is not None:
        envelope["artifact"] = attach_assistance(
            conn, run_id, "analyze_evidence", str(completion["request_id"]),
            {"brief": brief, "suggestions": completion["result"],
             "live": True, "request_id": completion["request_id"]},
            completion["model_result"],
        )
    return envelope


def build_economics_brief(
    conn: Any,
    run_id: UUID,
    scenario_key: str = "",
    selection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Labeled deterministic figures plus the label allowlist."""
    records = list_records_in_transaction(conn, run_id, "scenario")
    figures: dict[str, dict[str, str]] = {}
    scenarios: list[dict[str, Any]] = []
    for record in records:
        payload = _payload(record)
        name = str(payload.get("name", "") or getattr(record, "record_key", ""))
        if scenario_key and name != scenario_key:
            continue
        finance = (payload.get("financial_assessment") or {}).get("assessment") or {}
        if not finance.get("complete"):
            scenarios.append({"name": name, "complete": False})
            continue
        outputs = finance.get("outputs") or {}
        metrics = {
            "capex_usd": outputs.get("capex_usd"),
            "opex_usd_per_year": outputs.get("opex_usd_per_year"),
            "revenue_usd_per_year": outputs.get("revenue_usd_per_year"),
            "operating_net_usd_per_year": outputs.get("operating_net_usd_per_year"),
            "simple_payback_years": outputs.get("simple_payback_years"),
        }
        for metric, value in metrics.items():
            if value is not None:
                figures[f"{name}:{metric}"] = {
                    "label": f"{name}:{metric}",
                    "value": str(value),
                    "provenance": "michael.assess",
                }
        for sens, value in ((finance.get("outputs") or {}).get("sensitivity") or {}).items():
            figures[f"{name}:sensitivity:{sens}"] = {
                "label": f"{name}:sensitivity:{sens}",
                "value": str(value),
                "provenance": "michael.assess",
            }
        scenarios.append(
            {
                "name": name,
                "complete": True,
                "verdict": str(finance.get("verdict", "")),
                "verdict_reason": str(finance.get("verdict_reason", "")),
                "drivers": [str(d) for d in (finance.get("drivers", []) or [])],
            }
        )
    brief = {
        "scenarios": scenarios,
        "figures": figures,
        "dossier": _selection_brief(selection),
        "note": (
            "Figures are deterministic Michael outputs; the model explains "
            "them by label and must not invent computed values."
        ),
    }
    return brief, set(figures)


def explain_economics(conn: Any, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Explain Michael's computed results and sensitivities by figure label."""
    if conn is None:
        raise WondrueError("real target requires a database connection")
    run_id = _run_uuid(args.get("run_id"))
    requested = str(args.get("dossier_key", "") or "") or None
    selection = resolve_run_dossier(conn, run_id, requested)
    brief, labels = build_economics_brief(
        conn, run_id, str(args.get("scenario", "") or ""), selection
    )
    if not labels:
        raise WondrueError("no completed financial assessment available")
    completion = _dispatch(
        agent="michael", capability="explain_economics", purpose="explain_economics",
        brief=brief, schema_version=ECONOMICS_SCHEMA_VERSION,
        validator=functools.partial(
            validate_economics_explanation, figure_labels=labels
        ),
        ctx=ctx, run_id=run_id,
        dossier_id=(selection or {}).get("dossier_id"),
        dossier_version=(selection or {}).get("version"),
        idempotency_key=str(args.get("idempotency_key", "") or "") or None,
    )
    envelope = _envelope(completion, brief=brief, data_key="commentary")
    envelope["commentary_note"] = (
        "model commentary; authoritative values are server-rendered below"
    )
    if completion.get("outcome") != "deferred":
        envelope["rendered"] = _render_economics_points(
            brief["figures"], completion.get("result", {})
        )
    if completion.get("live") and completion.get("model_result") is not None:
        envelope["artifact"] = attach_assistance(
            conn, run_id, "explain_economics", str(completion["request_id"]),
            {"brief": brief, "commentary": completion["result"],
             "rendered": envelope.get("rendered", []),
             "live": True, "request_id": completion["request_id"]},
            completion["model_result"],
        )
    return envelope


def _render_economics_points(
    figures: dict[str, dict[str, str]], commentary: dict[str, Any]
) -> list[dict[str, Any]]:
    """Server-render cited labels to authoritative values and provenance."""
    rendered = []
    points = commentary.get("points", []) if isinstance(commentary, dict) else []
    for point in points:
        if not isinstance(point, dict):
            continue
        values = []
        for label in point.get("figures", []) or []:
            figure = figures.get(label)
            if figure is not None:
                values.append(dict(figure))
        rendered.append({"text": point.get("text", ""), "values": values})
    return rendered


def _validated_pages(pages: Any) -> list[dict[str, Any]]:
    if not isinstance(pages, list) or not pages:
        raise WondrueError("pages must be a non-empty list")
    if len(pages) > MAX_PAGES:
        raise WondrueError(f"at most {MAX_PAGES} pages per extraction")
    cleaned: list[dict[str, Any]] = []
    seen: set[int] = set()
    images = 0
    total_bytes = 0
    for entry in pages:
        if not isinstance(entry, dict):
            raise WondrueError("pages entries must be objects")
        try:
            page_no = int(entry.get("page_no", 0))
        except (TypeError, ValueError) as exc:
            raise WondrueError("page_no must be an integer") from exc
        if page_no < 1:
            raise WondrueError("page_no must be positive")
        if page_no in seen:
            raise WondrueError(f"duplicate page_no: {page_no}")
        seen.add(page_no)
        mime = str(entry.get("mime", "") or "")
        if mime not in PAGE_MIMES:
            raise WondrueError(
                f"unsupported page format {mime!r}: supply PNG/JPEG/WebP "
                "images or extracted text"
            )
        if mime == "text/plain":
            text = entry.get("text", "")
            if not isinstance(text, str) or not text.strip():
                raise WondrueError("text pages require extracted text")
            if len(text) > MAX_PAGE_TEXT_CHARS:
                raise WondrueError("text page exceeds size bound")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            total_bytes += len(text.encode("utf-8"))
            cleaned.append(
                {
                    "page_no": page_no, "mime": mime, "text": text,
                    "digest": digest, "bytes": len(text.encode("utf-8")),
                }
            )
            continue
        images += 1
        if images > MAX_IMAGES:
            raise WondrueError(f"at most {MAX_IMAGES} images per extraction")
        data_b64 = entry.get("data_b64", "")
        if not isinstance(data_b64, str) or not data_b64:
            raise WondrueError("image pages require base64 data")
        if len(data_b64) > MAX_IMAGE_B64_CHARS:
            raise WondrueError("image page exceeds size bound")
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except Exception as exc:
            raise WondrueError("image page is not valid base64") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise WondrueError("image page exceeds size bound")
        detected = _image_format(raw)
        if detected is None:
            raise WondrueError("image page is not a supported image format")
        if detected != mime:
            raise WondrueError(
                f"image content is {detected}, not the claimed {mime}"
            )
        digest = hashlib.sha256(raw).hexdigest()
        total_bytes += len(raw)
        cleaned.append(
            {
                "page_no": page_no, "mime": mime, "data_b64": data_b64,
                "digest": digest, "bytes": len(raw),
            }
        )
    if total_bytes > MAX_RETAINED_BYTES:
        raise WondrueError(
            f"extraction exceeds the {MAX_RETAINED_BYTES}-byte aggregate bound"
        )
    cleaned.sort(key=lambda page: page["page_no"])
    return cleaned


def validate_extraction_body(
    body: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Validate an extraction request into working pages plus a canonical spec.

    The canonical spec binds ask-mode approval to the complete
    operation: document/source identity, page identities and content
    digests, run/dossier association, live/replay selection, request
    limits, and idempotency identity. It carries digests, never bulk
    bytes; execution re-derives digests from the supplied pages and
    rejects any material mismatch.
    """
    if not isinstance(body, dict):
        raise WondrueError("extraction body must be an object")
    doc_id = str(body.get("doc_id", "") or "")
    source_ref = str(body.get("source_ref", "") or "")
    if not doc_id or not source_ref:
        raise WondrueError("doc_id and source_ref are required")
    if len(doc_id) > 200 or len(source_ref) > 500:
        raise WondrueError("doc_id or source_ref too long")
    pages = _validated_pages(body.get("pages"))
    run_raw = body.get("run_id")
    run_id = _run_uuid(run_raw) if run_raw else None
    dossier_raw = body.get("dossier_id")
    try:
        dossier_id = UUID(str(dossier_raw)) if dossier_raw else None
    except (ValueError, AttributeError, TypeError) as exc:
        raise WondrueError("dossier_id must be an operational UUID") from exc
    dossier_key = str(body.get("dossier_key", "") or "") or None
    if dossier_key and len(dossier_key) > 200:
        raise WondrueError("dossier_key too long")
    if dossier_key and run_id is None:
        raise WondrueError("dossier_key requires a run_id")
    use_live = bool(ctx.get("use_live", False))
    try:
        max_tokens = int(ctx.get("max_tokens", 1024) or 1024)
    except (TypeError, ValueError) as exc:
        raise WondrueError("max_tokens must be an integer") from exc
    idempotency_key = str(body.get("idempotency_key", "") or "") or None
    canonical = {
        "doc_id": doc_id,
        "source_ref": source_ref,
        "pages": [
            {
                "page_no": page["page_no"], "mime": page["mime"],
                "digest": page["digest"], "bytes": page["bytes"],
            }
            for page in pages
        ],
        "run_id": str(run_id) if run_id else None,
        "dossier_id": str(dossier_id) if dossier_id else None,
        "dossier_key": dossier_key,
        "use_live": use_live,
        "max_tokens": max_tokens,
        "idempotency_key": idempotency_key,
    }
    return {
        "canonical": canonical,
        "pages": pages,
        "run_id": run_id,
        "dossier_id": dossier_id,
        "dossier_key": dossier_key,
    }


def check_originals_in_transaction(
    conn: Any, doc_id: str, source_ref: str, pages: list[dict[str, Any]]
) -> set[int]:
    """Fast-fail obvious same-locator conflicts before any provider spend.

    Returns the page numbers already retained with identical identity.
    Advisory only: the atomic store after inference is the guarantee,
    since another request may commit between this check and storage.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT page_no, mime, digest, source_ref FROM extraction_originals "
            "WHERE doc_id = %(doc)s",
            {"doc": doc_id},
        )
        rows = cur.fetchall() or []
    stored = {int(row["page_no"]): dict(row) for row in rows}
    kept: set[int] = set()
    for page in pages:
        known = stored.get(page["page_no"])
        if known is None:
            continue
        _require_same_original(doc_id, page, known, source_ref)
        kept.add(page["page_no"])
    return kept


def _require_same_original(
    doc_id: str,
    page: dict[str, Any],
    known: dict[str, Any],
    source_ref: str = "",
) -> None:
    """Reject a retained original whose identity differs from the page.

    Identity is MIME, content digest, and source: identical bytes from
    a newly claimed source do not silently merge into an old original.
    """
    if str(known.get("mime", "") or "") != str(page.get("mime", "") or ""):
        raise WondrueError(
            f"page {page['page_no']} of {doc_id!r} was already stored "
            "with a different mime; use a new doc_id"
        )
    if str(known.get("digest", "") or "") != str(page.get("digest", "") or ""):
        raise WondrueError(
            f"page {page['page_no']} of {doc_id!r} was already stored "
            "with a different digest; use a new doc_id"
        )
    if source_ref and str(known.get("source_ref", "") or "") != source_ref:
        raise WondrueError(
            f"page {page['page_no']} of {doc_id!r} was already stored "
            "with a different source; use a new doc_id"
        )


def store_originals_in_transaction(
    conn: Any,
    doc_id: str,
    source_ref: str,
    pages: list[dict[str, Any]],
    request_id: str,
    skip: set[int] | None = None,
) -> int:
    """Atomically retain page originals; conflicts raise, reuse is free.

    Each page inserts with ``ON CONFLICT DO NOTHING RETURNING``; pages
    that lose a race are re-read and compared on MIME and digest, so a
    conflicting writer can never appear successful and claims never
    attach to a different retained original. Returns the actual number
    of newly inserted pages. Runs after inference in the route
    transaction: no locks are held across the provider call.
    """
    skip = skip or set()
    stored = 0
    with conn.cursor() as cur:
        for page in pages:
            if page["page_no"] in skip:
                continue
            content = page["data_b64"] if page["mime"] != "text/plain" else page["text"]
            cur.execute(
                "INSERT INTO extraction_originals (doc_id, page_no, mime, "
                "digest, content, byte_size, source_ref, request_id) "
                "VALUES (%(doc)s, %(page)s, %(mime)s, %(digest)s, "
                "%(content)s, %(bytes)s, %(source)s, %(request)s) "
                "ON CONFLICT (doc_id, page_no) DO NOTHING RETURNING page_no",
                {
                    "doc": doc_id, "page": page["page_no"], "mime": page["mime"],
                    "digest": page["digest"], "content": content,
                    "bytes": page["bytes"], "source": source_ref,
                    "request": str(request_id),
                },
            )
            if cur.fetchone() is not None:
                stored += 1
                continue
            cur.execute(
                "SELECT mime, digest, source_ref FROM extraction_originals "
                "WHERE doc_id = %(doc)s AND page_no = %(page)s",
                {"doc": doc_id, "page": page["page_no"]},
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - no deletes exist
                raise WondrueError("original retention conflict could not be verified")
            _require_same_original(doc_id, page, dict(row), source_ref)
    return stored


def extract_document(
    conn: Any,
    body: dict[str, Any],
    ctx: dict[str, Any],
    approved_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract unverified candidate claims from supplied pages/images.

    No URL fetching, no filesystem access, no crawler: pages arrive
    inline, and originals are retained in extraction_originals under
    the database's existing access control. When an approval token
    bound a canonical spec, the resubmitted operation must match it
    exactly; mismatched pages, associations, or live/replay selection
    are rejected before any spend.
    """
    if conn is None:
        raise WondrueError("real target requires a database connection")
    validated = validate_extraction_body(body, ctx)
    if approved_spec is not None and validated["canonical"] != approved_spec:
        raise WondrueError("approval token does not cover this extraction operation")
    doc_id = validated["canonical"]["doc_id"]
    source_ref = validated["canonical"]["source_ref"]
    pages = validated["pages"]
    run_id: UUID | None = validated["run_id"]
    supplied_dossier: UUID | None = validated["dossier_id"]
    supplied_key: str | None = validated["dossier_key"]
    if run_id is not None and get_run_in_transaction(conn, run_id) is None:
        raise WondrueError("unknown workflow run")
    # Only stored-verified dossier identity reaches the ledger.
    # Caller assertions that cannot be checked stay out of usage
    # attribution and are marked as such in the brief and artifact.
    verified: dict[str, Any] | None = None
    asserted: str | None = None
    if run_id is not None:
        stored = list_stored_dossier_identities(conn, run_id)
        if supplied_key is not None:
            selection = resolve_run_dossier(conn, run_id, supplied_key)
            assert selection is not None  # explicit key names a stored record
            if supplied_dossier is not None and str(supplied_dossier) != str(
                selection.get("dossier_id") or ""
            ):
                raise WondrueError("dossier_id does not match the dossier_key record")
            verified = selection
        elif supplied_dossier is not None:
            match = stored.get(str(supplied_dossier))
            if match is None and stored:
                raise WondrueError("dossier_id does not match the run's stored dossier")
            verified = match
            if match is None:
                asserted = str(supplied_dossier)
        else:
            verified = resolve_run_dossier(conn, run_id, None)
    elif supplied_dossier is not None:
        asserted = str(supplied_dossier)
    dossier_id = (verified or {}).get("dossier_id")
    dossier_version = (verified or {}).get("version")
    association = (
        "stored" if verified is not None
        else ("caller-asserted" if asserted is not None else "none")
    )
    kept = check_originals_in_transaction(conn, doc_id, source_ref, pages)
    locators: dict[str, str] = {}
    prompt_pages: list[dict[str, Any]] = []
    images: list[dict[str, str]] = []
    for page in pages:
        locator = f"{doc_id}:{page['page_no']}"
        locators[locator] = f"{doc_id} page {page['page_no']} ({page['mime']})"
        if page["mime"] == "text/plain":
            prompt_pages.append(
                {
                    "page": locator, "mime": page["mime"],
                    "digest": page["digest"], "text": page["text"],
                }
            )
        else:
            prompt_pages.append(
                {
                    "page": locator, "mime": page["mime"],
                    "digest": page["digest"], "bytes": page["bytes"],
                }
            )
            images.append(
                {
                    "mime": page["mime"], "data_b64": page["data_b64"],
                    "digest": page["digest"], "page": locator,
                }
            )
    brief = {
        "doc_id": doc_id,
        "source_ref": source_ref,
        "pages": prompt_pages,
        "dossier": {
            **_selection_brief(verified),
            "assertion": association,
            "asserted_dossier_id": asserted,
        },
        "instruction": (
            "Extract candidate claims exactly as shown. Never infer "
            "engineering approval from a floor plan. Claims stay "
            "unverified until Raphael's policy accepts supporting evidence."
        ),
    }
    completion = _dispatch(
        agent="gabriel", capability="extract_document", purpose="extract_document",
        brief=brief, schema_version=EXTRACTION_SCHEMA_VERSION,
        validator=functools.partial(validate_extraction, pages=locators),
        ctx=ctx, dossier_id=dossier_id, dossier_version=dossier_version,
        run_id=run_id, images=images or None,
        idempotency_key=validated["canonical"]["idempotency_key"],
    )
    envelope = _envelope(completion, brief=_public_brief(brief), data_key="extraction")
    envelope["verification"] = "unverified: claims require Raphael-policy evidence"
    envelope["association"] = {
        "dossier": association,
        "note": "caller-asserted links are unverified until stored identity confirms them",
    }
    if completion.get("outcome") == "deferred":
        return envelope
    store_originals_in_transaction(
        conn, doc_id, source_ref, pages, str(completion["request_id"]), kept
    )
    if (
        run_id is not None
        and completion.get("live")
        and completion.get("model_result") is not None
    ):
        envelope["artifact"] = attach_assistance(
            conn, run_id, "extract_document", str(completion["request_id"]),
            {"brief": _public_brief(brief), "extraction": completion["result"],
             "live": True, "request_id": completion["request_id"]},
            completion["model_result"],
        )
    return envelope


def _public_brief(brief: dict[str, Any]) -> dict[str, Any]:
    """Brief safe to return/store (page metadata, never image bytes)."""
    pages = []
    for page in brief.get("pages", []) or []:
        entry = {
            "page": page.get("page"),
            "mime": page.get("mime"),
            "digest": page.get("digest"),
        }
        if page.get("mime") == "text/plain":
            entry["text"] = page.get("text")
        else:
            entry["bytes"] = page.get("bytes")
        pages.append(entry)
    return {
        "doc_id": brief.get("doc_id"),
        "source_ref": brief.get("source_ref"),
        "pages": pages,
        "dossier": brief.get("dossier", {}),
    }


def draft_outreach(conn: Any, args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """Draft Uriel campaign copy from an eligible package (never sends)."""
    from app.uriel import UrielError, get_package_in_transaction

    if conn is None:
        raise WondrueError("real target requires a database connection")
    kind = str(args.get("draft_kind", "") or "")
    if kind not in DRAFT_KINDS:
        raise WondrueError(
            f"unsupported draft kind {kind!r}: one of {sorted(DRAFT_KINDS)}"
        )
    entry = str(args.get("package_entry", "") or "")
    if not entry:
        raise WondrueError("package_entry is required")
    try:
        package = get_package_in_transaction(conn, entry)
    except UrielError as exc:
        raise WondrueError(str(exc)) from exc
    if package is None:
        raise WondrueError("unknown Uriel package")
    if package.get("simulated"):
        raise WondrueError("synthetic packages are not eligible for drafting")
    if str(package.get("execution_state", "")) != "not_attempted":
        raise WondrueError("executed packages are not eligible for drafting")
    state = str(package.get("package_state", "") or "")
    if state not in ("prepared", "reviewed"):
        raise WondrueError("package is not qualified for drafting")
    payload = package.get("payload") or {}
    unknowns = [str(u) for u in (payload.get("unknowns", []) or [])]
    blockers = [str(b) for b in (payload.get("blockers", []) or [])]
    brief = {
        "package_entry": str(package.get("entry_id")),
        "dossier_id": str(package.get("dossier_id")),
        "version": package.get("version"),
        "package_state": state,
        "lineage": payload.get("lineage", {}),
        "engineering": _truncate(json.dumps(payload.get("engineering", {}), default=str), 2000),
        "economics": _truncate(json.dumps(payload.get("economics", {}), default=str), 2000),
        "unknowns": unknowns[:20],
        "blockers": blockers[:20],
        "omitted": {
            "unknowns": max(0, len(unknowns) - 20),
            "blockers": max(0, len(blockers) - 20),
        },
        "draft_kind": kind,
        "note": (
            "Draft content only: no image/video generation, print "
            "fulfillment, message delivery, publication, or outreach "
            "automation. Disclose material uncertainty in the copy."
        ),
    }
    try:
        dossier_id = UUID(str(package.get("dossier_id")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WondrueError("package names an unusable dossier") from exc
    completion = _dispatch(
        agent="uriel", capability="draft_outreach", purpose="draft_outreach",
        brief=brief, schema_version=DRAFT_SCHEMA_VERSION,
        validator=functools.partial(validate_draft, kind=kind),
        ctx=ctx, dossier_id=dossier_id,
        dossier_version=package.get("version"),
        package_entry=package.get("entry_id"),
        optional=(kind == "audience_variant"),
        idempotency_key=str(args.get("idempotency_key", "") or "") or None,
    )
    envelope = _envelope(completion, brief=brief, data_key="draft")
    envelope["delivery"] = "not sent: drafts never send, publish, or automate outreach"
    return envelope


def inference_usage_snapshot() -> dict[str, Any]:
    """Allowance state plus usage by agent/capability (read-only)."""
    from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
    from app.inference import read_usage_snapshot

    try:
        return read_usage_snapshot()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise WondrueError(f"inference usage unavailable: {exc}") from exc
