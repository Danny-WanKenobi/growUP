"""Wondrue conversational interface (MVP): JSON commands + results page."""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth import wondrue_grant
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.demo import DEMO_LABEL
from app.model_adapter import ModelError
from app.properties_db import get_connection
from app.wondrue import DEMO, WondrueAuthError, WondrueError, handle_command
from app.workflow_db import WorkflowError

router = APIRouter(tags=["wondrue"])


class CommandRequest(BaseModel):
    mode: str = "read-only"
    text: str = ""
    target: str = "demo"
    approval_token: str | None = None
    use_live: bool = False
    intent: dict[str, Any] | None = None


def _close_conn(conn: Any, *, rollback: bool) -> None:
    """Rollback (when asked) and close a route-owned connection."""
    if conn is None:
        return
    if rollback:
        try:
            conn.rollback()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass


def _command_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if isinstance(exc, WondrueAuthError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
    if "blocked in read-only mode" in message:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
    if "approval token" in message:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)
    if "unknown mode" in message or "unknown target" in message or "unknown intent" in message:
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
    if isinstance(exc, (DatabaseNotConfiguredError, DatabaseUnavailableError)):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable")
    if "requires a database connection" in message:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable")
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


@router.post("/wondrue/command")
def post_command(body: CommandRequest, request: Request) -> dict[str, Any]:
    """Interpret and optionally execute one Wondrue command (mode-gated).

    The caller authenticates with X-Wondrue-Key; the server-side grant
    caps the requested mode. A caller-supplied ``mode: full`` alone
    confers no authority.
    """
    grant = wondrue_grant(request)
    principal = "wondrue-operator" if grant == "full" else "wondrue-reader"
    conn = None
    if body.target == "real":
        try:
            conn = get_connection()
        except (DatabaseNotConfiguredError, DatabaseUnavailableError):
            # Mutating intents use audited wrappers (which fail closed
            # without a database); reads report unavailability below.
            conn = None
    try:
        result = handle_command(
            mode=body.mode,
            max_mode=grant,
            text=body.text,
            target=body.target,
            approval_token=body.approval_token,
            use_live=body.use_live,
            intent_override=body.intent,
            conn=conn,
            principal=principal,
        )
    except (
        WondrueError,
        ModelError,
        WorkflowError,
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        ValueError,
    ) as exc:
        _close_conn(conn, rollback=True)
        raise _command_error(exc)
    except HTTPException:
        _close_conn(conn, rollback=True)
        raise
    except Exception:
        _close_conn(conn, rollback=True)
        raise
    if conn is not None:
        # Commit only on success: a failed command must never leave
        # partial writes behind, and a failed commit must surface as
        # a persistence failure rather than a silent success.
        try:
            conn.commit()
        except Exception as exc:
            _close_conn(conn, rollback=True)
            raise HTTPException(
                status_code=503, detail="database_unavailable"
            ) from exc
        _close_conn(conn, rollback=False)
    return result


class ExtractRequest(BaseModel):
    mode: str = "read-only"
    approval_token: str | None = None
    use_live: bool = False
    doc_id: str = ""
    source_ref: str = ""
    pages: list[dict[str, Any]] = []
    run_id: str | None = None
    dossier_id: str | None = None
    dossier_key: str | None = None
    idempotency_key: str | None = None


@router.post("/wondrue/assist/extract")
def post_assist_extract(body: ExtractRequest, request: Request) -> dict[str, Any]:
    """Extract unverified claims from caller-supplied pages/images.

    Extraction is consequential: read-only callers are refused, ask
    mode returns a preview plus a single-use approval token, and full
    mode executes within the shared inference budget. Pages arrive
    inline; the route never fetches URLs or reads the filesystem.
    """
    from app import assist as assist_module
    from app.wondrue import APPROVALS, MODES

    grant = wondrue_grant(request)
    principal = "wondrue-operator" if grant == "full" else "wondrue-reader"
    if body.mode not in MODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown mode")
    if MODES.index(body.mode) > MODES.index(grant):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="mode exceeds grant")
    if body.mode == "read-only":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="intent 'extract_document' is blocked in read-only mode",
        )
    spec = {
        "doc_id": body.doc_id,
        "source_ref": body.source_ref,
        "pages": body.pages,
        "run_id": body.run_id,
        "dossier_id": body.dossier_id,
        "dossier_key": body.dossier_key,
        "idempotency_key": body.idempotency_key,
    }
    ctx = {
        "principal": principal,
        "use_live": body.use_live,
        "mode": body.mode,
        "approval_via_token": False,
        "demo": False,
    }
    # Bounded inputs are validated before any preview is issued; the
    # canonical spec (identities, page digests, association, live
    # selection, limits, idempotency key) is what approval binds to.
    try:
        validated = assist_module.validate_extraction_body(spec, ctx)
    except (WondrueError, ModelError, ValueError) as exc:
        raise _command_error(exc)
    canonical = validated["canonical"]
    approved_spec: dict[str, Any] | None = None
    if body.mode == "ask" and body.approval_token is None:
        token = APPROVALS.issue("extract_document", {"spec": canonical}, "real")
        return {
            "outcome": "preview",
            "intent": "extract_document",
            "target": "real",
            "preview": {
                "operations": ["validate supplied pages", "extract candidate claims (page-validated)"],
                "inputs": {
                    "doc_id": canonical["doc_id"],
                    "pages": [
                        {"page_no": page["page_no"], "mime": page["mime"]}
                        for page in canonical["pages"]
                    ],
                    "run_id": canonical["run_id"],
                    "dossier_id": canonical["dossier_id"],
                    "dossier_key": canonical["dossier_key"],
                    "use_live": canonical["use_live"],
                },
                "effects": "unverified claims plus attributed artifact; no dossier changes",
                "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
            },
            "approval_token": token,
            "model": {"mode": "none"},
        }
    if body.mode == "ask":
        assert body.approval_token is not None
        entry = APPROVALS.redeem(body.approval_token)
        if entry["intent"] != "extract_document" or entry["target"] != "real":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="approval token does not match this command",
            )
        approved_spec = entry["args"].get("spec")
        if not isinstance(approved_spec, dict):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="approval token does not match this command",
            )
        ctx["approval_via_token"] = True
    conn = None
    try:
        conn = get_connection()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        conn = None
    try:
        data = assist_module.extract_document(conn, spec, ctx, approved_spec)
    except (WondrueError, ModelError, WorkflowError, ValueError) as exc:
        _close_conn(conn, rollback=True)
        raise _command_error(exc)
    except HTTPException:
        _close_conn(conn, rollback=True)
        raise
    except Exception:
        _close_conn(conn, rollback=True)
        raise
    if conn is not None:
        try:
            conn.commit()
        except Exception as exc:
            _close_conn(conn, rollback=True)
            raise HTTPException(
                status_code=503, detail="database_unavailable"
            ) from exc
        _close_conn(conn, rollback=False)
    return {
        "outcome": "result",
        "intent": "extract_document",
        "target": "real",
        "data": data,
        "model": (data.get("model") or {"mode": "none"}),
    }


@router.post("/wondrue/demo/reset")
def post_demo_reset(request: Request) -> dict[str, Any]:
    """Reset only the isolated demo store (never touches real records).

    Reset mutates demo state, so it requires the operator grant; reader
    credentials and unauthenticated callers are refused even though real
    records are unreachable here.
    """
    if wondrue_grant(request) != "full":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="operator grant required"
        )
    DEMO.reset()
    return {"reset": True, "label": DEMO_LABEL}


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Wondrue &mdash; Grow Up MVP</title>
<style>
body {{ font-family: sans-serif; max-width: 72em; margin: 1em auto; padding: 0 1em; }}
.banner {{ padding: 0.6em 1em; border-radius: 6px; font-weight: bold; margin-bottom: 1em; }}
.banner.real {{ background: #fde8e8; color: #7a1f1f; border: 2px solid #7a1f1f; }}
.banner.demo {{ background: #efe6fb; color: #3d2470; border: 2px solid #3d2470; }}
.row {{ margin: 0.6em 0; }}
#output {{ white-space: pre-wrap; background: #f4f4f4; padding: 1em; border-radius: 6px;
  min-height: 12em; font-family: monospace; font-size: 0.9em; }}
.badge {{ display: inline-block; padding: 0.1em 0.6em; border-radius: 4px; font-size: 0.85em;
  font-weight: bold; margin-left: 0.5em; }}
.badge.live {{ background: #16700f; color: #fff; }}
.badge.replay {{ background: #8a6d00; color: #fff; }}
.badge.none {{ background: #666; color: #fff; }}
button {{ margin-right: 0.5em; }}
input[type=text] {{ width: 70%; }}
</style>
</head>
<body>
<h1>Wondrue &mdash; Grow Up MVP</h1>
<div id="modeBanner" class="banner demo">__DEMO_LABEL__</div>
<div class="row">
<label>Mode:
<select id="mode">
<option value="read-only">Read-Only</option>
<option value="ask" selected>Ask for Permission</option>
<option value="full">Full Access</option>
</select></label>
<label>Target:
<select id="target">
<option value="demo" selected>Synthetic demonstration</option>
<option value="real">Real-property evaluation</option>
</select></label>
<label><input type="checkbox" id="useLive"> Use live Meta inference</label>
<label>Key: <input type="password" id="apiKey" placeholder="X-Wondrue-Key" autocomplete="off"></label>
</div>
<div class="row">
<input type="text" id="command" placeholder="e.g. run the synthetic demo">
<button id="send">Send</button>
<button id="resetDemo">Reset demo</button>
</div>
<div class="row"><span id="modelBadge" class="badge none">model: none</span>
<span id="approvalBox"></span></div>
<div id="output" aria-live="polite">Results appear here.</div>
<script>
const out = document.getElementById("output");
const badge = document.getElementById("modelBadge");
const banner = document.getElementById("modeBanner");
const approvalBox = document.getElementById("approvalBox");
let pendingToken = null;
function show(obj) {{ out.textContent = JSON.stringify(obj, null, 2); }}
function setBadge(modeName) {{
  badge.textContent = "model: " + modeName;
  badge.className = "badge " + (modeName === "live" ? "live" : modeName === "replay" ? "replay" : "none");
}}
function refreshBanner() {{
  const target = document.getElementById("target").value;
  if (target === "real") {{
    banner.className = "banner real";
    banner.textContent = "REAL-PROPERTY EVALUATION \\u2014 genuine records only";
  }} else {{
    banner.className = "banner demo";
    banner.textContent = "__DEMO_LABEL__";
  }}
}}
document.getElementById("target").addEventListener("change", refreshBanner);
async function send(approvalToken) {{
  const body = {{
    mode: document.getElementById("mode").value,
    text: document.getElementById("command").value,
    target: document.getElementById("target").value,
    use_live: document.getElementById("useLive").checked,
    approval_token: approvalToken || null,
  }};
  const key = document.getElementById("apiKey").value;
  const resp = await fetch("/wondrue/command", {{
    method: "POST",
    headers: {{"Content-Type": "application/json", "X-Wondrue-Key": key}},
    body: JSON.stringify(body),
  }});
  const data = await resp.json();
  setBadge((data.model && data.model.mode) || "none");
  if (data.outcome === "preview") {{
    pendingToken = data.approval_token;
    approvalBox.innerHTML = "";
    const btn = document.createElement("button");
    btn.textContent = "Approve and execute";
    btn.addEventListener("click", () => send(pendingToken));
    approvalBox.appendChild(btn);
  }} else {{
    pendingToken = null;
    approvalBox.innerHTML = "";
  }}
  show(data);
}}
document.getElementById("send").addEventListener("click", () => send(null));
document.getElementById("resetDemo").addEventListener("click", async () => {{
  const key = document.getElementById("apiKey").value;
  const resp = await fetch("/wondrue/demo/reset", {{
    method: "POST", headers: {{"X-Wondrue-Key": key}},
  }});
  show(await resp.json());
}});
refreshBanner();
</script>
</body>
</html>
"""


@router.get("/wondrue", response_class=HTMLResponse)
def get_page() -> str:
    """Serve the compact Wondrue interface (static page; no user echo)."""
    return _PAGE.replace("__DEMO_LABEL__", html.escape(DEMO_LABEL))
