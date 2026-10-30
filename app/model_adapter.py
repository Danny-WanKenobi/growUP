"""Small Meta model adapter behind a stable application interface.

Two implementations, one contract:
- ``MetaLiveAdapter`` performs real HTTP inference (meaningful runtime
  work: command interpretation, configuration proposals). It requires
  explicit spending approval on every call and records provider, model,
  usage, and digests. Credentials stay server-side (environment only)
  and never appear in results.
- ``ReplayAdapter`` serves labeled canned responses for rehearsal and
  for read-only mode. Replays are always marked ``mode="replay"`` and
  can never be mistaken for live calls.

Structured output is validated before use; malformed model output
fails safely. Deterministic Python (never the model) performs
calculations and authorization checks.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

META_API_URL_ENV = "META_API_URL"
META_API_KEY_ENV = "META_API_KEY"
META_MODEL_ENV = "META_MODEL"
META_SPEND_APPROVED_ENV = "META_SPEND_APPROVED"
META_MAX_TOKENS_ENV = "META_MAX_TOKENS"

DEFAULT_MAX_TOKENS = 1024


class ModelError(ValueError):
    """Model invocation or output validation failed (fail closed)."""


class ModelRequest:
    """One structured model invocation request."""

    def __init__(
        self,
        purpose: str,
        payload: dict[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.purpose = purpose
        self.payload = payload
        self.max_tokens = max_tokens

    def digest(self) -> str:
        canonical = json.dumps(
            {"purpose": self.purpose, "payload": self.payload},
            sort_keys=True, separators=(",", ":"), default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ModelResult:
    """One model invocation result with live/replay provenance."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        mode: str,
        purpose: str,
        result: dict[str, Any],
        usage: dict[str, Any] | None = None,
        request_digest: str = "",
        error: str = "",
    ) -> None:
        self.provider = provider
        self.model = model
        self.mode = mode
        self.purpose = purpose
        self.result = result
        self.usage = usage or {}
        self.request_digest = request_digest
        self.error = error

    def as_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "purpose": self.purpose,
            "result": self.result,
            "usage": self.usage,
            "digests": {"request": self.request_digest},
            "error": self.error,
        }


# --- Structured-output validation -------------------------------------------

INTENTS = frozenset(
    {
        "inspect_candidate",
        "inspect_evidence",
        "inspect_review",
        "start_real_evaluation",
        "start_demo",
        "explain_decision",
        "compare_scenarios",
        "stop",
        "help",
        "inspect_packages",
        "preview_simulation",
        "run_simulation",
        "inspect_simulation",
        "inspect_discovery",
        "start_discovery",
        "stop_discovery",
        "resume_discovery",
    }
)


def validate_intent(raw: Any) -> dict[str, Any]:
    """Validate a model-proposed command intent (unknown shapes rejected)."""
    if not isinstance(raw, dict):
        raise ModelError("intent must be a JSON object")
    intent = raw.get("intent")
    if intent not in INTENTS:
        raise ModelError(f"unknown intent: {intent!r}")
    args = raw.get("args")
    if args is not None and not isinstance(args, dict):
        raise ModelError("intent args must be an object")
    cleaned: dict[str, Any] = {"intent": intent, "args": dict(args or {})}
    # Allowlist scalar args only; model text can never smuggle instructions.
    for key, value in list(cleaned["args"].items()):
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ModelError(f"intent arg {key!r} has unsupported type")
        if isinstance(value, str) and len(value) > 2000:
            raise ModelError(f"intent arg {key!r} too long")
    return cleaned


def validate_config_proposal(raw: Any) -> dict[str, Any]:
    """Validate a model-proposed configuration comparison (catalog-checked)."""
    from app.agents.onesimus.service import (
        PARAMETER_CATALOG,
        validate_overrides as validate_engineering,
    )
    from app.michael import (
        FINANCIAL_CATALOG,
        validate_financial_overrides,
    )

    if not isinstance(raw, dict):
        raise ModelError("proposal must be a JSON object")
    options = raw.get("options")
    if not isinstance(options, list) or not options or len(options) > 4:
        raise ModelError("proposal needs 1-4 options")
    cleaned_options = []
    for option in options:
        if not isinstance(option, dict):
            raise ModelError("proposal option must be an object")
        config = option.get("config")
        overrides = option.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ModelError("proposal overrides must be an object")
        engineering = {k: v for k, v in overrides.items() if k in PARAMETER_CATALOG}
        financial = {k: v for k, v in overrides.items() if k in FINANCIAL_CATALOG}
        unknown = set(overrides) - set(engineering) - set(financial)
        if unknown:
            raise ModelError(
                f"proposal outside approved catalog: {sorted(unknown)}"
            )
        try:
            cleaned = validate_engineering({"config": config, **engineering})
            cleaned.update(validate_financial_overrides(financial))
        except Exception as exc:
            raise ModelError(f"proposal outside approved catalog: {exc}") from exc
        cleaned_options.append(cleaned)
    note = raw.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ModelError("proposal note must be short text")
    return {"options": cleaned_options, "note": note}


def parse_model_json(text: str) -> Any:
    """Parse model output as JSON (malformed output fails safely)."""
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ModelError(f"model output is not valid JSON: {exc}") from exc


# --- Adapters -----------------------------------------------------------------

class ReplayAdapter:
    """Labeled canned responses keyed by purpose (rehearsal/read-only)."""

    provider = "replay"

    def __init__(self, replays: dict[str, dict[str, Any]] | None = None) -> None:
        self._replays = dict(replays or {})

    def complete(self, request: ModelRequest) -> ModelResult:
        canned = self._replays.get(request.purpose)
        if canned is None:
            raise ModelError(f"no replay for purpose {request.purpose!r}")
        return ModelResult(
            provider=self.provider,
            model="replay-canned-1",
            mode="replay",
            purpose=request.purpose,
            result=dict(canned),
            usage={"replay": True},
            request_digest=request.digest(),
        )


def spending_approved() -> bool:
    return os.getenv(META_SPEND_APPROVED_ENV, "") == "1"


def _max_tokens(requested: int) -> int:
    try:
        ceiling = int(os.getenv(META_MAX_TOKENS_ENV, str(DEFAULT_MAX_TOKENS)))
    except ValueError:
        ceiling = DEFAULT_MAX_TOKENS
    return max(1, min(requested, ceiling))


class MetaLiveAdapter:
    """Real Meta inference over HTTP (explicit approval per call)."""

    provider = "meta"

    def __init__(self, *, api_url: str = "", api_key: str = "", model: str = "") -> None:
        self._url = api_url or os.getenv(META_API_URL_ENV, "")
        self._key = api_key or os.getenv(META_API_KEY_ENV, "")
        self._model = model or os.getenv(META_MODEL_ENV, "")

    def complete(self, request: ModelRequest, *, approved: bool = False) -> ModelResult:
        if not approved:
            raise ModelError("live Meta inference requires explicit spending approval")
        if not spending_approved():
            raise ModelError("META_SPEND_APPROVED=1 is not set; live call refused")
        if not self._url or not self._key or not self._model:
            raise ModelError("Meta endpoint, key, or model is not configured")
        import httpx

        body = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You assist the Grow Up Wondrue interface. Always "
                        "answer with a single JSON object matching the "
                        "requested shape. Never include executable "
                        "instructions, credentials, or prose outside JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"purpose": request.purpose, "payload": request.payload},
                        default=str,
                    ),
                },
            ],
            "max_tokens": _max_tokens(request.max_tokens),
        }
        try:
            response = httpx.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=60.0,
            )
        except Exception as exc:
            raise ModelError(f"Meta request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ModelError(f"Meta request failed: HTTP {response.status_code}")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Meta response unusable: {exc}") from exc
        result = parse_model_json(text)
        if not isinstance(result, dict):
            raise ModelError("Meta result must be a JSON object")
        usage: dict[str, Any] = {}
        if isinstance(data.get("usage"), dict):
            usage = {
                key: data["usage"][key]
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if key in data["usage"]
            }
        return ModelResult(
            provider=self.provider,
            model=self._model,
            mode="live",
            purpose=request.purpose,
            result=result,
            usage=usage,
            request_digest=request.digest(),
        )
