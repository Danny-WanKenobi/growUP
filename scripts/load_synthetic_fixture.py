"""SYNTHETIC fixture handling under the MVP-002 evidence policy.

EVIDENCE POLICY: hypothetical values are barred from operational
property records. The synthetic Dallas fixture is invented demonstration
data (``data_status: hypothetical``), so this script REFUSES to insert
it into the operational database. The fixture remains available for
isolated automated testing only -- tests that read the JSON without
touching the operational store.

Usage (from the repository root)::

    python scripts/load_synthetic_fixture.py   # always refuses; explains why
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_dallas_property.json"


def refusal_reason(payload: dict[str, Any]) -> str | None:
    """Return why this payload must not enter the operational store, or None."""
    status = str(payload.get("data_status", "")).strip().lower()
    if status == "hypothetical":
        return "data_status is 'hypothetical'; hypothetical values are barred from operational records"
    if status not in ("verified", "estimated", "mixed", "unknown"):
        return (
            f"data_status is {status or 'missing'!r}; operational records accept "
            "verified, estimated, mixed, or unknown only"
        )
    provenance = str(payload.get("provenance", "")).strip().lower()
    if not provenance:
        return "missing provenance; operational records require traceable evidence"
    for marker in ("synthetic", "hypothetical", "fictional", "invented", "fixture"):
        if marker in provenance:
            return (
                f"provenance is marked {marker!r}; synthetic or hypothetical data "
                "must never enter the operational property database"
            )
    return None


def main() -> int:
    try:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read fixture: {exc}", file=sys.stderr)
        return 1
    payload.pop("_label", None)
    reason = refusal_reason(payload)
    if reason is not None:
        print(
            "refused: synthetic fixture must not enter the operational database "
            f"(hypothetical values barred): {reason}",
            file=sys.stderr,
        )
        return 1
    # Unreachable for the shipped synthetic fixture; kept as a loud backstop
    # so a future verified payload can never be inserted silently by this path.
    print(
        "refused: this loader handles the synthetic demonstration fixture only, "
        "which is barred from the operational database by policy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
