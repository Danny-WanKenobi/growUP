# GUADE operating instructions — November 2, 2026

Windows hosts development and GUADE execution. Git/GitHub transfers owner-approved revisions. Ubuntu verifies the deployed application and PostgreSQL. Do not install or authenticate an engineering CLI on Ubuntu for GUADE execution.

## History and authorization

The retained activation audit records completion at 2026-11-02T07:18:05Z: coach approval, Wonder exit 0 (93 events), passing validation, final coach approval. The trial artifact is committed. The activation ledger is exhausted: 3 dispatches, 6 coach calls, 7,066 reported tokens, 1 retry. Preserve guade/state/, state-operating/ and the historical overnight ledger. CLI mutation commands refuse these historical directories.

The runtime correction task authorizes local implementation and offline tests only. **No new live validation, campaign, provider spending, Git promotion or deployment is authorized.** Config comments and --approved-live acknowledge a grant; they cannot create owner permission. The new example envelopes are proposals. Do not recreate the activation.

## Bounded tasks and evidence

run-task accepts exactly one task; run-campaign processes a finite owner-reviewed list in order. It stops on the first non-completion, exhausted allowance, stop request, deadline or completed list. No generated follow-on work. Each JSON task requires id, title, prompt, exact allowed_files and tests (existing repository pytest files, never shell commands). See tasks.validation.example.json. Each task must address README synchronization in its prompt and allowlist, or justify no change in its report.

Only uppercase APPROVE, optionally followed by ' - reason', authorizes assignment dispatch. REVISE/STOP, ambiguous or malformed responses and provider failures cannot. Review receives actual bounded unified diffs, changed paths, before/after digests and test results. No file changes means no completion. Review REVISE may use the finite correction allowance; STOP ends execution.

Workspace snapshots include added, deleted and untracked files. Exact allowlists are checked before and after tests. Runtime controls and secret paths cannot be allowed. Unexpected edits stop execution and remain for owner inspection; no rollback discards work. Complete diff evidence must fit 48,000 UTF-8 bytes; binary/oversized evidence requires owner review. After engineering, the controller checks ledger/grant/mission integrity and unexpected audit-history changes.

**These are fail-closed completion checks, not an OS sandbox.** They cannot prevent writes outside the checkout or external actions through other tools. .git, virtual environments and Python/tool caches are excluded from inventory; active controller state is checked separately. Directory links are inventoried without following their targets; allowed files cannot use linked paths. Do not treat --disable-shell or prompts as OS permissions. Use an owner-reviewed workspace and keep production credentials/services outside the engineering environment. The audit is local evidence, not an external tamper-proof log.

## Accounting

Before sending, each coach call reserves serialized prompt UTF-8 bytes + 1,024 framing units + maximum completion tokens. These conservative local planning units are not an exact provider tokenizer. Valid reported total usage reconciles the reservation. Missing/malformed usage, failures and uncertain calls retain it. Usage exceeding the reservation records actual usage and blocks further calls pending owner reconciliation. Dispatch, call, retry and local token-unit limits are enforced.

baseten_budget_usd_cap is a legacy field name: it limits **estimated cumulative planning exposure**, not provider billing. Exposure uses reserved units times planning_usd_per_million_units (default 8.5, a provisional buffered assumption), with no refunds. It is not an invoice or verified live price. No provider-enforced dollar ceiling is installed here. Meta dollar spend is not tracked or enforced: dispatch/step limits cannot guarantee a dollar cap. Review provider account controls and reconcile usage before authorizing another run.

## Mission controls and interruption

A workspace-wide guade/mission.lock covers the whole task/campaign. Authorization is bound in authorization.json and tasks are retained in tasks.json. Policy/task-list changes refuse. The persisted deadline does not reset on restart; a monotonic bound also applies within the process. max_runtime_seconds cannot exceed 18,000. Five hours is a maximum; work stops earlier when the finite list completes or a limit is reached.

STOP/deadline checks occur before/after coach calls, before dispatch, during subprocess polling, before tests and between tasks. An in-flight synchronous HTTP call may take its configured timeout (capped to mission time remaining) to return; no next operation starts afterward. Windows interruption attempts taskkill /T /F process-tree termination. Unconfirmed cleanup retains the workspace lock for owner investigation. Hard process/host termination leaves a stale lock and running task. No automatic stale-lock removal or task replay is allowed. Confirm relevant processes stopped and reconcile ledger/provider outcomes before resolving a lock. Cleanup may need a short grace period beyond the deadline. Muse auto-update and interactive login are disabled in child processes; the controller installs nothing.

## Credentials and no-spend readiness

The existing loader reads BASETEN_API_KEY from process environment, Windows user environment, then GUADE_ENV_FILE / ~/.config/growup/guade.env / the readable Ubuntu shared file. Its existing file alias META_API_KEY uses the endpoint-binding rule. Readiness never prints keys. It does not directly read Windows Credential Manager. Presence is not live authentication.

From the Windows project root:

```powershell
Set-Location -LiteralPath 'C:\Code\growUP'
.\.venv\Scripts\python.exe -B -m guade.cli --state-dir guade/state-validation --config guade/config.validation.example.json check
```

check is read-only: no ledger creation, Muse execution, authentication or provider call. It reports configuration/headroom, not spending authority. Global options precede the subcommand.

## Prepared validation — separate approval required

The proposed task improves INSTALLATION.md and its README link, not the activation artifact. Review tasks.validation.example.json and config.validation.example.json before approving: one Wonder dispatch, two coach calls, zero correction retries, 100,000 local token units, $1 provisional planning exposure, 30 minutes maximum.

```powershell
Set-Location -LiteralPath 'C:\Code\growUP'
.\.venv\Scripts\python.exe -B -m guade.cli --state-dir guade/state-validation --config guade/config.validation.example.json run-task --tasks guade/tasks.validation.example.json --approved-live
if ($LASTEXITCODE -ne 0) { throw 'STOP: inspect the ledger; never reset or retry automatically.' }
git status --short
git diff --stat
```

## Prepared campaign — separate approval after validation

config.campaign.example.json proposes 18,000 seconds, 20 Wonder dispatches, 40 coach calls, 2 aggregate correction retries, 2,000,000 local token units and $50 Baseten provisional planning exposure. The separate $50 Meta target has no controller dollar enforcement. These are proposed finite ceilings, not permission. Approve the actual task list at guade/tasks.campaign.approved.json first; the runtime deliberately does not invent it. No Brave, outreach, Git or deployment authority is included.

```powershell
Set-Location -LiteralPath 'C:\Code\growUP'
if (-not (Test-Path -LiteralPath 'guade/tasks.campaign.approved.json')) { throw 'STOP: owner-reviewed campaign task list required.' }
.\.venv\Scripts\python.exe -B -m guade.cli --state-dir guade/state-campaign --config guade/config.campaign.example.json run-campaign --tasks guade/tasks.campaign.approved.json --approved-live
if ($LASTEXITCODE -ne 0) { throw 'STOP: inspect the ledger; never reset or retry automatically.' }
```

Emergency stop from another PowerShell window:

```powershell
Set-Content -LiteralPath 'C:\Code\growUP\guade\state-validation\STOP' -Value 'owner stop'
# For the separately approved campaign instead:
# Set-Content -LiteralPath 'C:\Code\growUP\guade\state-campaign\STOP' -Value 'owner stop'
```

Inspect ledger.json, audit.jsonl, authorization.json, tasks.json and mission.json in that state directory. Do not reset spent ledgers, remove STOP or increase limits to bypass reconciliation.

## Offline regression

Provider transports and engineering subprocesses are faked; one harmless local Python sleeper separately verifies real Windows timeout/tree cleanup. Temporary workspaces exercise changes, interruption and diff evidence.

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_guade_runtime.py tests/test_guade_mission.py tests/test_guade_credentials.py tests/test_guade_safety.py tests/test_discovery_precedence.py -q -p no:cacheprovider
```

These tests do not replace live compatibility validation or the owner-controlled deployed Ubuntu/PostgreSQL gate. Daily windows, quota recovery, automatic Git promotion and autonomous deployment are not implemented.
