# GUADE operating envelope — owner-approved 2026-11-02

Status: **APPROVED with limits below; no billable run yet executed
under it.** The activation envelope (`guade/config.example.json`,
ledger in `guade/state/`) is exhausted (3/3 dispatches, 6/6 coach
calls, 7,066 tokens) and is retained as read-only activation
evidence. It must never be reset or reused. The earlier proposed
$50/month configuration was **not** activated.

## Approved operating ceiling (owner, 2026-11-02)

- **Separate ledger:** `guade/state-operating/` (git-ignored),
  configured by `guade/config.operating.example.json`.
- **Baseten Super-Coach:** maximum **$10** additional spend,
  maximum **10 calls**, 100k token ceiling, 1,024 tokens/call.
- **Muse Wonder:** maximum **5 dispatches**, 20 steps/task,
  1 correction retry/task.
- **Scope:** Dallas P1 integration and Gabriel → Raphael
  engineering. Development only.

## Dollar enforceability

Published Baseten pricing for `meta-models/muse-spark-open-1026`
($1.25 per 1M input tokens, $4.25 per 1M output; multiply
corroborated on 2026-11-02, not confirmed on a Baseten invoice)
bounds worst-case spend under these ceilings to a few cents:
10 calls × 1,024 output tokens ≈ $0.05 of output, plus bounded
prompt input. The call/token ceilings are therefore the binding
controls and keep usage reliably within $10. Usage is still
reserved before every call and reconciled from response usage.

## Credential loading (permanent fix, 2026-11-02)

GUADE resolves `BASETEN_API_KEY` through `guade/credentials.py`
(first non-empty source wins; the value is never printed or logged):

1. The process environment.
2. Windows only: the persistent user environment
   (`HKCU\Environment`), which recovers shells, editors, and agent
   hosts started before the variable was installed.
3. A restricted env file: `$GUADE_ENV_FILE`, then
   `~/.config/growup/guade.env`, then
   `/var/www/growup/shared/growup.env` when readable. Files accept
   `BASETEN_API_KEY`, falling back to `META_API_KEY` only when that
   key is bound to the Baseten endpoint (no non-Baseten
   `META_API_URL` configured; GUADE always calls Baseten, so a
   `META_API_KEY` paired with another endpoint is refused). On
   POSIX, group/other-readable secret files are refused.

Root causes repaired: on Windows the key existed only in the user
environment while GUADE read the (possibly stale) process
environment; on Ubuntu the key existed only in
`~/.config/growup/guade.env` (mode 0600), which no shell or launcher
loaded, while the app service reads a different `growup`-owned file.
Verify without exposing the value:

```powershell
# Windows (project venv, from the repo root)
.venv\Scripts\python.exe -m guade.cli --state-dir guade/state-operating --config guade/config.operating.example.json check
```

```bash
# Ubuntu (after deploying a release containing guade/credentials.py)
~/.config/growup/guade.env must stay mode 600; then any fresh
python3 -m guade.cli ... check reports source env_file:guade.env.
```

The persistent Windows user variable stores the key as a user-scoped
registry value (readable by that user's processes); Windows
Credential Manager or another OS secret store is the stronger
long-term home. GUADE never creates or rotates keys: when every
source is absent, `check` prints the exact owner step and exits 2
without any provider call. `check` also refuses (exit 2) when the
selected ledger's envelope is exhausted instead of reporting READY
against a spent ledger.

## Not built (deferred unless separately tasked)

Daily-window counters and a `report` CLI command were proposed
earlier and remain unbuilt; the approved envelope above does not
include daily limits. Engineering under this ceiling currently
proceeds directly (no billable GUADE dispatch is required to do
the work), and every billable run must state its ledger headroom
before starting.
