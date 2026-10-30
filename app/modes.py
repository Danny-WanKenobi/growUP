"""Global run-mode switches.

``DEMO_MODE=yes`` means prepare and analyze using real data, but
simulate downstream external execution. It affects Michael-stage and
later external behavior only:

- Gabriel, Raphael, and Onesimus use genuine evidence and unchanged
  verification rules in both modes; they never read this switch.
- Michael's deterministic financial analysis is genuine in both modes.
- Uriel prepares genuine campaign packages in both modes; no sending,
  scheduling, or other consequential external action exists yet.
- Priscilla may record explicitly simulated delivery, engagement,
  response, and conversion events only with ``yes``. Simulated events
  never enter actual totals.

Genuine packages are never labeled synthetic because of demo mode.
Switching to ``no`` replays nothing and sends nothing: simulated rows
stay simulated, and real execution additionally requires implemented
providers, authorized scope, and spending approval where applicable.
The isolated synthetic upstream harness (``app/demo.py``) is a
separately selected diagnostic fallback, independent of ``DEMO_MODE``.

Unset or blank means ``yes``. Any other value is rejected loudly so a
typo can never silently pick a mode.
"""

from __future__ import annotations

import os

DEMO_MODE_ENV = "DEMO_MODE"


def demo_mode_enabled() -> bool:
    """True unless ``DEMO_MODE=no`` is explicitly configured."""
    raw = os.getenv(DEMO_MODE_ENV, "yes").strip().lower()
    if raw in ("", "yes"):
        return True
    if raw == "no":
        return False
    raise ValueError(f"invalid {DEMO_MODE_ENV}={raw!r} (expected yes/no)")
