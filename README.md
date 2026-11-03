# 🌱 Grow Up

## Permanent synthetic canonical-handoff demonstration (November 3, 2026)

The owner approved fictional test records to prove the canonical handoff system
without waiting for a real property to qualify. This is a permanent, explicitly
isolated demonstration facility, not an exception that lets synthetic evidence
enter the real pipeline. All fourteen operational roles share **one** demo
database and the same canonical custody implementation:

Gabriel → Raphael → Onesimus → Michael → Uriel → Priscilla → Nicole →
Theresia Keys → Simeon → Solomon → Elmer → Yoseph → Deborah → Dan.

`app/handoff_demo.py` generates one fully labeled GO scenario and a distinct
missing-input NO-GO scenario for every checkpoint. Successful reports are attached
to full inherited copies of the same canonical dossier. Each forward transfer
uses `app/custody_db.py` and its real version, snapshot, latest-holder, HMAC and
predecessor-chain rules. A rejection stays at its checkpoint, then returns **one
agent at a time** through the intervening stages to Gabriel, preserving its reason
and the enhanced dossier. The Dan NO-GO case proves the entire reverse route.
Gabriel's own NO-GO has no upstream recipient and terminates at Gabriel.

The fixture suite has **15 scenarios**, **195 verified receipts** in total:
13 forward receipts for the all-GO case, plus the forward/return paths for all
14 rejection cases. IDs, stored records, reports, decisions, viewer responses and
exports are explicitly synthetic. Fictional sources use `fiction.example` and
are never fetched. The isolated fixture registry never modifies the production
source registry. Raphael reuses the shared verification/evaluation policy over
fictional fixture evidence; Onesimus and Michael reuse their deterministic
calculations. The ten version-one downstream roles reuse their implemented
report/intake checks. Their GO means **synthetic intake acceptance**, not a permit,
legal opinion, signed contract, financing approval, insurance coverage, actual
lead conversion, construction authorization or asset ownership.

### Isolation and durable contracts

The only permitted target is PostgreSQL **`growup_handoff_demo`**, role
**`growup_demo`**, configured exclusively through `HANDOFF_DEMO_DATABASE_*`.
No operational `DATABASE_*` credential fallback exists. Real connections must
confirm their actual database and role before any demonstration writes. A separate
`HANDOFF_DEMO_HMAC_KEY` signs receipts in an exception-safe context local to that
request; it never replaces the operational signing key or leaks into another
request. Both the demo entrypoints and shared synthetic payload write guards
refuse an operational database. Internal UUIDs use the normal canonical identity
algorithm inside the isolated database; public run keys begin `demo-handoff-`.
There is no automatic promotion/import into operational property records.

All agent state, versions, snapshots, receipts and permanent audit events stay in
this single demo database. No agent has its own store. The existing append-only
custody tables protect history. Progress and rejection reasons survive refresh,
process restart and interruption; a sequence-bound retry does not repeat a
handoff. Mutations and their permanent events commit together, and a failed step
rolls back. Start replay refuses changed scenarios; concurrent steps serialize
on the shared run/dossier locks. Resetting the old in-memory demo does not delete
this database or its records. No reset/delete command is installed.

### Owner-controlled installation and proof

First use the normal owner-controlled Git → GitHub → development deployment
checkpoint and run the Ubuntu regression suites. Nothing is deployed or provisioned
by local implementation tests. The reviewed setup script, when explicitly run
with `--apply` on **securecode-dev**, creates the restricted demo role/database,
keeps generated secrets in root-owned mode-0600
`/var/www/growup/shared/handoff-demo.env`, initializes the **demo database only**
with the existing migrations, and adds a service environment-file drop-in.
It preserves existing credentials and refuses role/ownership/configuration
conflicts. Setup failure diagnostics redact the dedicated password and signing
key. The migration runner now accepts an explicit connection configuration; demo
initialization never falls back to the operational environment. No operational schema migration is added. Enabling the drop-in restarts
`growup.service` and checks readiness; no scenarios automatically launch unless `--prove` is supplied.

After deployment, the owner can provision the demo and run the complete bounded
PostgreSQL proof in one Ubuntu block:

```bash
bash <<'BASH'
set -Eeuo pipefail
trap 'echo "STOP: synthetic demo setup/proof failed; preserve output and records." >&2' ERR
sudo -v
release_dir="$(sudo -u growup readlink -f /var/www/growup/current)"
[[ "$release_dir" == /var/www/growup/releases/* ]]
sudo /usr/bin/python3 "$release_dir/deployment/setup-handoff-demo.py" \
  --release "$release_dir" --apply
proof_key="demo-handoff-owner-$(date -u +%Y%m%dT%H%M%SZ)"
sudo systemd-run --wait --pipe --collect --quiet \
  -p User=growup -p Group=growup \
  -p "WorkingDirectory=$release_dir" \
  -p EnvironmentFile=/var/www/growup/shared/handoff-demo.env \
  /usr/bin/env PYTHONDONTWRITEBYTECODE=1 OBS_ENABLED=0 \
  "$release_dir/.venv/bin/python" -B -m app.handoff_demo suite \
  --run-key "$proof_key" \
  | tee "/home/ubuntu/${proof_key}.json"
echo "SYNTHETIC POSTGRESQL PROOF COMPLETE: $proof_key"
echo "No discovery, provider spending, operational property writes or external execution."
BASH
```

For a single-console setup and proof, add **`--prove`** to the setup command.
It runs all 15 bounded cases, validates the synthetic PASS response, and archives
a sanitized mode-0600 JSON proof owned by `ubuntu` under
`/home/ubuntu/growup-handoff-proof-*.json`. It does not disclose credentials.
Existing partial setup state is preserved; do not reset it after an init failure.

**First dedicated PostgreSQL execution and browser visual acceptance are pending**
until the owner performs that checkpoint. Local success does not claim the demo
DB/role/drop-in has already been installed or that its PostgreSQL proof passed.
The prior owner health check passed on release `fdad42a`; skipped-test investigation
and genuine-property preview remain separately deferred.

### Observable product demonstration

Open **`https://securecodeusa.local:8443/wondrue/handoff-demo`** after installation.
Enter the existing Wondrue operator key, choose all-GO or a specific NO-GO stage,
and press **New scenario**. **Advance one checkpoint** demonstrates each receipt;
**Play to completion** sends a bounded series of internal steps. **Stop playback**
prevents further requests after the current request finishes. **Inspect stored
run** loads existing progress without a write, including after refresh/restart.
The timeline shows the agent, GO/NO-GO, recipient, dossier version and rejection
reason. Details retain all reports and label limitations. The access key is never
saved in browser storage or included in run records/URLs. No live-inference control
is offered on this page.

The existing Wondrue server-side reader/operator grants still apply. Structured
commands require `target: "demo"`, `use_live: false`:

- `start_handoff_demo`: `run_key`, optional `fail_at`; mutation, bounded fixture
  creation only.
- `advance_handoff_demo`: `run_key`, `expected_sequence`; one mutation/step.
- `inspect_handoff_demo`: `run_key`; read-only, verifies the stored chain.

Ask-mode approval tokens bind the operator and exact stored arguments; reader mode
cannot mutate. The CLI also supports `init`, `start`, `advance --sequence N`,
`inspect`, bounded `run`, and the 15-scenario `suite`, all through the same isolated
service. No additional scheduler, GUADE worker or discovery engine is introduced.
The older in-memory `app/demo.py` remains compatible and is not mistaken for this
persistent PostgreSQL proof.

### Checks and scope of evidence

Windows offline checks exercise the **actual canonical DAO/report functions**
against a disposable SQL test double, including all 15 scenarios, exact reverse
routing, immutable-copy preservation, replay, sequence limits, wrong DB/role
refusal, signing-key scope restoration, tamper rejection and authenticated viewer
commands. The durable PostgreSQL proof is a separate owner-run acceptance step.
A sanitized offline proof artifact is generated at ignored
`tmp/handoff-demo-proof.json`; it explicitly identifies the offline test-double
backend. It is not evidence of real-world property verification. No real outreach,
professional approval or conversion is claimed anywhere in this milestone.

Validation: **2,544 passed, 149 skipped, 16 warnings**, zero failures in the
Windows offline suite. The focused canonical/demo regression passed **98 tests**
with five PostgreSQL-gated skips before the final suite-orchestration check was
added. Python syntax, extracted viewer JavaScript syntax and `git diff --check`
passed. No database setup, operational migration, deployment or provider call
was performed during implementation.

Initialization correction: **2,546 passed, 149 skipped, 16 warnings** in the
Windows offline suite. Added a regression exercising the actual migration-runner
interface with explicit demo configuration and no operational fallback, plus
credential-redaction checks for setup failures. The first owner setup stopped
before successful schema initialization; preserve its credentials and rerun the
corrected setup rather than resetting the demo database.

## Downstream agent foundations, version one (November 3, 2026)

Grow Up is a **single-owner autonomous system**, not a multi-user product. Normal
internal work is intended to proceed under configured backend permissions without
repeated human prompts. Unresolved blockers, edge cases, emergencies and sensitive
decisions belong in the human owner's review queue. Wondrue remains the sole
operational interface. Dan is a software owner-representative/asset role; he is
not the human owner and cannot grant signing, spending or execution authority.
A future power of attorney needs a separate authenticated authority integration;
its planned existence is not present permission to sign documents.

These are **version-one deterministic preparation capabilities**, with explicit
runtime instructions and input/output contracts, not newly trained models or
claims of professional domain competence. Definitions live in
`app/agents/registry.py`; the shared preparation implementation is
`app/agents/stages.py`. No additional discovery/verification engine or database
has been created. Existing Uriel (`app/uriel.py`) and Priscilla (`app/priscilla.py`)
implementations remain the package/event authorities.

| Agent / stable ID | Useful first-version output | Work That Can Wait / deferred |
| --- | --- | --- |
| Uriel / `uriel` | Opportunity intake, inherited Michael assessments, evidence-linked factual draft brief; existing package preparation retained | Automated contact research/ranking, campaign design and authenticated delivery with suppression, consent and rate-limit enforcement |
| Priscilla / `priscilla` | Tracking intake; existing append-only events and separately labeled actual/simulated metrics, readable by Wondrue | Inbound webhooks, reply correlation, richer bounced/replied/interested states and full property timeline UI |
| Nicole / `nicole` | Jurisdiction, proposed-use and official-requirement intake checklist | Current permit/code/license research, authenticated jurisdiction adapters, applications and submissions |
| Theresia Keys / `theresia_keys` | Ownership/lien, finance-term and contract-preparation checklist | Title/lien verification, underwriting rules, final instruments and signing |
| Simeon / `simeon` | Consent, terms, signing-authority and closing-conditions checklist | Negotiations, closing channels, verified power of attorney and execution |
| Solomon / `solomon` | Preconstruction design, permit, workforce/safety and budget prerequisites | Contractor/robot procurement, scheduling, site work, supervision and maintenance |
| Elmer / `elmer` | Period, transaction, entity/jurisdiction and reconciliation intake | Double-entry bookkeeping, bank feeds, tax calculations, filings and payments |
| Yoseph / `yoseph` | Risk, policy and coverage-requirement intake | Carrier quotes, policy interpretation, coverage validation, binding and claims |
| Deborah / `deborah` | Legal-question, jurisdiction and governing-document issue register | Current-law research, supported legal opinions, filings and representation |
| Dan / `dan` | Ownership, operating-mandate and asset-obligation intake | Verified asset register, performance/maintenance automation and delegated transactions |

### Canonical handoff contract from the outset

Every role definition includes the same operational instructions: keep the
existing property UUID/dossier identity; load the latest inherited full dossier;
verify its digest, custody chain and current holder; append only the role's report
to a new full version; create the normal immutable snapshot; use the existing
signed `forward`, `return` or `review` receipt service. No agent creates its own
research dossier, pipeline, log or independent custody mechanism.

The existing custody registry, role interpretation and shared event AgentId enum
now recognize all eight additional roles. Existing `mvp-1`/`canonical-1` receipt
meanings, MAC format and historical records are unchanged. Review routing extends
after Priscilla in the listed order. This is an **internal review route**, not a
construction/closing execution schedule: Nicole, Deborah, Yoseph and other
relevant reviews must be resolved before any future consequential operation.
Returns and human-review escalation reuse the existing service. No handoff has
been performed for a new role and no real dossier has yet demonstrated this
expanded route.

`app.agents.stages.prepare_audited` is the common, currently uninvoked worker
entrypoint. With workflow enabled and an authenticated worker/operator principal,
it requires the latest shared dossier and the **actual agent as current custody
holder**, then appends `agent-intake-report-1` through `app/dossier_reports.py`.
A repeat returns the existing report; conflicting or corrupt reports refuse.
It never automatically forwards custody, creates a property, generates demo data,
starts a worker, fetches a source or invokes a model. Future workers must call the
existing custody service when a permissible internal transfer is appropriate.

Inputs come only from stored inherited reports and
`dossier.specialist_inputs[agent_id][input_name]`, where each entry carries
`value` and `evidence_refs`. Version one records them as **provided_unverified**;
a source reference alone is not authentication or an agent approval. Missing or
invalid input yields `needs_evidence`; a complete intake is
`prepared_not_approved`. Neither status establishes legal compliance, permitted
use, financing, contract enforceability, a closed deal, tax liability, coverage or
ownership. Uriel's brief copies Michael's stored conditional assessments and
never invents a profit claim. Priscilla never infers engagement from preparation.

When that worker entrypoint is eventually invoked, unresolved intake items become
idempotent `owner_review_task` workflow records, bound to the property, source
dossier and report digests, in the same audited transaction as the report.
These are durable pending requests, not permission grants. Resolution automation
and a mutation/approval UI are deferred. No pending review tasks were created by
this implementation. Existing schemaless stores suffice; no migration or new
secret/dependency is required.

### Wondrue inspection commands and activation boundary

Use structured commands on the existing authenticated `/wondrue/command` route
with `mode: "read-only"`, `target: "real"`, `use_live: false`:

- `inspect_agents`, empty args: lists all ten roles, handoff instructions,
  required inputs/reports, current capabilities and deferred work. No database is
  required for this roster.
- `preview_agent_report`, args `run_id`, `dossier_key`, `agent`: reads the current
  stored dossier and previews a role's intake report without creating reports,
  tasks, receipts or sample data. This command has not been run on a real dossier.
- `inspect_owner_queue`, arg `run_id`: reads digest-checked pending owner tasks.
- `inspect_lead_metrics`, optional arg `package_entry`: reuses Priscilla's existing
  actual/simulated event counts. Empty history stays empty; no engagement is
  invented and no new simulation is run.

There is **no new public mutation command or background dispatcher** for these
agents in version one. Domain experts, external-action adapters and autonomous
execution workers are deferred; the callable preparation/queue primitives are
ready for a later bounded activation. Live-mode outreach autonomy remains the
product goal under standing channel permissions, authenticated delivery,
compliance/suppression checks and limits; no sender is enabled merely by setting
Live Mode. Demo-mode sends/conversions remain explicitly simulated by the existing
separate mechanism, and that mechanism has not been invoked here.

**Initial agent-foundation checkpoint boundary (superseded by the synthetic
demo section above):** discovery remains paused. The owner subsequently passed
database/application health checks; skipped-test investigation and the real
stored-dossier preview remain deferred. No new genuine or synthetic
business dossier, conversion, custody receipt or demonstration data was created.
Only contract/pure routing checks and the existing offline regression are run for
this task; no new-agent handoff proof is claimed. Owner Git/deployment and Ubuntu
validation remain separate checkpoints. No GUADE changes are included.

Version-one validation: **2,532 passed, 149 skipped, 16 warnings**, zero
failures in the Windows offline suite; focused role/custody/Wondrue/Uriel/Priscilla
checks: **115 passed, 4 skipped**. Syntax and whitespace checks passed. The new
role checks are contract-only and create no sample property or custody receipt.

## Stored-evidence assessment through Michael (November 3, 2026)

This local implementation adds an **offline reassessment** of an existing genuine
property; it does not run discovery, fetch evidence, relax source authentication,
or enable inference/outreach. `app/retained_assessment.py` consumes an existing
collector-minted DCAD account page, its original transmission claim, and its
property-bound assessment. The collection, claim, assessment, original research
artifacts and latest dossier/custody digests must agree. The page's actual retained
bytes are parsed again and evaluated by the existing shared verification policy.
A bare discovery row, uploaded HTML, filename, publisher name or supplied verdict
cannot substitute for those records. Acquisition enablement and account
allowlisting are not required to **read already collected evidence**; collection
itself retains its existing gates. No source registry enrollment is added.

The same canonical property/dossier continues immediately when Raphael authorizes
it. There is no batch, county quota or source-completion wait. An exact normalized
street binding, unchanged city/state, original property-bound collection assessment
and any retained original DCAD account binding prevent evidence from another
property being substituted. Mixed or contradictory classes remain unresolved or
rejected under the shared policy; missing evidence still produces needs-evidence.
DCAD's scope is recorded appraisal classification/account identity, **not** current
vacancy, structural/electrical capacity, agricultural permission or profitability.

### Read preview, then approve the exact append

Wondrue provides two structured intents on the existing `/wondrue/command` route:

- `preview_retained_assessment`: read-only, target `real`; accepts
  `parent_run_id`, `dossier_key`, `collection_run_id`, and optional JSON-string
  `site_inputs` / `financial_inputs`. Returns the rebuilt dossier, computed
  decision, inspection preview, evidence locators, current custody, zero external
  request bounds and `preview_digest`. No database writes or transport calls.
- `commit_retained_assessment`: operator mutation, target `real`, accepts those
  **identical** inputs plus `preview_digest` and a nonempty `reason`. The server's
  authenticated principal must already be enrolled in existing `CUSTODY_AMENDERS`;
  no enrollment or secret is changed by this command. Ask mode previews the append
  and binds its approval token to the operator. The commit rechecks the preview
  under the canonical dossier lock. Changed evidence, inputs or custody require a
  fresh preview. Business changes and a permanent event commit atomically through
  the shared audited transaction. Exact replay returns the saved result without
  new dossier versions or agent execution; amendment authority is checked again.

After owner deployment/validation, a read-only example (replace identifiers and
provide the existing Wondrue credential in `$headers`; never commit credentials):

```powershell
$assessmentArgs = @{
    parent_run_id = '<latest stored evaluation UUID>'
    dossier_key = 'main'
    collection_run_id = '<stored DCAD collection evaluation UUID>'
    site_inputs = '{}'
    financial_inputs = '{}'
}
$previewBody = @{
    mode = 'read-only'; target = 'real'; use_live = $false
    intent = @{ intent = 'preview_retained_assessment'; args = $assessmentArgs }
}
$preview = Invoke-RestMethod -Method Post `
    -Uri 'https://securecodeusa.local:8443/wondrue/command' `
    -Headers $headers -ContentType 'application/json' `
    -Body ($previewBody | ConvertTo-Json -Depth 12)
$preview.data.assessment_preview
```

This example does **not** append. Only after reviewing that result and authorizing
the database write, submit `commit_retained_assessment` with the same arguments,
its returned `preview_digest`, a reason and the appropriate operator mode. These
instructions do not authorize a live append or deployment.

### Reports, assumptions and incomplete results

The append preserves the original workflow and every historical custody version.
It records the parent run/digest and explicit run/key/digest locators for inherited
research evidence, including repeated reassessments. Previous agent reports move
into `report_history` in the new copy; historical copies are never overwritten.
Raphael appends his inspection, Onesimus appends his passive-Kratky concept report,
and Michael appends his deterministic financial assessment to successive full
copies of the **same** dossier. Snapshots and holder-linked custody receipts use
the existing shared store. On this path processing stops with Michael; no Uriel
package, conversion simulation or outreach is requested.

Michael requires the latest received engineering report and checks scenario
integrity and exact agreement with its inherited scenario before calculating.
His `michael-finance-report-1` binds both the engineering report and input dossier
digests. A normal complete report-bearing chain now has an additional Michael
version (Gabriel / Raphael / Onesimus / Michael); older three-version examples
below describe the earlier implementation. Reports for missing inputs are retained
as incomplete results, not a positive feasibility claim. A fully supplied
assumption bundle can produce conditional calculations; this is not evidence of
real building capacity or profitability.

Financial calculator version `michael-3` rejects incompatible currencies, units,
periods and classification labels. No implicit currency or gallon conversion is
performed. Canonical units are USD/kg, USD/L, USD/kWh, h/m2/week, USD/h and
USD/m2/year as appropriate to the named input. Nutrient periods distinguish
prepared solution from concentrate; unsupported bases refuse. Legacy blank units
and periods preserve the existing documented planning assumptions. A missing
observation date stays blank rather than being replaced with today's date.
Financial input names cannot override inspected geometry. Physical assumptions
belong in `site_inputs`, financial entries in `financial_inputs`; engineering
unknowns and safety limitations remain explicit.

**Validation / operational limits:** offline tests exercise genuine-record-shaped
**test material**, not newly acquired property evidence. They cover read-only
preview, same-dossier progression through all three reports, needs-evidence,
original-history preservation, replay, successor lineage, changed evidence/input
refusal, amendment permission, operator-bound approval, audited dispatch and
financial unit validation. Owner Ubuntu PostgreSQL regression and a separately
approved stored-real-dossier demonstration are still required after deployment.
Final Windows offline regression: **2,517 passed, 149 skipped, 16 warnings**,
zero failures; syntax and `git diff --check` passed. PostgreSQL-gated checks
were not executed locally. No new dependency, schema migration, discovery run
or provider allowance is added.
This does not implement authentication adapters for every discovered dataset or
solve missing/contradictory source facts by assertion.

## Current discovery contract: source-first, no batches (November 3, 2026)

**The 144-dossier / 36-cell batch workflow is retired, not paused.** Gabriel
qualifies and ranks sources, then scans one resource at a time from that retained
ranked plan. A source's permitted pages finish before the next resource is visited.
County and category are descriptive evidence, never scheduling quotas, completion
gates or geographic ranking bonuses. Source-ordered scans cover the entire selected
dataset, without the old geographic-cell or category filters. Collection still requires an approved
resource and request allowance; this does not authorize
unbounded browsing or change source licensing/access rules.

Each persisted candidate proceeds to the existing dossier/Raphael coordinator
without waiting for other counties, classes or source completion. The durable
backlog is recovery/backpressure, not a batch hold: it drains before more discovery.
An authorized dossier proceeds individually to Onesimus and Michael unless the
operator explicitly requested an inspection hold or first-authorization stop.
Research qualification never substitutes for Raphael authorization. Needs-evidence
returns and original dossier/custody history are preserved.

Historical pilot ledger rows and documents remain for audit. Old `pilot` and
`timed` runner policy inputs normalize to `source_ordered`; neither restores quota
behavior. `app.pilot plan` and `resume` refuse before database access. Historical
status and stop remain available. Old batch checkpoints are not silently imported:
use a new scope for this source-ordered workflow. No table deletion or migration
is required. The older pilot/batch descriptions below are historical and are
superseded by this section.

### Ranked plans and continuation

Durable source finding now retains `source_plan`: each executable resource keeps
its own qualified selection, resource digest and original finding-evidence binding.
The existing publisher, licensing, freshness and supported-adapter checks still
apply. Children are committed alongside the parent finding, loadable by digest,
and replay without another catalog/provider call. No uncommitted plan is cached
as authorization. The runner accepts an envelope `source_plan` parent digest plus
a legacy `bbox` context field, or an explicit `sources` list with retained `selection` proofs
and rank numbers. Wondrue accepts scalar `source_plan` and JSON-string `bbox` args
for the same backend path. Selection is not category verification.

DCAD bulk uses stable file ordering, a saved CSV position and exchange-snapshot
identity. Whole-resource pages stream a bounded number of rows without loading the
entire exchange into memory; duplicate/unusable rows still advance the cursor. A changed snapshot refuses the old cursor. Socrata uses ordered offsets;
ArcGIS requires an observed stable object-id field. The whole-resource query
omits the old cell geometry; Socrata and DCAD likewise do not filter by cell. Generic response pages and
continuations remain tied to the selected resource and shared durable acquisition
claims. All-duplicate or filtered pages advance using raw page positions rather
than new-property counts. A provider repeating the same page is blocked. Dataset exhaustion requires an explicit provider end; it never means all properties
on the internet, every dataset on a website, or verified property facts. Mutable APIs can
change during a scan; their offsets do not establish snapshot completeness.

CKAN/CSV, Overpass and any other adapter without a supported continuation contract
are reported blocked/incomplete in this sequential workflow; the runner does not
pretend their first page completes a source. Existing bounded one-pass APIs remain
available for compatibility. Missing pagination is not automatic authorization to
crawl a website. Per-source caps pause the current unfinished source instead of
hopping away. Duration, stop, Sabbath, creation, failure and network bounds remain.
Sources with unsupported pagination are recorded as blocked, not exhausted.

### Current commands and configuration

`config/source_ordered.example.json` is a bounded local-DCAD example, not a launch
authorization. It scans the staged exchange across all supported and unknown
category hints; unknown classification remains researchable. Example preview:

```powershell
.\.venv\Scripts\python.exe -B -m app.discovery_runner preview --scope source-first-dallas --envelope config/source_ordered.example.json
```

After owner approval and deployment, the same envelope is used by `run` with
`--acquire-approved`; add `--resume` to continue its saved source position. Stop
uses `python -m app.discovery_runner stop --scope source-first-dallas`. No live
worker is installed or launched by the implementation task. Default runtime policy
is `source_ordered`; no new environment variables or dependencies are introduced.
For Gabriel's discovered ranked plan, replace the example's `sources` with
`"source_plan": "<retained parent selection digest>"` and a legacy context
`"bbox": [south, west, north, east]` (not used as a source-ordered candidate filter). They cannot be combined with `sources`/`units`.
The old `cells`/`units` input names remain compatible. Their category labels and bounding boxes do
not filter source-ordered collection; equivalent resource entries collapse
to one scan. A legacy county field may still supply the approved bootstrap source
jurisdiction, but it does not establish a property's county or control handoff.

**Evidence requirement:** observed bulk/generic discovery records alone do not
authenticate category claims. The offline admission path above can reuse eligible
retained official account evidence; absent evidence still blocks authorization.
The scheduling change preserves that distinction;
it does not implement the separately proposed DCAD account-admission extension,
resolve DCAD connectivity/crawl-policy uncertainty, or promise a positive Raphael
decision. No synthetic property, publisher enrollment or verification bypass is
introduced. Owner PostgreSQL regression and deployed source-first demonstration
remain pending. No provider calls are made by local fixture tests.

Local source-first validation: **2,497 passed, 149 skipped, 17 warnings**, zero
failures. Syntax and whitespace checks passed. PostgreSQL-gated proof now includes
ranked-child selection loading, distinct per-page claims, zero-call page replay,
and persisted continuation JSON; it remains pending on the owner's Ubuntu gate.


### Turning Underutilized Commercial Real Estate into Productive Urban Agriculture

**Grow Up** is an AI-powered platform designed to identify underutilized commercial properties, evaluate their potential for conversion into indoor agricultural facilities, and help property owners discover new opportunities for their buildings.

By combining artificial intelligence, real estate analysis, agricultural engineering, and financial modeling, Grow Up explores how existing urban infrastructure can support sustainable food production.

**Developed for the Meta Global AI Developer Hackathon 2026.**

---

## 🎯 The Mission

Across American cities, commercial buildings contain vacant or underutilized space.

Meanwhile, urban communities face challenges involving food access, agricultural sustainability, and economic development.

Grow Up aims to connect these challenges with a new opportunity:

**What if underperforming commercial real estate could become productive indoor farmland?**

Our platform investigates that possibility through coordinated AI agents that discover properties, evaluate feasibility, design agricultural scenarios, and help connect viable opportunities with interested stakeholders.

Grow Up does not assume that every property is suitable or financially viable. Its purpose is to identify promising candidates through transparent, evidence-based analysis.

---

## 🏢 Target Properties

Grow Up initially focuses on three equally prioritized categories:

1. **Skyscrapers and high-rise buildings**
2. **Downtown commercial properties**
3. **Warehouses and industrial buildings**

Initial geographic coverage focuses on major metropolitan areas in Texas, with opportunities for future expansion.

The platform considers property utilization, building suitability, potential agricultural production, conversion costs, and financial feasibility.

---

## 🤖 Meet the Seven AI Agents

Grow Up is designed around seven coordinated AI agents working through a shared property-analysis workflow.

| Agent | Name | Responsibility |
|---|---|---|
| Property Discovery | **Gabriel** | Identifies and prioritizes commercial properties with conversion potential. |
| Property Inspection | **Raphael** | Evaluates building characteristics, occupancy, and potential conversion constraints. |
| Agricultural Engineering | **Onesimus** | Designs hypothetical indoor agricultural systems and evaluates production scenarios. |
| Conversational cockpit | **Wondrue** | Sole end-user interface: presents findings, takes commands/corrections, and invokes permitted operations through backend-enforced controls. |
| Financial Feasibility | **Michael** | Owns deterministic financial calculations and feasibility assessments. |
| Opportunity & Outreach | **Uriel** | Prepares opportunity and outreach campaign packages; no sender exists, and live mode alone grants no sending authority. |
| Conversion Tracker | **Priscilla** | Tracks delivery, engagement, responses, and lead conversion, distinguishing actual from simulated events. |

The agents share a common property record and coordinated workflow rather than operating as seven independent applications. Processing order is Gabriel → Raphael → Onesimus → Michael → Uriel → Priscilla, with Wondrue coordinating throughout (not a duplicate pipeline).

**MVP scope:** the coordinated MVP implements the full six-stage pipeline plus the Wondrue cockpit. Gabriel assembles evidence-backed dossiers; Raphael performs deterministic authorization over persisted dossier versions (preliminary simulation scoping only); Onesimus evaluates two passive-Kratky engineering configurations; Michael owns all financial arithmetic and feasibility verdicts; Uriel prepares genuine campaign packages (no sender exists); Priscilla tracks conversion events with simulated and actual evidence strictly separated. Wondrue enforces read-only / ask / full access modes server-side. Dossiers move between agents under shared operational custody (append-only versions, immutable snapshots, authenticated receipts); a resumable 36-cell pilot ledger tracks the 144-dossier target. No outreach execution exists.

**Long-term capabilities (planned, not implemented):** Nicole — Permits & Licenses; Theresia Key — Underwriting; Simeon — Deal Coordination/Closing; Solomon — Construction Coordination; Gabriella — Vacant-Lot Discovery; Elmer — Accounting; Yoseph — Insurance & Liability; Deborah — Legal; Dan — Owner/Asset Management. These names reserve future responsibilities only: no placeholder agents exist, and the implemented canonical roles stay Michael (finance), Uriel (outreach), and Priscilla (conversion tracking). Future execution agents follow the same boundary as outreach — demo dispatch is labeled simulation only, and live mode alone grants no spending, sending, contract, filing, or construction authority.

**Successor processing (bounded, implemented):** admitted DCAD or Census evidence is assessed on the same property, dossier, and custody lineage: the successor run references its parent, replays retained evidence without a new acquisition, appends the next custody version under an enrolled authorizer, and routes a fresh Raphael decision. Identical retries replay; original history is never rewritten and pilot counts replay without new credit. Census successors apply county-only attribution with the ordinary fail-closed category verdict. Further successor sources remain planned, not built.

---

## 🌿 Agricultural Approach

Grow Up focuses on **passive, non-circulating Kratky hydroponics**.

This approach allows plants to grow in nutrient solutions without requiring continuously circulating water systems.

Potential configurations include:

- Individual growing containers
- Shared or larger nutrient reservoirs
- Modular growing installations
- Vertically stacked growing systems

Agricultural scenarios must account for real-world constraints such as structural loads, lighting, electricity, humidity, crop suitability, labor, and operating expenses.

All proposed conversions remain hypothetical until their assumptions and constraints are independently verified.

---

## 🧠 Technology

Grow Up is being developed using:

| Component | Technology |
|---|---|
| Programming language | Python |
| Backend framework | FastAPI |
| Database | PostgreSQL |
| Artificial intelligence | Meta AI models via Baseten inference (shared budget boundary: live + labeled replay) |
| AI-assisted development | Meta Muse Code |
| Version control | Git |
| Source repository | GitHub |
| Development environment | Windows 11 and Ubuntu Linux |

The architecture emphasizes modularity, deterministic financial calculations, shared data, and responsible AI integration.

AI models assist with language understanding, analysis, explanations, and opportunity development. Financial arithmetic remains in inspectable Python code.

At runtime, Meta inference runs through one shared boundary (`app/inference.py`) over a small transport adapter (`app/model_adapter.py`, Baseten chat-completions transport): it interprets Wondrue commands, proposes configuration comparisons from an approved parameter catalog, explains stored results and economics, analyzes evidence, extracts document claims, and drafts Uriel copy. Structured output is validated before use; deterministic Python performs all calculations and authorization checks. Every live call reserves allowance from a shared PostgreSQL budget (`app/inference_budget.py`, migrations 016–017) before any provider request and is recorded with principal, agent, capability, association, provider, model, profile, usage, pricing provenance, and digests; labeled replays cover rehearsal and read-only mode. Live calls require explicit spending approval (`META_SPEND_APPROVED=1`), a granted token allowance, and configured pricing — otherwise they are refused before spending anything.

---

## 🚀 Hackathon MVP

The initial development goal is a functioning demonstration that:

1. Processes a commercial-property candidate.
2. Evaluates its potential suitability for agricultural conversion.
3. Generates a hypothetical Kratky hydroponic design.
4. Calculates preliminary financial feasibility.
5. Produces an opportunity assessment.
6. Tracks the opportunity through a shared workflow.
7. Presents results through a conversational dashboard.

The MVP will distinguish verified information, estimates, assumptions, and synthetic demonstration data.

**Project status:** Coordinated MVP implemented with two demonstration paths — real-property evaluation (fail-closed; a candidate may legitimately stop at `needs_evidence`) and an isolated synthetic demonstration that runs the full software workflow on fictional fixtures. Deferred: self-launching autonomous execution (bursts are owner-launched and bounded, with durable progression across restarts), exhaustive city searches, OAuth/admin systems, outreach, additional agents, advanced underwriting, and detailed permitting.

**GUADE runtime (Windows development only):** The retained activation audit records a completed Baseten coach -> Muse Wonder -> tests -> coach-review cycle on November 2, 2026 at 07:18:05 UTC; the committed trial artifact is `documentation/guade-activation-trial.md`. The activation ledger is exhausted (3 dispatches, 6 coach calls, 7,066 reported tokens, 1 retry) and is retained read-only. A historical completion is not a new spending authorization. The focused runtime corrections now require a strict explicit `APPROVE` before dispatch, exact task file allowlists, actual diff evidence for review, prompt-plus-completion reservations, a workspace-wide mission lock, a persisted deadline, stop checks and interruption records. File boundaries are fail-closed completion checks, not an OS sandbox. New `run-task` and `run-campaign` commands consume a finite owner-reviewed task list; neither generates new tasks, commits, deploys or sends outreach. `check` is read-only and reports configuration/headroom rather than live authentication. Prepared validation/campaign examples are not activated: see [GUADE operating instructions](guade/OPERATING.md) for the separate approval gates, exact commands, accounting limitations and emergency stop. The dollar planning ceiling is not a provider-enforced billing cap; Meta spending has no dollar enforcement in this controller. Dallas County official sources are cataloged in `config/dallas_official_sources.example.json`: DCAD bulk parcels/appraisal data are integrated as the `dcad_bulk` P1 discovery source, with `app/agents/gabriel/sources/dcad_convert.py` converting the official archives (PARCEL_GEOM shapefiles, EPSG:2275 centroids inverse-projected to WGS84; ACCOUNT_INFO / ACCOUNT_APPRL_YEAR / COM_DETAIL CSVs) into its exchange — 697,219 parcels and 847,075 accounts converted from the 2026-11-02 downloads. DCAD is enrolled in the shared verification registry as an authoritative source for recorded facts only (identifiers, situs addresses, appraisal values/years, recorded land-use classifications, improvements): a single-class record can verify that category fact, a mixed-class record cannot, and no DCAD fact establishes occupancy, vacancy, availability, or suitability. City of Dallas GIS zoning (ArcGIS `sdc_public/Zoning` layer 15) and Dallas Open Data (Socrata, dataset `f3t3-a5rd`) are integrated as enrichment adapters; Dallas County portals are excluded for discovery because DCAD administers the parcel/appraisal records and the County publishes no verified free bulk/API endpoint. The first real-data acceptance (2323 Chalk Hill Rd, account `00000700090000000`) discovered 90 real candidates in its official-data window and ended at Raphael `needs_evidence` — the recorded warehouse+office mix is preserved as a contradiction rather than resolved in our favor. The three M03A Dallas opening cells now use `dcad_bulk` as their primary P1 source (Overpass supplemental only); in the same window, 801 Jackson St (account `00000100324000000`, single recorded STORAGE WAREHOUSE class) verified its recorded warehouse classification through the authority pathway and passed Raphael's gate as `authorized`, with City of Dallas base zoning `CA-1(A)` returned by the GIS enrichment adapter. Verified appraisal classification is not verified occupancy, vacancy, or suitability, and the opening itself still requires an owner-approved live launch envelope (`acquire_approved` remains false).

**Overnight mission controls and agent completions (2026-11-02):** `guade/mission.py` adds a one-time mission layer (immutable authorization snapshot, exclusive lock, persisted deadline, emergency stop, preflight) with its own git-ignored ledger at `guade/state-overnight-20261102/`. Preflight found billable unattended launch **blocked**: `BASETEN_API_KEY` is absent from the mission process and Meta dispatch billing/free-credit spillover cannot be verified from this environment, so no billable GUADE call was made and engineering proceeded directly offline. **Recovery update (2026-11-02, later):** the credential defect is repaired permanently in guade/credentials.py (process env, then the Windows user environment for stale parents, then a restricted per-user env file; check now also refuses an exhausted ledger instead of reporting READY), and the worldwide qualification policy is wired into pilot planning through app/agents/gabriel/sources/discovery.py (Dallas keeps its registry-enrolled dcad_bulk adapter; the other eleven counties stay needs_review with no adapter until publisher verification and enrollment). A completed activation cycle is retained in the local audit and trial artifact; no new live validation or five-hour campaign is authorized by these runtime corrections. Provider billing remains separate from local planning counters, the 144-dossier pilot has zero confirmed dossiers (the pilot ledger is unreachable from Windows and no approved 144 envelope or staged exchange is present), and no five-hour mission was launched. Gabriel gained a universal source-qualification policy (`app/agents/gabriel/sources/qualification.py`) separating worldwide qualification rules from regional catalogs: candidates are scored on publisher identity, jurisdiction, access, licensing, format, and freshness; official status on non-`.gov` domains routes to publisher verification rather than trust or rejection (the Tarrant Appraisal District evaluates to `needs_review`, never automatic P1 authority), and the Dallas profile remains the first proven regional implementation. Onesimus now quantifies HVAC energy, nutrient volume, and labor hours when classified inputs are supplied (absence is flagged, never zeroed), and Michael adds operating break-even (kg and price), energy-cost sensitivity, optional occupancy costing, and named conservative/base/optimistic scenario bundles. Demonstrated end-to-end on 801 Jackson St with explicitly assumed planning inputs: engineering complete (rack3, 6,204 m² growing area, 232,650 kg/yr) and finance `unfavorable_under_assumptions` (operating net −$1,504,339.72/yr; break-even $14.47/kg vs the assumed $8.00) — a preliminary, assumption-bound result, not a verified opportunity. Uriel gained deterministic opportunity ranking (`rank_opportunities`/`draft_outreach` in `app/uriel.py`): verified facts outrank favorable projections, duplicates are excluded by canonical account/property identity, readiness separates outreach-ready from evidence-pending and not-ready, and drafts are never sent and may cite only verified facts. Priscilla's conversion tracking was verified as already meeting its lifecycle brief (durable events, simulated/actual strictly separated, metrics and funnel). Wondrue gained a read-only `inspect_pipeline` intent backed by `app/pipeline_view.py`, which presents the canonical Gabriel → Raphael → Onesimus → Michael → Uriel → Priscilla record with recorded truthfulness labels, names absent stages instead of hiding them, and never merges simulated with actual conversions.

---

## 🔧 Installation and Development

For the Windows-only GUADE operator workflow (project venv, credential presence check, separate spending approval, exact task allowlists, and STOP-file handling), see [Windows GUADE in the installation guide](INSTALLATION.md#windows-guade-windows-development-only).

Prerequisites: Python 3.12+ and PostgreSQL (for the real-property path; the synthetic demonstration needs no database).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell)
pip install -r requirements.txt
copy .env.example .env          # then fill in local values; .env is git-ignored
python -m app.migrate           # apply SQL migrations in filename order
uvicorn app.main:app --port 8001
```

Configuration lives in environment variables (see `.env.example`): `DATABASE_*` for PostgreSQL, `INTAKE_API_KEY` / `RESEARCH_API_KEY` for route authentication, `BRAVE_SEARCH_API_KEY` and `BRAVE_MONTHLY_CEILING` for research spend, `MVP_WORKFLOW_ENABLED=1` to switch on the real-property workflow entry points (off by default), `META_API_URL` (Baseten base or full chat-completions endpoint) / `META_API_KEY` / `META_MODEL` (`meta-models/muse-spark-open-1026`) / `META_SPEND_APPROVED` / `META_MAX_TOKENS` / `META_TIMEOUT_SECONDS` / `META_MAX_RESPONSE_BYTES` for Meta inference, plus `INFERENCE_TOKEN_ALLOWANCE` (default `0` = live disabled) / `INFERENCE_USD_PER_1M_INPUT` / `INFERENCE_USD_PER_1M_OUTPUT` / `INFERENCE_PRICING_MULTIPLIER` / `INFERENCE_PRICING_STATUS` / `INFERENCE_PRICING_SOURCE` / `INFERENCE_PRICING_DATE` (required provisional pricing basis; the retired `INFERENCE_USD_PER_1K_TOKENS` must stay unset) / `INFERENCE_DEMO_RESERVE` / `INFERENCE_LARGE_BATCH_TOKENS` / `INFERENCE_TOKENS_PER_IMAGE` / `INFERENCE_MAX_PROMPT_CHARS` / `INFERENCE_MAX_CONCURRENT` for the shared inference budget (live calls refused unless spending is approved, allowance granted, and pricing configured), `DEMO_MODE` (`yes` default / `no`; anything else rejected) for simulated external execution, `EMERGENCY_DIAGNOSTIC_DEMO` for the restricted post-pilot synthetic fallback, `CUSTODY_HMAC_KEY` for receipt authentication, `CUSTODY_AMENDERS` for the amendment authorizer allowlist (empty refuses all amendments), `WONDRUE_OPERATOR_KEY` / `WONDRUE_READER_KEY` for cockpit credentials (operator grants the full mode range, reader grants read-only), `SABBATH_BLACKOUT` (default `1`) for the discovery runner's Friday-evening-to-Saturday-evening pause window, and `DCAD_COLLECTION_ENABLED` / `DCAD_ALLOWED_ACCOUNTS` for bounded DCAD collection (both default closed), plus `CENSUS_COLLECTION_ENABLED` for bounded Census county admission (default closed).

**Permanent search priority (owner directive 2026-11-02):** Gabriel searches in one fixed nationwide sequence, installed in app/agents/gabriel/search_priority.py over config/search_priority.json (2020 Census basis): (1) DFW Metroplex county-by-county in the existing DFW order, (2) Houston Metroplex (12 most populous counties), (3) remaining Texas metros by principal-city population descending, (4) every remaining state by state population descending, each front being that state most populous city metroplex, then (5) restart at DFW with the next cycle. A front advances only when attempt-complete (quota met or legitimate sources exhausted; owner-selected trigger). The order is permanent; metro county sets resolve through batch-geography datasets and report geography_data_required where a dataset is not yet present rather than inventing counties. Gabriel discovery, qualification, and geography derivation contain no DEMO_MODE branch: demo/live parity for Gabriel is pinned by contract test (tests/test_search_priority.py), while the separate synthetic demonstration harness (app/demo.py) and Priscilla simulated events remain labeled simulations outside Gabriel. Checks: python -m pytest -q tests/test_search_priority.py.

**Nationwide US deployment (owner directive 2026-11-02):** Gabriel needs no preliminary human research for a new US jurisdiction, but there is no worldwide hard-coded source list and no all-US execution claim: `app/agents/gabriel/sources/us_discovery.py` is a thin compatibility wrapper that builds one mission over the configured national P1 catalogs (Socrata catalog and ArcGIS Hub; a CKAN catalog only when `GABRIEL_CKAN_CATALOG_URL` configures a live portal, data.gov having retired its CKAN API) and delegates to the existing shared durable selection path (`select_for_mission` → `source_finder.find_candidates` → shared qualification/ranking → `source_selection.select_sources`). All configured P1 catalogs are queried only within the shared finite allowance, preserving configured P1 catalog → approved P2 SearXNG (`GABRIEL_SEARXNG_URL`) → budget-approved P3 Brave precedence (Brave is not used by the generic proof path; Qwen/models are excluded from Gabriel finding/ranking). Candidates carry only observed metadata: jurisdiction, record types, publisher, license and freshness are never inferred from the search query, platform, or a nonempty license id (only known-clear license identifiers clear terms), duplicate dataset titles resolve by dataset/resource identity, and catalog observations never enroll the trusted registry or authorize property facts. A candidate whose terms/coverage/freshness/property content is unknown stays `needs_review` and is never selected. Only a fully qualified candidate is selected, and the retained decision persists the exact dataset/resource URL, platform, publisher/coverage metadata, supported field mapping, observed terms/freshness, policy version and resource/selection digests. The generic adapters (`generic_socrata`, `generic_arcgis`, `generic_ckan` in `app/agents/gabriel/sources/generic_open_data.py`) collect only that retained binding — never `GENERIC_*` environment locators (removed) and never via bootstrap; changing URL, publisher, jurisdiction, selection/resource digest, category or assignment on replay refuses before transmission. Catalog failure yields an empty ranking plus blockers, never invented sources. Batch geography states beyond Texas are data files in config/batch_geography.d/ (same Census schema), not code changes. Checks: `python -m pytest -q tests/test_us_discovery.py tests/test_source_discovery.py tests/test_batch_geography.py`.

**Dynamic batch geography (permanent, owner directive 2026-11-02):** new 144-dossier batches derive their geography; nothing is hard-coded to Dallas. config/batch_geography.json holds the versioned 2020-Census population dataset, and app/agents/gabriel/batch_geography.py computes (1) the most populated city in the requested state, (2) the 12 most populated counties of that city metropolitan area (OMB MSA + CSA counties), and (3) the diversity matrix of 4 candidates x 3 canonical categories (high_rise, commercial_property, warehouse_industrial) x 12 counties = 144. Applied to Texas today the derivation selects Houston (2,301,572) and Harris, Fort Bend, Montgomery, Brazoria, Galveston, Liberty, Walker, Waller, Chambers, Wharton, Matagorda, and Washington counties; Austin and San Jacinto counties rank 13th/14th and are excluded by the top-12 rule. The legacy Dallas/DFW roster (app/agents/gabriel/geography.py) and the in-flight DFW pilot ledger remain unchanged for Dallas searches and the first pilot batch. Unknown states or metros with fewer than 12 counties fail closed. Checks: python -m pytest -q tests/test_batch_geography.py.

Run the local suite with `python -m pytest -q tests` (PostgreSQL-gated tests skip without `GROWUP_PG_INTEGRATION=1`).

**Gabriel source selection (development implementation):** `select_for_mission` in `app/agents/gabriel/sources/discovery.py` accepts a jurisdiction mission, finds candidate sources, and reuses `qualify_source` / `rank_sources`. It returns `selected`, `needs_review`, or `no_supported_adapter`, with reasons and a policy digest. It does not enroll trusted publishers or verify property facts. Adapter bindings must match the candidate publisher; missing adapters or unresolved jurisdiction, licensing and freshness remain blockers. The pilot catalog still has only the Dallas official adapter: source finding does not manufacture adapters or prove coverage of the other 33 cells.

Real source finding requires a stable `assignment_key`, explicit mission approval and a finite `DiscoveryBudget` (default zero requests). The existing PostgreSQL workflow store retains an exclusive claim before transmission, conservatively reserves the entire request envelope, and stores bounded response evidence and the decision as `research_artifact` records. Same-key replay loads the stored result without DNS/HTTP; a claimed incomplete attempt is uncertain and never automatically retransmitted. Changed mission/limits refuse. The process dictionary is a cache, not durable storage. The API preserves the decision's assignment/run/evidence identifiers; collector enforcement can reload a stored decision after restart. PostgreSQL race/crash guarantees still require Ubuntu integration validation.

Finding precedence is configured catalog (`GABRIEL_CATALOG_API_URL`, CKAN `package_search` or a structured `sources` response), configured SearXNG (`GABRIEL_SEARXNG_URL`), then Brave only with an approved paid-request allowance and the shared monthly reservation. **Finding policy (owner directive 2026-11-03):** `GABRIEL_FINDING_POLICY=searxng_primary` disables government catalog/portal sourcing entirely (the configured catalog, the national Socrata/ArcGIS/CKAN catalogs, and the configured official catalog are all skipped) and makes SearXNG the primary and only unpaid finder; Brave remains a budget-approved last resort. With no `GABRIEL_SEARXNG_URL` configured, `searxng_primary` refuses honestly rather than silently returning to catalogs. Self-hosted SearXNG on loopback is supported (owner-approved 2026-11-03): `validate_endpoint` accepts a loopback literal only when the caller explicitly marks it as the operator-configured finder endpoint; every catalog, publisher, and candidate-derived URL remains public-HTTPS-only, credentials are refused even on loopback, and private LAN addresses stay refused. A SearXNG result is a lead, not a source: it claims no license, freshness, publisher authority, or adapter, so it routes to review and is never selected for collection until a later publisher-direct verification qualifies it. **Lead-to-source conversion (2026-11-03):** `app/agents/gabriel/sources/lead_conversion.py` resolves a lead to a concrete dataset resource under a bounded budget (at most four leads, at most two resolution requests each): Socrata resource/view/dataset-page URLs resolve directly, ArcGIS layer URLs resolve directly, ArcGIS Hub/arcgis.com item pages resolve through the publisher-maintained sharing item record, and any other page is fetched once and scanned only for explicit dataset-download or service-layer links (a download link records a `bulk_candidate` -- no generic bulk adapter exists yet, so it is never collected). Resolved Socrata/ArcGIS resources go through the existing publisher-direct verification (`verify_and_select`); only a fully verified resource becomes a durable selection, with every lead's outcome kept in `lead_attempts`. `discover_sources_anywhere(..., auto_convert=True)` runs conversion when finding produced leads but no selection. Checks: `python -m pytest -q tests/test_lead_conversion.py`. The default (unset) policy remains `catalog_first` for the existing Dallas/DCAD and test paths. Fallback after a definitive failure stays within the same total envelope; uncertain transport stops without fallback. Observed CKAN extras can establish candidate metadata; absent metadata remains unknown. These variables configure endpoints, not authorization. HTTP refuses redirects and private/non-global resolved addresses, uses `trust_env=False`, and bounds response bytes/time/results. DNS failure refuses; pre-resolution is not IP pinning and DNS rebinding remains a limitation. Provider selection and library tests are not live compatibility proof.

Discovery service and runner enforce a retained decision before collection. Existing DCAD bootstrap is Dallas-only; OSM bootstrap is supplemental geography; generic adapters cannot obtain any bootstrap (an omitted jurisdiction/source string refuses). Bootstrap records no verified license/freshness, and the pre-collection event is an audit trail, not durable provider replay protection. Generic collection additionally takes its own durable idempotent claim (existing workflow `research_artifact` records, no new tables): the claim commits before the provider transmission, the bounded response/digest and outcome are persisted, same-key/cold-cache replay issues zero HTTP/DNS and creates no new records/receipts/credits, and a claimed-but-unfinished attempt stays consumed — never auto-retried or refunded. Catalog and resource HTTP share one policy helper (`app/agents/gabriel/sources/http_policy.py`): credential-free public HTTPS, no localhost/private/internal literal or DNS-resolved non-global endpoints, redirects rejected, `trust_env=False`, streamed response capped before full buffering, bounded time/rows, no retries; DNS failure fails closed and DNS rebinding/TOCTOU remains a documented limitation (pre-resolution is not IP pinning). Test-injected transports/resolvers/claim stores are injectable callables. Test-injected transports may use the pure in-process finding path; real finding may not omit its stable assignment key. Offline checks: `python -m pytest -q -p no:cacheprovider tests/test_source_discovery.py tests/test_source_qualification.py tests/test_gabriel.py tests/test_discovery_runner.py tests/test_us_discovery.py`.

**Three-category inspection boundary (development implementation):** DCAD conversion now retains all improvement rows and serializes the largest-area improvement's paired story count, area, class and year. `dcad_bulk` maps an observed primary commercial/industrial improvement of at least eight stories to the same preliminary `high_rise` discovery hint used by OSM; invalid or absent story counts do not establish height. This threshold is not a structural or regulatory determination. Conflicting improvement classes remain retained. All three category hints still face the existing qualification and Raphael policies.

`start_evaluation_audited(..., stop_after_raphael=True)` persists the inspection boundary. Authorized dossiers remain in Raphael custody with `complete_inspected`; needs-evidence/rejected dossiers return to Gabriel on the same identity and custody version. Replays cannot silently advance a held dossier to engineering/outreach. Default existing end-to-end evaluations retain their earlier behavior. Checks: `python -m pytest -q -p no:cacheprovider tests/test_dcad_bulk.py tests/test_dcad_convert.py tests/test_funnel_acceptance.py tests/test_mvp_flow.py`.

**Official-record admission bridge (development implementation):** `app/agents/gabriel/sources/official_records.py` admits retained official discovery (currently staged `dcad_bulk`) and retained generic open-data discovery (`generic_socrata`/`generic_arcgis`/`generic_ckan`, labeled `generic_retained` — a generic source is not thereby official or authenticated) into the canonical dossier/custody/Raphael pipeline without a Brave call. It loads one server-stored `gabriel.discovery.decision` by exact discovery-run/event/property identity, validates the operational property address against the observed address, preserves raw observed fields/source/retrieval time and a computed evidence digest, and fails closed on digest or property mismatch. Caller-invented provenance/verified flags are never accepted. Only observed facts are built (category hint as observed claim via existing attribution); a staged CSV is an official-source claim, not authenticated publisher evidence, so `candidate_verification` stays absent and Raphael returns `needs_evidence` (returned to Gabriel, `current_holder=gabriel` with `feedback_reason`) unless genuine authenticated evidence later passes the existing shared verification policy. County stays unknown without a Census collector claim (explicit finite census limit, claim committed before transmission); occupancy/distress/zoning stay unknown. Authorized dossiers with `stop_after_raphael=True` hold at Raphael `complete_inspected` and never proceed to Onesimus/outreach. Same-key replay reuses dossier/custody identity with no duplicate receipts/credits or provider calls; explicit search evaluation remains available only for approved search missions, and source selection/envelope chooses the path (default provider budgets zero). Pilot readiness (`pilot_readiness` in `app/pilot.py`, also in runner `preview`) reports the 36-cell 12-county x 3-category x 4 target (144, never a Dallas-only substitute). Ledger counts cover credited county cells only; global raw/research-qualified totals and actual receipt-based return totals are not queried and remain null. The ledger incomplete counter includes research shortfalls and must not be labeled a Raphael needs-evidence count. The preview it names 33 unsupported county/category cells (11 counties missing adapters) and missing staged DCAD data honestly, and never claims the 144 goal met from raw candidates. Data staging prerequisite: lawfully downloaded DCAD exchange staged at `DCAD_BULK_DATA_DIR` with `dcad_accounts.csv` (historical download/conversion totals below are not proof files are present now). Offline checks: `python -m pytest -q -p no:cacheprovider tests/test_official_records.py tests/test_dcad_bulk.py tests/test_dcad_convert.py tests/test_funnel_acceptance.py tests/test_discovery_runner.py tests/test_source_discovery.py`. Read-only preview: `python -m app.discovery_runner preview --scope SCOPE --envelope ENVELOPE.json` and `python -m app.pilot status --json`. Launch requires an explicit bounded owner-approved envelope (no launch is part of this implementation). Offline proof is not live/PostgreSQL verification: Ubuntu PG remains the owner checkpoint, and no deployment/migration is included.

**Autonomous publisher enrollment (owner-authorized 2026-11-03):** Gabriel now makes enrollment decisions himself in `app/agents/gabriel/sources/autonomous_enrollment.py`, but only from evidence he retrieves directly from the publisher's own domain under the shared HTTP policy (Socrata view metadata plus a bounded 5-row sample; exactly two requests, claim committed durably before transmission, uncertain attempts stay consumed, replay returns the retained decision with zero network). The deterministic decision requires all of: government (`.gov`) domain, publisher identity matching the mission jurisdiction, a known-clear license observed in publisher-served metadata, known freshness within 800 days, observed property fields (parcel/account identity or address plus coordinates/address), observed sample rows, and sample geography consistent with the jurisdiction when geographic fields exist. Anything missing is recorded as `needs_more_evidence` with the exact missing criteria -- never guessed. Enrollment is narrow fact-level authority for the recorded facts the observed fields support only; it never covers vacancy, occupancy, availability, ownership, zoning, structural, agricultural, or financial claims. Enrollments persist as digest-bound workflow records, expire at a 180-day review-due date, never mutate the curated registry (curated entries win hostname conflicts), and merge into the verification path through `effective_registry()` (database failure falls back to the curated registry alone). Two owner-approved evidence avenues (2026-11-03) extend the same mechanism without weakening it. **Portal-terms license rule:** when dataset metadata carries no known-clear license, Gabriel seeks the license where the portal actually publishes it -- first the portal's own machine-readable `data.json` catalog entry for that exact dataset, then a terms page linked from the portal root on the publisher's own host. Terms clear only when the observed text affirmatively grants reuse (public domain, free to use, redistribution, CC0/CC-BY family identifiers) and contains no prohibition (reproduction bans, written-permission requirements, personal/non-commercial-only limits); silence, JS-only pages, or prohibitive terms never clear, and the terms evidence is digest-bound (`license_basis` records which avenue answered). **Directory-corroborated `.org` verification:** a non-`.gov` publisher domain may satisfy the authority basis when a directory served from a `.gov` host links to that exact domain with anchor text naming the publisher or jurisdiction -- directly, or through one jurisdiction-named subpage of the same directory (two-level directories such as a state index of per-county pages; bounded at two subpages) (`verify_directory_corroboration`; directory URLs are configuration via `GABRIEL_DIRECTORY_URLS`, never a hard-coded county list; the directory page is digest-bound). Verified live against the Texas Comptroller county directory: the Tarrant, Collin, and Denton appraisal-district domains are all corroborated through their per-county pages. Either basis alone suffices for the authority criterion; all other criteria are unchanged. **ArcGIS publisher evidence (2026-11-03):** the same mechanism now verifies retained `generic_arcgis` layers directly against the publisher's ArcGIS service -- layer metadata (`?f=pjson`: fields, last-edit freshness, copyright) and a bounded 5-feature sample, plus the publisher-maintained ArcGIS item record (owner, title, license/access information) when the resource carries an item id. License clears only on a known-clear identifier or an affirmative publisher-served terms statement. County-hosted ArcGIS servers (e.g. a county appraisal/parcel MapServer on the county's own domain) enroll under their exact service hostname, corroborated through the directory rule when the domain is not `.gov`; shared Esri hosting domains are never enrolled as a whole. **Item-scoped authority + directory-bound identity (2026-11-03):** an ArcGIS item hosted on a shared Esri domain may still enroll with `authority_scope: "item"` -- never as a registry domain entry -- when (a) the publisher-maintained item record was fetched and binds that exact layer (`item_bound`), and (b) the publisher's identity is bound through the directory chain: a `.gov` directory's per-jurisdiction page names the district's website, and that district site itself states its full district name (containing the jurisdiction) and uses the acronym the item publisher uses, with the acronym equal to the stated name's initials (e.g. CCAD = Collin Central Appraisal District). An acronym the district site never states, or an item record that does not bind the layer, proves nothing. The item record itself can also bind identity directly: when its publisher-stated credit (`accessInformation`) names a full district containing the jurisdiction and that name's initials equal the acronym the item publisher uses (owner/title/tags), identity is bound by the same record that binds the layer -- verified live for CCAD (Collin Central Appraisal District), whose district website bot-walls automated access (HTTP 403). **Polygon geometry in generic collection (2026-11-03):** parcel layers are polygons by nature, and the generic collector previously read coordinates only from Point geometry, silently dropping every polygon feature; it now derives a listing's coordinates from the polygon centroid (shoelace formula -- a representative point, never a fabricated address). **Appraisal-code vocabulary at the resource level (2026-11-03):** when a verified ArcGIS layer carries a category-code field, Gabriel observes the layer's own code distribution (one bounded group-by statistics query) during verification and records `category_codes` ({field, map, observed distribution, standard}) in the selection resource. The map applies the approved Texas appraisal category vocabulary (Comptroller/PTD: exact B and F/F1 → `commercial_property`, exact F2 → `warehouse_industrial`) only to observed codes and only when the retained layer/sample evidence establishes Texas scope. Publisher-maintained item metadata can supply that scope only when the item binds the layer; an unrelated item or a search query cannot. Other codes, including unknown F-prefixed codes, stay unmapped. These broad discovery classes do not establish actual use, vacancy, suitability, or Raphael authorization; observed high-rise evidence takes precedence over a broad commercial code. The category map, observed distribution and evidence digest now survive source selection inside the resource digest and are retained with the publisher evidence. Without a recorded map, generic observed-use classification applies. ArcGIS collection defaults to **one page/request**; a retained resource may explicitly set integer `max_pages` from 1 to 4. The full page allowance is reserved durably before transmission, never refunded on uncertainty, and completed collections retain every page URL, parameters, bytes and digest for zero-network replay. Up to 25 rows per page and 1,000,000 bytes per response are permitted; per-request timeout remains bounded at 30 seconds maximum, so multi-page elapsed time may exceed one request timeout. No automatic retry occurs. Older single-response records remain readable but cannot retroactively acquire missing earlier-page evidence. Appraisal field aliases (`propid`, `situsconcat`, `situscity`, `situszip`) are part of the observed-field vocabulary. Checks: `python -m pytest -q tests/test_autonomous_enrollment.py`. Selection executability (collection) never depends on enrollment; enrollment governs fact-level authority for Raphael only, and all other Raphael gates still apply. When a national-catalog candidate is stuck in `needs_review` only because the catalog could not establish its metadata, Gabriel asks the publisher directly (`verify_and_select`): only an `enrolled` publisher-direct decision lets him build a candidate from observed values, run it through the shared selection policy, and persist it as a durable selection under its own assignment key -- otherwise the cell stays `needs_more_evidence` with the missing criteria recorded. Checks: `python -m pytest -q tests/test_autonomous_enrollment.py`.

**Interrupted Muse recovery (2026-11-03):** the retained direct Muse session ended on a Meta HTTP 402 billing error after deploying `3d2455d`; its Collin attempt reported `category_codes: null`, zero candidates and no Raphael evaluation. That attempt is not a successful Collin demonstration. The correction preserves the map through selection and fixes page accounting/provenance without changing historical selections, claims or dossiers. A historical completed selection replays its original result; use a separately authorized, explicitly linked new assignment for a corrected live evaluation, never overwrite history or silently replace an uncertain claim. No migration, new environment variable, provider call, outreach or engineering change is part of this correction. Local checks: `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_autonomous_enrollment.py tests/test_us_discovery.py tests/test_source_discovery.py`. Recovery validation on Windows: **2,452 passed, 148 skipped, 17 warnings** with PostgreSQL integration disabled, provider credentials blanked and spending disabled. The full run needs local loopback access for FastAPI TestClient and a fresh temporary directory outside the repository for GUADE workspace-isolation tests. Owner-controlled Git/deployment, Ubuntu PostgreSQL gates and a bounded live Collin demonstration remain separate validation steps; offline fixtures do not prove them.

**Service-environment regression isolation (2026-11-03):** Ubuntu validation of `3618bc1` reported four failures (2,595 passed, one skipped) because catalog-fixture tests inherited the development service's `GABRIEL_FINDING_POLICY=searxng_primary` and `GABRIEL_SEARXNG_URL`. The catalog tests now pass an explicit isolated environment; a separate regression verifies that runtime calls without an override still honor the service environment and query SearXNG without catalog fallback. Runtime policy, service settings and credentials are unchanged. Focused checks: `.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests/test_source_discovery.py tests/test_us_discovery.py` (62 passed, two PostgreSQL-gated skips). Full Windows offline regression with the SearXNG service settings present: **2,453 passed, 148 skipped, 17 warnings**. Ubuntu PostgreSQL revalidation remains required; there is no migration or new live acquisition in this correction.

**Bounded Collin live proof — PASS (2026-11-03):** the owner-approved `owner-collin-proof-20261103-v4` assignment completed against development release `89db1f335683a9cc1b1252b28d630d5b77e2010f`. Exactly **five public ArcGIS GET requests** (four publisher-verification requests, one collection) were attempted under a durable five-request reservation, 12-second HTTP timeouts, no redirects/retries, and a one-property creation cap. No Brave, Census, Meta/Baseten, outreach, engineering, or financial execution occurred. The verified source enrollment is **item-scoped**, not authority for the shared Esri hostname. The selected resource retained the observed Texas appraisal-code vocabulary and its evidence digest. The canonical discovery service created the genuine record for **701 18th Street, Plano, TX 75074** (`df77d30e-f533-4fb6-a2e2-4ff2a5b958ac`); its raw source address and city were preserved, while the separate operational `state` field remains unknown/null. The ordinary retained-evidence evaluation persisted dossier `d37b2831-9027-5ff2-b55b-ff535930ee48` in evaluation run `ecdd2088-b43c-499b-bd65-ab66c347e74a`. Research qualification returned `qualified`, but county credit is empty and **zero pilot events** were credited. Raphael correctly returned `needs_evidence`: **no persisted category verification on the dossier version**. Two authenticated custody receipts prove Gabriel → Raphael → Gabriel on version 1; current custody is Gabriel. Same-key evaluation replay preserved the dossier and receipt count and added no provider requests. Parent run `e687783d-d7b1-4a74-8e7b-b0f6d39fbc28` and its historical records remained unchanged. Audit run `ce515ac5-3045-4a32-97ce-24d34b4b9bca` retains the envelope, individual attempted requests/responses and final result. This proves a real non-Dallas discovery/dossier/inspection-return path through the Python service APIs; it does not prove cockpit command dispatch, Raphael authorization, engineering feasibility, all-county coverage, or completion of the 144-dossier pilot. Do not rerun the acquisition assignment automatically: its five-request envelope is consumed. Next validation requires a separately bounded assignment. The accepted Ubuntu checkpoint before this proof was **2,600 passed, one skipped, 17 warnings**, with both development and gate-database migration/index/constraint checks passing; the proof added one genuine development property and no schema change.

**Catalog steering and observed-metadata rules (2026-11-02):** national-catalog queries carry property subject terms so property datasets surface over generic city data; jurisdiction is established only from a candidate own title, publisher, or portal domain (never the query alone); observed Public Domain and CCZero license labels are known-clear; and among executable candidates, selection prefers observed property relevance (property subject, parcel/account identity, then address/coordinates) so budget, wage, or incident datasets cannot outrank property datasets. Verified live against Seattle: the Building Energy Benchmarking dataset (parcel id, address, coordinates) is the selected generic Socrata source.

**Generic formats and the non-Dallas proof (development implementation):** Socrata collects the actual retained `/resource/<dataset>.json` endpoint with observed field metadata; ArcGIS queries the retained layer with `inSR=4326`/`outSR=4326` and preserves GeoJSON geometry; CKAN collects a retained CSV resource with finite bounded rows and explicit fields. Rows with missing/non-finite coordinates or outside the requested bbox are strictly excluded (never returned as a fallback), requested category filters are enforced, and category is mapped only from observed use/building/stories through the existing normalization — unknown/missing category stays unresolved and earns no credit, mixed/contradictory observations are preserved. Observed dataset fields are retained as records and provenance, not disguised as Brave snippets; no state, county, vacancy, ownership, occupancy or zoning is invented, and Census county credit is reused only from stored authenticated Census records/approved bounds (unknown county stays unknown). One shared operational property and deterministic dossier UUID, the same append-only version/snapshot/receipts, and fail-closed Raphael apply: a generic record therefore returns `needs_evidence` to Gabriel with an authenticated return receipt unless genuine authenticated evidence later passes the shared policy. **Repeatable read-only preview:** `python -m app.discovery_runner preview --scope SCOPE --envelope ENVELOPE.json` and `python -m app.pilot status --json` (no provider, production or model/outreach calls). **Bounded non-Dallas proof procedure (offline, disposable data):** run `python -m pytest -q -p no:cacheprovider tests/test_us_discovery.py tests/test_source_discovery.py tests/test_discovery_provenance.py tests/test_official_records.py` — these drive a fake-realistic non-Dallas (Travis County) catalog through shared selection, the actual discovery service, retained structured admission, dossier persistence and Raphael `needs_evidence` return on the same identity, plus same-key replay with zero provider calls. This fixture proof is test-only (no synthetic property in `growup_dev`, no real-property claim); a finite real-property proof is exercised separately by the owner when service access is available. Actual regional coverage today remains the DFW-first pilot (12 counties × 3 canonical categories × 4 qualified dossiers = 144, never invented quota and never a Dallas-only substitute); no nationwide rollout, scheduler expansion or 144 batch is part of this path, and `stop_after_raphael` remains the proof boundary (no Onesimus/outreach is executed or presented).

**Current data readiness (verified November 2, Windows):** existing archives were found at `%LOCALAPPDATA%\Temp\growup-dcad\raw`; both sizes and SHA-256 hashes match the saved `acquisition.json`. This verifies local integrity against that manifest, not property claims. The original exchange contains 847,075 account rows and 697,219 parcel rows. A separate regenerated exchange at `guade/state-source-pilot/dcad-exchange-20261102` preserves all improvement rows and paired story counts; its conversion manifest records output hashes and converter identity. It is ignored by Git; do not commit raw property datasets. Preliminary category hints across the exchange are 373 high-rise, 15,887 commercial and 8,316 warehouse/industrial, not qualified or verified totals. No new archive download or property/database write was needed. The inspected Ubuntu development service still has `DCAD_BULK_DATA_DIR` unset, and its scoped search found no matching files. Windows files are not automatically accessible to that service. Stage the regenerated files using the owner-controlled deployment/data procedure before a live run; do not overwrite the originals. Earlier 801 Jackson acceptance remains a historical report rather than a record confirmed in the currently inspected development database. No migration is needed for these changes.

API credentials and other secrets must never be committed to the public repository.

---

## 🗄️ Property Data Foundation (MVP-002)

MVP-002 provides the shared PostgreSQL-backed property record that all seven
agents will eventually use. Each property has a stable UUID, address, category,
sourced numeric facts, evidence references, and timestamps.

**Evidence model (owner-approved):** numeric facts are stored as explicit
`estimated` / `verified` pairs within one record, so sourced estimates and
verified facts coexist without being confused. Unknown values stay null --
never zero, never invented. Hypothetical values are not permitted in
operational records; hypothetical agricultural or tenant-consolidation
scenarios belong in separate future storage, and no simulation subsystem is
built in this milestone.

Sourcing rules, enforced at the API boundary:
- Any populated estimated or verified value requires traceable record-level
  `provenance` naming its source (substantive, non-placeholder).
- Every populated verified field additionally requires a supporting reference
  in `field_evidence`, keyed by field name.
- `data_status` (`verified` / `estimated` / `mixed` / `unknown`) is derived
  server-side from the populated pairs. A client label is never trusted, and
  `hypothetical` is rejected outright.
- Vacant area must not exceed building area within the same evidence class.

### API endpoints

| Method | Path | Result |
|---|---|---|
| `POST` | `/properties` | `201` with the created record (server assigns `id`, timestamps, derived status) |
| `GET` | `/properties` | `200` with stored records, newest first (`limit`/`offset` supported) |
| `GET` | `/properties/{property_id}` | `200` with the record, or `404` when the id does not exist |

Invalid input -- including unsourced estimates, unverified-without-evidence
values, or a `hypothetical` status -- returns `422`; an unconfigured or
unreachable database returns `503` with a generic error category (never
credentials or driver details). The MVP-001 `GET /health` and `GET /ready`
endpoints are unchanged.

Example (sourced estimate):

```bash
curl -X POST http://127.0.0.1:8001/properties \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example Tower","city":"Dallas","state":"TX",
       "category":"commercial_property",
       "building_area_sqft_estimated":50000,
       "provenance":"Dallas Central Appraisal District record, account 00001234, retrieved 2026-10-26"}'
```

Record fields: `name` (required), `street_address`, `city`, `state`,
`zip_code`, `category` (required: `high_rise`, `commercial_property`,
`warehouse_industrial`, `empty_lot`, or `other`), `occupancy`, `owner`,
`provenance`, `field_evidence` (JSON object mapping field names to evidence
references), `data_status` (server-derived), and `notes`, plus these nullable
estimated/verified pairs:

| Estimated | Verified | Type |
|---|---|---|
| `building_area_sqft_estimated` | `building_area_sqft_verified` | sqft (nonnegative) |
| `vacant_area_sqft_estimated` | `vacant_area_sqft_verified` | sqft (nonnegative) |
| `gross_monthly_rent_income_estimated` | `gross_monthly_rent_income_verified` | money |
| `owner_monthly_costs_estimated` | `owner_monthly_costs_verified` | money |
| `property_value_estimated` | `property_value_verified` | money |
| `liens_total_estimated` | `liens_total_verified` | money |
| `months_on_market_estimated` | `months_on_market_verified` | distress: whole months, nonnegative |
| `months_vacant_estimated` | `months_vacant_verified` | distress: whole months, nonnegative |

Money is stored as PostgreSQL `NUMERIC` and handled as exact `Decimal` values
in Python -- never converted through float. Send monetary values as JSON
strings (e.g. `"4500000.10"`) and expect them back as JSON strings. Unknown
financial information stays null.

Distress indicators: `months_on_market` is the duration a property has been
actively listed for sale; `months_vacant` is the duration the relevant
building or space has been vacant. They are separate measurements and are
never treated as equivalent. A property being listed for sale, being vacant,
and being financially distressed are three distinct conditions -- one does
not imply the others, and distress requires supporting evidence.

### Canonical categories and the downtown attribute

The three canonical pilot categories are `high_rise`, `commercial_property`,
and `warehouse_industrial`. The former `downtown_commercial` category is
retired: historical records keep their stored value and remain readable, and
legacy input spelling `downtown_commercial` normalizes to
`commercial_property` on intake, but new canonical search plans and newly
persisted records always use `commercial_property`.

Downtown location is a separate property attribute, `is_downtown`
(default `false`), independent of category. `false` means downtown priority
is *not established* -- the property may be non-downtown or its downtown
status may simply be uncertain. `true` requires credible,
property-specific evidence explicitly locating the property downtown
(e.g. "located in Downtown Dallas"); "near downtown", "minutes from
downtown", downtown views, search-query terms, and bare keyword matches are
insufficient, as is a historical `downtown_commercial` label. Positive
determination is currently deferred: all records carry `false` until an
evidence-based determination step lands.

Discovery prioritization follows the Big Game Hunter hierarchy -- high-rise
first, then downtown commercial property, then (tied) non-downtown
commercial and warehouse/industrial property, with asset value breaking the
tie. Hunting priority only orders where Gabriel looks first: it never
excludes properties, overrides qualification, or starves lower-priority
pilot cells. The full priority scheduler is deferred; current ranking
behavior is unchanged.

Note: `empty_lot` is accepted by the data model but remains outside Gabriel's
initial discovery scope, alongside food forests.

### Database migrations

SQL migrations live in `migrations/` and are applied in filename order with:

```bash
python -m app.migrate
```

The runner is repeatable: applied files are recorded in a `schema_migrations`
table and skipped on later runs. It only touches the configured database and
never creates, drops, or alters other databases.

Migration `012_workflow_runs.sql` adds the `workflow_runs` table (JSONB run
payloads plus versioned workflow records) backing Gabriel → Raphael →
Onesimus run persistence. It is additive: rolling back the application never
requires schema reversal. Apply with `python -m app.migrate` (never against
production/real data without explicit approval) and validate with
`python -m pytest -q tests/test_workflow_store.py`. The synthetic harness never
touches this table; demo state lives only in memory.

Migration `013_dossier_custody.sql` adds the shared custody, intake, and
pilot tables: `dossier_versions` (one deterministic dossier identity per
operational property, append-only), `custody_snapshots` plus
`custody_snapshots_archive` (immutable pre-handoff freezes), `custody_receipts`
(HMAC-authenticated forward/return/review handoffs), `michael_intake`
(pre-canonical intake queue, renamed by migration 014), and `pilot_state`
/ `pilot_cells` / `pilot_events` (the 36-cell ledger). Database triggers
reject updates and deletes on the permanent tables; hot snapshots
additionally allow archival moves. Validate with
`python -m pytest -q tests/test_custody.py tests/test_pilot.py
tests/test_michael.py`.

Migration `014_canonical_roles.sql` renames `michael_intake` to
`uriel_packages` (pre-canonical rows keep `role_schema`
`mvp1-michael-intake`; new rows use `canonical1-uriel`), adds
`package_state` / `execution_state`, versions custody receipts
(`mvp-1` history vs `canonical-1` new rows), widens `pilot_cells` with
the engineering/financial/package/tracked stage counters, and adds the
append-only `conversion_events` table for Priscilla. It never rewrites
existing rows. Validate with `python -m pytest -q tests/test_uriel.py
tests/test_priscilla.py`.

Migration `015_simulation_discovery_pilot.sql` adds the append-only
`priscilla_simulations` run table, a nullable `simulation_id` on
`conversion_events` plus a partial unique index over
(simulation, package, kind) so repeated execution cannot duplicate
events, provenance columns on `pilot_events` (`run_id`, `record_key`,
`identity_kind`) with an index over canonical qualified rows, and the
`discovery_runs` worker table with a partial unique index enforcing one
active worker per scope. It is additive: no existing table is altered
beyond `ADD COLUMN` with safe defaults, and no rows are rewritten.

Migration `016_inference_usage.sql` adds the shared inference budget
tables: `inference_allowance` (singleton competition-wide token
allowance, consumed units, demonstration reserve), the
`inference_usage` request-status ledger (request/idempotency
identity, principal, agent, capability, dossier/run/package
association, provider, model, prompt/schema versions, digests,
limits, bound/reserved/reported/excess units, estimated cost,
outcome, live/replay status, `replay_of` links, timestamps), and
`extraction_originals` (page originals keyed by document and page
with digests and source references). Ledger rows transition status
exactly once with spend columns only increasing — status-tracking,
not append-only. It creates new tables only: no existing table is
altered, no rows are rewritten, and application rollback needs no
schema reversal. Validate with
`python -m pytest -q tests/test_inference_assist.py` (mocked) plus
the `GROWUP_PG_INTEGRATION`-gated budget round-trip and concurrency
checks on a disposable database after `python -m app.migrate`.

### Migrations & rollback

`python -m app.migrate` applies pending files in filename order inside
one transaction and records them in `schema_migrations`; a failed run
leaves the schema unchanged and is safe to retry. There are no
down-migrations: application rollback never reverts schema changes, so
every migration must stay readable by the previously deployed code —
except migration 014, whose `michael_intake` → `uriel_packages` table
rename is **not** rollback-transparent: older application code that
queries `michael_intake` will fail after 014 is applied. Rolling back
past 014 therefore requires either keeping code that reads the new
table name or a reviewed forward migration restoring a compatible view.
Migrations 012, 013, and 015 through 017 are additive-only and
need no schema reversal on application rollback. Local coverage is
transaction-faithful fakes plus the focused suites
(`tests/test_priscilla_sim.py`, `tests/test_pilot.py`,
`tests/test_discovery_runner.py`, `tests/test_funnel_acceptance.py`,
`tests/test_inference_assist.py`).
`GROWUP_PG_INTEGRATION`-gated live tests exist for the migration-created
schema — runner SQL replay, backlog round-trip, crash recovery fold,
ceiling drain, two-connection exclusion (`tests/test_discovery_runner.py`),
simulation idempotency/conflict/separation and concurrent winner
(`tests/test_priscilla_sim.py`), custody round-trip, concurrent issuance
serialization, append-only triggers, archival (`tests/test_custody.py`),
run lifecycle, atomic audit, and the `identity_review` record
(`tests/test_workflow_store.py`), pilot round-trip
(`tests/test_pilot.py`), Raphael authorize idempotency
(`tests/test_raphael.py`), and the full coordinator chain
(`tests/test_funnel_acceptance.py`), and the inference budget
reserve/reconcile round trip (`tests/test_inference_assist.py`).
Confirmed deployed checkpoint `2aebc93` (migrations 001–017 on
both databases): full regression reached **2,097 passed, zero
failures/skips** (17 deprecation warnings); operational health
checks passed for both databases, application health/readiness,
PostgreSQL availability, and the service; HTTPS `/wondrue`
routing works, and owner browser acceptance passed for profile
persistence, the throttle indicator, and the target banner.
Earlier runs had surfaced (and corrected) five custody-ordering
failures, two test-contract mismatches, two route-test
ambient-database assumptions, the throttle-profile review gaps,
and the unrendered cockpit template braces. This tree holds only
the owner-operated Wondrue credential procedure below (no code
changes): provisioning, restart, and validation are **pending
owner execution**. No migration, Nginx change, or deployment is
part of this task.

The owner has additionally confirmed the deployed baseline
(`2aebc93`: 2,097 PostgreSQL tests passed, zero failures/skips, 17
warnings; health, HTTPS, browser, and Wondrue authentication checks
passed) and then **intentionally reset application data**:
`growup_dev` currently contains no properties, evidence, or workflow
records. The acquisition-budget and transaction-ordering code in
this tree (`app/agents/raphael/acquire.py`,
`app/mvp.py`, `app/wondrue.py`, `app/discovery_runner.py`,
`app/workflow_db.py`, `tests/test_acquisition_budget.py`) is newer
than that checkpoint: local suites are green (see the acquisition
test commands below), while targeted rerun, full regression, and
the `GROWUP_PG_INTEGRATION`-gated acquisition checks on the
disposable database are **pending owner execution**.

### Synthetic demo fixture

`fixtures/synthetic_dallas_property.json` is a clearly labeled **SYNTHETIC**
Dallas commercial property with invented values (`data_status: hypothetical`).
It is not a real listing. Hypothetical values are barred from operational
records, so the API rejects it (`422`), and the loader refuses it:

```bash
python scripts/load_synthetic_fixture.py   # exits 1: refused by policy
```

The fixture remains available for isolated automated testing only (tests read
the JSON without inserting it anywhere).

The coordinated MVP adds a fuller synthetic harness (`app/demo.py`): an
in-memory demo store running the complete Gabriel → Raphael → Onesimus
software workflow on fictional fixtures. Demo identifiers use the `demo-`
prefix and are rejected by every production mutation/authorization interface;
fictional sources (`*.fiction.example`) are never fetched; every artifact,
decision, scenario, export, and UI response carries the synthetic-origin
marker and the label **SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY;
VERIFICATION AND AUTHORIZATION ARE SIMULATED**. There is no
demo-to-production promotion path, and resetting the demo store
(`POST /wondrue/demo/reset`) cannot affect real records. The harness needs no
production database credentials and writes no operational properties,
evidence, dossiers, decisions, registry entries, quotas, or audit business
records.

The harness is a separately selected diagnostic fallback
(`POST /wondrue/command` with `target: "demo"`), independent of
`DEMO_MODE`: Gabriel, Raphael, and Onesimus use genuine evidence and
unchanged verification rules in both modes, and simulated upstream
outcomes can never enter operational custody, pilot counts, Uriel
packages, or Priscilla actual metrics (`demo-` identifiers are rejected
at each boundary). After the first evidenced 144-dossier pilot,
ordinary operational selection of the harness is refused; tests keep
working and emergency diagnostic use needs `EMERGENCY_DIAGNOSTIC_DEMO=1`
plus explicit owner authorization.

---

## 🔍 Gabriel — Property Discovery Agent (MVP-003)

Gabriel researches **real** commercial properties and records them as
preliminary operational records through the existing MVP-002 validation gate.
He discovers possible opportunities; he never verifies claims. Verification
belongs to Raphael (see [Coordinated MVP Workflow](#-coordinated-mvp-workflow-gabriel--raphael--onesimus--michael--uriel--priscilla)).

### Discovery source

Gabriel ships two real providers behind a modular interface so more can be
added later:

- **Overpass (`overpass`)** — OpenStreetMap building footprints with names,
  addresses, and building-type tags, queried over a caller-supplied bounding
  box via the public Overpass API (`https://overpass-api.de/api/interpreter`,
  docs: `https://wiki.openstreetmap.org/wiki/Overpass_API`). No credentials
  required. Access complies with the OSM usage policy: bounded result counts, explicit timeouts, identifying User-Agent, and no bypassed restrictions.

- **OSM API (`osm_api`)** — Mapped building geometry and tags from the official
  OpenStreetMap API v0.6 map endpoint
  (`https://api.openstreetmap.org/api/0.6/map`, docs:
  `https://wiki.openstreetmap.org/wiki/API_v0.6`). No credentials required.
  Strictly limited to small controlled boxes (at most 0.05° per side and
  0.001 square degrees); larger boxes are rejected before any request, with
  no retries, subdivision, or bulk harvesting. One request per search, with an identifying User-Agent.
Mapped buildings are not verified commercial-property opportunities: a record
means "this building exists at this location according to OpenStreetMap,"
not "this property is listed, vacant, or distressed." Suitability and
verification belong to Raphael and Onesimus in later milestones.

High-rise, commercial-property, and warehouse/industrial candidates are
queried together with no ranking boost, preserving equal priority. OpenStreetMap
provides no listing prices, vacancy figures, or ownership data, so Gabriel's
records carry `unknown` status with unknowns as null and a notes entry
describing what is missing. Long listing or vacancy periods are never treated
as proof of financial distress.

To add a provider: implement the `DiscoverySource` interface in a new module
under `app/agents/gabriel/sources/`, register it in `SOURCES`, and cover it
with mocked tests. No other Gabriel code changes.

### API endpoints

| Method | Path | Result |
|---|---|---|
| `GET` | `/discovery/sources` | `200` with provider metadata (no credentials needed) |
| `POST` | `/discovery/runs` | `200` with run counts, created ids, and skip reasons |

`POST /discovery/runs` accepts `source` (default `overpass`), a bounded `bbox`
(max 0.5° span per side), optional `categories`, `limit` (1–25, default 10),
and `timeout_seconds` (5–60, default 15). Unknown sources and unbounded input
return `422`; provider failures return `502` with nothing fabricated or
persisted; database problems return `503` as with the property endpoints.

### Evidence, duplicates, and integrity

- Every record keeps traceable `provenance`: provider name, query bounds,
  retrieval timestamp, and the OpenStreetMap element URL.
- Gabriel populates estimated/unknown fields only -- never verified fields
  or `field_evidence`. All payloads pass the MVP-002 `PropertyCreate` gate.
- Duplicates are skipped, never merged: same normalized street address means
  the same property. Gabriel never updates existing rows, so verified values
  cannot be overwritten by discovery. Skipped candidates are reported with
  reasons (`already stored`, `duplicate within this run`, `insufficient
  location information`, `failed operational validation gate`).
- The synthetic Dallas fixture is never inserted by discovery; empty
  provider results persist nothing.

### External configuration requirements

None beyond MVP-002. No API keys or credentials. The host needs network
egress to `overpass-api.de` (HTTPS) and the usual `DATABASE_*` settings with
migrations applied.

### First controlled discovery test

Run against a development database with migrations applied (never production):

```bash
# 1. Confirm the provider is registered (no credentials needed).
curl http://127.0.0.1:8001/discovery/sources

# 2. Run a small bounded discovery pass over downtown Dallas.
curl -X POST http://127.0.0.1:8001/discovery/runs \
  -H 'Content-Type: application/json' \
  -d '{"source":"overpass",
       "bbox":{"south":32.770,"west":-96.810,"north":32.790,"east":-96.785},
       "limit":3,"timeout_seconds":20}'

# 3. Inspect what Gabriel stored (unknown-status, sourced, nulls for unknowns).
curl "http://127.0.0.1:8001/properties?limit=3"

# 4. Try the official OSM API provider on a small controlled box
#    (at most 0.05 degrees per side and 0.001 square degrees).
curl -X POST http://127.0.0.1:8001/discovery/runs \
  -H 'Content-Type: application/json' \
  -d '{"source":"osm_api",
       "bbox":{"south":32.780,"west":-96.797,"north":32.781,"east":-96.796},
       "limit":5,"timeout_seconds":20}'

# 5. Re-run step 2 unchanged: previously stored addresses must now be
#    reported as skipped duplicates with zero new records created.
```

### Known limitations

- OpenStreetMap coverage varies; some candidates lack names or addresses and
  are skipped as unusable. Address matching is intentionally simple (case,
  punctuation, whitespace) -- abbreviation variants may not dedup.
- Gabriel finds candidate buildings, not listings: prices, vacancy duration,
  and ownership require future providers and Raphael's verification.
- Live Overpass research needs direct HTTPS egress; sandboxed or proxied
  networks may fail closed with `502 source_unavailable`.

---

## 📥 Commercial Listing Intake (MVP-004A)

`POST /discovery/intake` accepts structured commercial listing information
supplied through authorized means: manually researched listings, user-supplied
details, permitted public datasets, and (later) licensed providers. It is an
**intake mechanism, not autonomous web search**: it never scrapes, never
bypasses restrictions, and never fetches submitted URLs from the server.

How it differs from discovery runs: discovery (`POST /discovery/runs`)
researches providers itself; intake receives explicit, attributable listings
and maps them onto operational records with evidence preserved.

### Supported listing fields

Identity: `name`, `street_address`, `city`, `state`, `zip_code`, `category`
(existing conventions, default `other`). At least a name or street address
is required.

Commercial: `asking_price`, `advertised_building_area_sqft`,
`advertised_available_area_sqft` (preserved as evidence only; available space
may still be occupied), `listing_type` (`sale`/`lease`/`auction`/
`unknown`), `occupancy_info`, `listing_date`, `months_listed_for_sale`,
`months_vacant` (explicit figures only), `asking_rent_monthly`,
`owner_advertised`, `listing_notes`, `price_reductions` (up to 10), plus up
to 10 `distress_observations`.

Mapping discipline: advertised building area →
`building_area_sqft_estimated`; explicit durations → estimated month fields.
Asking price is evidence only (an advertised price is not necessarily a
market-value estimate) and available area is evidence only (available space
may still be occupied): neither populates estimated nor verified value or
vacancy columns. Asking rent never becomes rental income; an advertised owner
never becomes ownership. Unmappable attributes (asking price, available area,
asking rent, advertised owner, listing type/date/history) are preserved as
`listing_evidence` rows linked to the property -- never forced into
unrelated columns. Unknowns stay null.

### Required provenance

Every listing needs `source_name` (traceable, non-placeholder),
`listing_url` (valid HTTP/HTTPS), and `observed_at` (valid timestamp, not
in the future). Intake builds record provenance from these and adds
per-field evidence references for each populated estimate. Distress
observations need an indicator type, factual description, and observation
details (defaulting to the listing source); they enter as `preliminary`
only -- intake rejects `verified` claims, since verification belongs to
Raphael. Synthetic or hypothetical sources are rejected.

### Illustrative example only (do not insert)

```bash
curl -X POST http://127.0.0.1:8001/discovery/intake \
  -H 'Content-Type: application/json' \
  -H "X-Intake-Key: $INTAKE_API_KEY" \
  -d '{"name":"ILLUSTRATIVE Example Warehouse (fictional)",
       "street_address":"9999 Example Way","city":"Dallas","state":"TX",
       "category":"warehouse_industrial",
       "source_name":"Illustrative Example Brokerage",
       "listing_url":"https://example.com/listings/illustrative-9999",
       "observed_at":"2026-10-20T12:00:00Z",
       "asking_price":"1250000.00","advertised_building_area_sqft":20000,
       "listing_type":"sale"}'
```

Response: `201` with `{"outcome":"created","property_id":"…",
"filled_fields":["building_area_sqft_estimated"],
"listing_evidence_id":"…","distress_observation_ids":[],
"message":"…"}`. Enrichments and duplicate skips return `200` with
`outcome` `enriched` or `skipped_duplicate`; invalid intake returns `422`.

### Duplicates and enrichment

Same normalized street address means the same property (address-less
listings cannot dedup and always create -- a known limitation). Intake
fills NULL estimated fields only, guarded per-column, and never touches
verified or existing values; conflicting fills are dropped while the new
listing evidence is still preserved. Resubmitting an identical
property+URL pair changes nothing.

### Security limitations

`POST /discovery/intake`, `POST /discovery/runs`, `POST /properties`,
`GET /properties`, and `GET /properties/{property_id}` require the
`X-Intake-Key` request header matching the `INTAKE_API_KEY` environment
variable (compared in constant time). `POST /discovery/research-runs` and
`POST /discovery/investigations` require the `X-Research-Key` header
instead. A missing or empty server key fails closed with `503`; a missing
or wrong header returns `401`. Keys never appear in logs or error responses.
Set a long random value via your environment (see `.env.example`); never
commit a real credential. Remaining limitations: one shared secret with no
rotation, per-client scoping, or audit trail. Some read-only and
computational endpoints intentionally remain public at this stage.
Submitted URLs are stored, never fetched, so intake cannot
become an SSRF mechanism.

### Future provider integration

Providers stay modular behind the existing source interface; licensed
listing APIs can later submit through this same intake contract once
credentials and terms are arranged. Trellistate is a potential future
provider, but its public API currently returns HTTP 500 and is not
integrated or depended upon in MVP-004A.

---

## 🗺️ Gabriel Search Planner (MVP-004B-1)

The planner generates structured commercial real estate research
assignments across the twelve-county operating area. **Listings first.
Value driven. Distress focused.** It produces assignments only: no
searches executed, no scraping, no AI calls, no database writes, no
network requests.

### Architecture

`app/agents/gabriel/planner.py` exposes a narrow Python interface:

- `plan_initial_searches(config)` — balanced county-wide assignment set.
- `plan_followups(parent, feedback, config)` — bounded single-step
  adaptation from structured research feedback (stays in the parent's
  county).

`GET /discovery/search-plan` previews the initial plan (read-only;
`total`, `per_category`, `downtown`, and `secondary_counties` query
parameters). It performs no live research and changes no data.

Each assignment carries a stable content-derived id, query, market,
county, five-city municipality priority list, category, strategy,
priority, research intent, planner version (`004B-2.0`), downtown
flag, depth, and parent id. Same inputs always yield the same
assignments in the same order.

### Supported search strategies

`for_sale`, `high_value`, `distressed`, `foreclosure_reo`,
`receivership_bankruptcy`, `auction_liquidation`,
`vacant_underutilized`, `price_reduced`, `long_listing`. Every query
seeks actual sale opportunities across the three primary categories in
sequential county-major allocation (12 each by default, 36 total),
with the baseline prioritizing `for_sale`. A query's
wording is never evidence that a property is distressed; intents say so
explicitly.

### Twelve-county coverage (B5-1 expansion)

County markets derive from the shared `geography.py` constants used by
the ranking engine, so planner territory can never drift from ranking
eligibility. Counties process sequentially in priority order — Dallas,
Tarrant, Collin, Denton, Ellis, Johnson, Kaufman, Parker, Rockwall,
Hunt, Wise, Navarro — with all three categories completing in one
county before advancing, so the default plan is exactly 36 baseline
assignments (3 categories x 12 counties) prioritizing properties
advertised for sale. Each assignment carries its county's five-city
municipality priority list (county context preserved per county);
baseline queries stay countywide. Set
`include_secondary_counties=false` for a Dallas-only plan.
Downtown is an opt-in refinement of Dallas County slots only, using
the explicitly configured municipality (`Downtown Dallas, TX` by
default); county-based downtown labels are never generated.

Because each research run executes one assignment per category, later
explicitly requested runs pass `rotation: 1` through `11` in
`POST /discovery/research-runs` to sweep the remaining counties —
twelve rotations cover all twelve counties with no run wider
than 3 Brave calls and no automatic recursion.

### Example research assignments

```json
[
  {"query": "high-rise office building for sale in Dallas County, TX",
   "category": "high_rise", "strategy": "for_sale", "priority": 1},
  {"query": "commercial office building for sale in Dallas County, TX",
   "category": "commercial_property", "strategy": "for_sale", "priority": 2},
  {"query": "industrial warehouse property for sale in Dallas County, TX",
   "category": "warehouse_industrial", "strategy": "for_sale", "priority": 3},
  {"query": "high-rise office building for sale in Tarrant County, TX",
   "category": "high_rise", "strategy": "for_sale", "priority": 4}
]
```

### Configuration and budget controls

`PlannerConfig`: `total_max_queries` (1–54, default 36),
`per_category_max` (1–18, default 12), `followup_max` (0–50, default 6),
`max_depth` (0–5, default 2), `downtown_refinement` (default false),
`include_secondary_counties` (default true).
Invalid budgets are rejected. Follow-ups derive deterministically from
feedback outcomes (`no_results`, `duplicates_heavy`, `promising`,
`provider_failure`, `missing_pricing`, `missing_distress_evidence`):
provider failures generate zero queries (no automatic retry), depth and
follow-up caps bound growth, and known queries are never repeated.

### Limitations

- No property values are invented and no minimum price is imposed; the
  planner ranks nothing because it has seen no listings.
- Geographic scope is the twelve-county operating area, with no grids,
  radius expansion, or boundary processing.
- Category treatment is equal across the three primary categories; no
  category receives an automatic scoring or scheduling advantage.

### Remaining MVP-004B phases

Provider execution, intake ingestion, ranking, investigation, and dossier
building have since landed (see below). Still unfinished: per-city search
ordering across the five priority cities per county (baseline queries stay
countywide), batch/city scheduling, autonomous stop-at-144 execution, and
quota allocation.

---

## 🔎 Gabriel Brave Research (MVP-004B-2)

Gabriel executes planner assignments against the Brave Search Web API
and returns attributable results (title, URL, snippet, domain, rank,
timestamps). No valuation, no distress verification, and no operational
property writes in this phase.

### Brave API configuration

Set `BRAVE_SEARCH_API_KEY` in the environment (see `.env.example`); the
server sends it as the `X-Subscription-Token` header to
`https://api.search.brave.com/res/v1/web/search`. Requests to the
research endpoint itself require the `X-Research-Key` header matching
`RESEARCH_API_KEY`. Never commit real credentials; keys never appear in
logs, errors, or responses.

### Research execution workflow

1. Generate assignments with `GET /discovery/search-plan`.
2. Submit them to `POST /discovery/research-runs` with the research key.
3. The service selects one assignment per primary category in priority
   order (max 3), reserves budget, executes in order, and returns
   normalized results with per-assignment errors.
4. For broader coverage, resubmit the same plan with `rotation: 1`
   (then `2` through `9`) to execute the next county's triple —
   sweeping all ten counties across explicitly requested runs.

Only planner-issued assignments (matching planner version) are accepted;
arbitrary caller queries are rejected. Dependent research options are
validated before budget reservation or provider calls, so a request for
ranking without intelligence fails before spending anything.

The coordinated MVP additionally persists each run's acquired evidence as
research artifacts (provider, assignment/query, source URLs, dates, actual
collection provenance, digest) in the workflow store, and records ambiguous
provider attempts explicitly instead of silently repeating potentially
charged calls after a crash.

### Request limits and monthly budget

- Max 3 Brave requests per run; no recursion.
- Monthly ceiling from `BRAVE_MONTHLY_CEILING` (default 100), tracked per
  UTC calendar month in the `research_budget` table.
- Exactly one request is reserved atomically immediately before each Brave
  call; attempts count even on timeout or provider error. Skipped
  assignments consume nothing, and the run stops immediately with a
  `budget_exhausted` error entry when the month is spent.
- Rate limits (429) and auth failures (401/403) abort the rest of the run.
- The MVP workflow enforces a task/run bound (default 9 Brave attempts;
  up to 18 only with explicit reacquisition approval) across month changes,
  alongside the monthly ceiling. These are ceilings, not spending
  authorization: live calls need explicit owner approval.

### Security considerations

Brave calls use bounded timeouts and no scraping. Missing Brave or
research credentials fail closed (`503`); bad research headers return
`401`. No credits are ever purchased automatically.

### Known limitations

- Results are web snippets, not verified listings; prices, vacancy, and
  distress must still flow through intake evidence and Raphael review.
- Budget rows record the ceiling in effect at creation for audit, while
  enforcement uses the live configured ceiling.
- True concurrent-reservation proof needs live PostgreSQL (guaranteed by
  single-statement conditional UPDATE under row locking).

### Live testing instructions

With `BRAVE_SEARCH_API_KEY`, `RESEARCH_API_KEY`, and `DATABASE_*` set and
migrations applied:

```bash
# 1. Preview the plan, then submit it for a 3-request research run.
curl "http://127.0.0.1:8001/discovery/search-plan?total=3&per_category=1" > plan.json
curl -X POST http://127.0.0.1:8001/discovery/research-runs \
  -H 'Content-Type: application/json' \
  -H "X-Research-Key: $RESEARCH_API_KEY" \
  -d "{\"assignments\": $(cat plan.json)}"

# 2. Confirm budget accounting decreased by the reported request_count.
# 3. Confirm no new rows in properties, listing_evidence, or distress_observations.

The live commands above were not executed during the coordinated-MVP task
(no external-spend authorization); research behavior is covered by mocked
provider tests plus the persisted-artifact and budget tests.

---

## 🧠 Listing Intelligence Extractor (MVP-004B-3)

The extractor (`app/agents/gabriel/extractor.py`) turns raw Brave results
into structured, evidence-backed research intelligence using deterministic
rules only: no AI calls, no network requests, no database writes. It plugs
into the existing flow as Search Planner → Brave Search → Intelligence
Extractor → (future) Evidence Intake, complementing intake without
duplicating or bypassing it.

### What it extracts

- **Classification**: `property_listing`, `property_lead`,
  `listing_directory`, `news_article`, `legal_or_public_record`,
  `background_information`, or `unknown` — each with an auditable reason.
  Directories, historical articles, and generic leads are never mistaken
  for current sale offerings; ambiguity falls back to `unknown`.
- **Property identity**: conservatively matched names, street addresses,
  and state-anchored cities (Farmers Branch is not Dallas proper).
  Multiple buildings in one result stay separate; attributes are never
  merged across properties.
- **Money**: explicit dollar amounts classified as current/historical
  asking, completed sale, proposed transaction, estimated value, price
  per square foot, or unclassified. Historical amounts never become
  current asking prices; ambiguous amounts stay unclassified.
- **Distress vs underuse**: separate signal vocabularies. Bankruptcy,
  foreclosure, receivership, default, REO, auctions, price cuts, and
  stale listings indicate distress; low occupancy, high vacancy,
  unleased space, and tenant departures indicate underuse. Underuse is
  never auto-classified as distress.
- **Occupancy safeguards**: reported percentages kept with period, scope
  (whole building vs partial), and evidence. Leased ≠ physical, available
  ≠ vacant, historical ≠ current; unknown stays unknown, never zero.
- **Temporal awareness**: publication dates resolved against retrieval
  time; old or undatable material is historical/undetermined, never
  presented as current availability.
- **Provenance**: every observation keeps assignment id, query, URL,
  domain, evidence excerpt, timestamps, method (`rule-based-004B-3.0`),
  rationale, and status (`reported_unverified`/`unknown` — never
  verified, never estimated, no fabricated confidence).

HTML markup and entities in snippets are normalized to clean text
without rendering. Conservative grouping by exact address or name+city
flags ambiguous matches for review while keeping observations separate.

Identity comparison uses one narrow Texas-only state rule
(`normalize_state`): equivalent "TX"/"Texas" spellings canonicalize
to `tx` in group keys, dossier identity, dedup keys, intake locks,
dispute comparison, and operational-property association (including
the SQL side of `find_by_address`); every other value passes
through unchanged, raw source wording is preserved on the
observations, and suites, buildings, and distinct addresses are
never merged by it. Location fields are attributed only within a
candidate's own reliable text context: an address match owns its
trailing segment up to the next address (a lone address also admits
bounded leading context), so a neighbor's ZIP, city, category, or
designator is never borrowed — 8702 S Lancaster Rd keeps 75241
even beside a 75212 neighbor. Shared or leading locations across
several addresses are ambiguous and stay unknown, as do names
beside other candidates; a lone name keeps combined-wide context.
Money and occupancy stay result-level and unattributed, as before.

### API behavior

`POST /discovery/research-runs` accepts `include_intelligence` (default
true) and returns an optional `intelligence` section alongside the
unchanged existing fields. Extraction runs over already-retrieved
results: zero additional Brave calls, zero property writes, zero intake
submissions. No migration required; off-market discovery is explicitly
out of scope but the intelligence model is sale-status independent.

---

## 🏆 Gabriel Ranking & Quality Control (MVP-004B-4)

The ranking engine (`app/agents/gabriel/ranking.py`) turns extractor
intelligence into two independent, explainable rankings plus preliminary
quality control. Pure deterministic logic: no API calls, no database
writes, no AI calls.

### Two independent ranking systems

- **Property value ranking** orders candidates by attributed current
  asking price, largest first. Asking price is never a market valuation;
  historical, proposed, and per-square-foot figures never qualify;
  unpriced candidates are listed as unpriced, never scored.
- **Grow Up opportunity ranking** prioritizes conversion-research
  candidates with weighted factors: property value 30%, financial
  distress 25%, underutilization 20%, building characteristics 15%,
  evidence quality 10% (initial heuristics, not investment criteria).
  Sale availability is not required, so the engine reuses cleanly for
  future off-market discovery.

### Twelve-county operating area and downtown preference (corrected in MVP-004B-5, expanded in B5-1)

The research territory is Dallas County (primary) plus eleven secondary
counties — Tarrant, Collin, Denton, Ellis, Johnson, Kaufman, Parker,
Rockwall, Hunt, Wise, Navarro. County classification
(`primary_area`/`secondary_area`/`county_unknown`/`outside_operating_area`)
rests on explicit county evidence only: no municipality lookup, no ZIP
inference, no fabrication. A small transparent downtown bonus (+0.05,
any operating-area municipality, evidence-backed or unknown) breaks
ties but never overwhelms stronger opportunity evidence.

### Quality control and missing data

Gates (not scores) exclude only clear cases: demolished,
vacant-land-without-structure, and incompatible-use — each with an
auditable rule, evidence, source, and date. Properties reliably outside
all twelve counties are deferred (evidence retained, out of the active
queue), not discarded. Everything uncertain becomes
RESEARCH_REQUIRED with agent-routed next steps; high occupancy, missing
prices, and off-market status never exclude. Unknown factors are left
unevaluated (reducing a reported completeness score) instead of scoring
zero; candidates below 50% completeness are listed as insufficient data
rather than ranked.

### Attribution and temporal rules

Price, occupancy, distress, and county facts attach to a property only
from single-property results; multi-property observations stay
unresolved and force further research. Repeated sources never count as
corroboration; distinct hosts with nonidentical snippets establish
multi-host agreement, not authenticated publisher independence.
Historical evidence never establishes current status.

### API fields

`POST /discovery/research-runs` accepts `include_ranking` (default
false) and returns an additive `ranking` section with candidates,
value/opportunity orders, unpriced and insufficient-data lists, screened
leads, factor explanations, and exclusions. Ranking requires
`include_intelligence`; existing fields and behavior are unchanged.
Raphael (inspection), Onesimus (engineering), and Priscilla (finance)
conclusions are never fabricated — only routed as next steps.
```

### Environment requirements

MVP-002 adds no new environment variables. It reuses the MVP-001 PostgreSQL
settings (`DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`,
`DATABASE_PASSWORD`); see `.env.example`. No AI inference, authentication, or
external property APIs are introduced.

### Verification limitations

The MVP-002 evidence gate is structural: it requires substantive,
non-placeholder, non-synthetic provenance for sourced values and per-field
evidence references for verified values, with PostgreSQL constraints as a
backstop. A client-provided "verified" label or evidence reference does not
independently establish truth: the gate cannot confirm cited facts against
external sources. A human review workflow, trusted-source registry, and
separate hypothetical-scenario storage are deferred to future milestones.

Three related but distinct gates now exist: **research qualification** (a
deterministic Gabriel-side eligibility screen over a persisted dossier),
**verification** (the shared category-evidence policy — paired voices,
category-specific defining facts, multi-host agreement), and **Raphael
authorization** (which additionally requires reviewed registry curation,
explicit identity alignment, and a persisted dossier version/digest).
Failing any gate stops the real-property path fail-closed.

---

## 🕵️ Gabriel Investigative Research (MVP-004B-5)

MVP-004B-5 extends Gabriel from discovery and ranking into coordinated
investigation: plan targeted follow-ups, execute them within budget,
and consolidate multi-round evidence into property dossiers.

**Pipeline:** Search Planner → Brave Search → Intelligence Extractor →
Quality Control → Value & Opportunity Ranking → Investigation Planner →
Bounded Executor → Evidence Dossier Builder → Ranking Reassessment →
Agent Handoff Recommendations.

### Geographic correction

MVP-004B-4 excluded everything outside Dallas County. That logic is
corrected: all seven operating-area counties are eligible, unknown
county stays eligible, and confirmed out-of-area leads are deferred
with evidence retained. **Compatibility note:** the public
`county` values `inside_county`/`outside_county` were replaced by
`primary_area`/`secondary_area`/`outside_operating_area`
(`county_unknown` unchanged), and a `deferred` QC status plus
`deferrals` records were added. Ranking version is now `004B-5.0`.

### 5A — Investigation planner (pure)

`app/agents/gabriel/investigation.py` (`plan_investigations`) derives
research questions (identity, geography, transaction, distress,
underuse, building, evidence) and targeted Brave-compatible queries
from ranked intelligence. Prioritization blends opportunity score with
a small primary-territory weight. Queries use supported names or
addresses only — weak identity yields questions, never blind searches —
with sanitized, length-capped, deduplicated templates. Defaults: 3
properties, 2 queries each, 6 total, depth 2, 1 iteration. No provider
calls, no database writes.

### 5B — Investigation executor (budgeted)

`run_investigation` regenerates the plan server-side from submitted
intelligence (deterministic reconstruction), so forged query text has
no path to the provider. It reuses the existing Brave provider,
`RESEARCH_API_KEY` authentication, and PostgreSQL monthly budget: one
reservation immediately before each call, max 3 queries per run,
attempts count even on failure, 429/401 abort remaining queries, no
recursion, no property writes.

### 5C — Evidence dossier builder (pure)

`app/agents/gabriel/dossier.py` (`build_dossiers`) merges
initial + follow-up rounds into one dossier per conservatively
identified property: supported vs. historical facts, distress/underuse
findings, multi-host-agreement vs. repeated-source corroboration,
temporal-aware contradictions (same-period conflicts flagged;
cross-year changes noted, not flagged), missing information, actions
performed, remaining questions, Raphael/Onesimus/Priscilla handoffs,
and a deterministic summary built only from supported evidence.
Reassessment reuses the existing ranking engine; optional
prior-ranking comparison explains score changes without inventing
priors.

The coordinated MVP persists complete dossier versions (input artifact
references, claims, conflicts, missing information, builder version,
digest, resolved property UUID) in the workflow store. Raphael loads
these server-owned versions — never caller-supplied dossiers — and
authorizes an exact version/digest; revised evidence invalidates reuse
of an old authorization.

### API endpoints

- `POST /discovery/investigation-plans` — pure planning (no auth; spends
  nothing). Body: `{intelligence, ranking?, config?, depth?, iteration?}`.
- `POST /discovery/investigations` — authenticated (`X-Research-Key`),
  budgeted follow-up execution. Body: `{intelligence, config?,
  query_ids?, timeout_seconds?}`; unknown query IDs are rejected.
- `POST /discovery/dossiers` — authenticated (`X-Research-Key`),
  pure consolidation (spends nothing). Body:
  `{rounds[1..10], prior_ranking?, executed_queries?}` (≤200 results).

No database migration required. No live Brave calls during tests; all
provider and budget interactions are mocked.

---

## 📡 Shared Agent Observability (MVP-004C-A, Phase 1)

Foundation for all seven agents: one typed event model, one emitter
API, a JSONL diagnostic trace, and a PostgreSQL activity index. Agents
never choose sinks — the event's retention class routes it.

### Event model and emitter

`app/observability/` exposes `AgentEvent` (ids, UTC timestamps, agent,
run/correlation/property ids, stable `event_type`, severity, summary,
bounded details, evidence refs, parent id, retention class) and
`emit()` plus helpers: `log_diagnostic`, `log_activity`,
`log_decision`, `log_evidence`, `log_handoff`, `log_warning`,
`log_error`. Routing: diagnostic → trace file; activity → file +
PostgreSQL; permanent (decisions, evidence, handoffs) → PostgreSQL +
file, with PostgreSQL as the durable copy.

### Durability and failure contract

File failures never raise and never break business operations (counted
in `get_status()`, echoed to stderr). Activity PG failures degrade to
file-only. Permanent PG failures raise `EventPersistenceError` after a
best-effort file copy — never silent. Error paths never emit events
(no recursion); PG writes use dedicated short-lived connections, never
business transactions.

The coordinated MVP adds two permanent workflow events —
`wondrue.evaluation.completed` and `wondrue.evaluation.stopped` — emitted
through the consequential coordinator
(`app/observability/transactions.py`), so each business transition and its
audit record commit atomically in one transaction. Outbox acceptance alone
never authorizes engineering work. No new observability configuration or
deployment steps are required.

### Retention

Diagnostic retention is a **maximum age, not a guaranteed minimum**:
records older than `OBS_DIAGNOSTIC_RETENTION_DAYS` (default 90) may be
purged, but size-based rotation (10MB × 10 backups by default) can
remove records earlier under heavy logging — 90 days of history is
never promised. Durable history lives in PostgreSQL: routine activity
rows default to 365 days (`OBS_ACTIVITY_RETENTION_DAYS`), while
permanent rows (decisions, evidence references, actual handoffs) are
never auto-deleted; cleanup SQL carries an explicit
`retention_class != 'permanent'` guard. File purge only matches the
sink's own rotated filenames. No automated cleanup scheduler exists;
purges are explicit function calls.

### Concurrency and permissions

Multiple threads within the one application process are safe (logging
locks keep each JSONL record intact). Multiple independent processes
must not share one rotating file; the deployment runs a single Uvicorn
worker and that architecture is preserved. Newly created log files are
chmodded to `640` (owner rw, group r); pre-existing files are never
loosened, and rotated siblings keep their mode via rename.

### Security

Centralized redaction runs on every event before any sink: secret-like
field names (keys, tokens, passwords, auth headers, cookies) plus
`Bearer` and `sk-` value patterns. Summaries are collapsed to one line;
control characters stripped (including NUL, which PostgreSQL rejects);
details capped (8KB default, truncated with marker); no full
request/response logging. No observability endpoints exist in this
phase — queries are Python-level only, for Wondrue's future controlled
read-only interface (`recent_by_agent`, `by_property`, `by_run`,
`by_correlation`, `decisions`, `handoffs`).

### Configuration and deployment

All `OBS_*` variables are optional (see `.env.example`); invalid values
fail closed to defaults. Migration `004_agent_events.sql` applies via
`python -m app.migrate`. Ubuntu prerequisite: set
`OBS_LOG_DIR=/var/www/growup/logs` in `/var/www/growup/shared/growup.env`
(the authoritative environment file) and pre-create that directory owned
by `growup` (releases are immutable, so logs live outside them).
Research payload capture/replay is deferred to MVP-004C-B.

### Phase 2 — Gabriel instrumentation

Gabriel's research, investigation, ranking, dossier, and intake flows
emit through `app/agents/gabriel/observe.py`, a preset over the shared
emitter (agent fixed to `gabriel`, decisions via `emit_nonfatal`).
Requests accept an optional `correlation_id`; every execution mints a
`run_id`, echoed back on the response. Event types:

- Research: `gabriel.research.started/selection_failed/assignments_selected/results_received/completed`,
  `gabriel.brave.request_attempted/completed/failed`, `gabriel.budget.exhausted`,
  `gabriel.extraction.completed` (counts only), `gabriel.ranking.completed`
  (heuristic summary), `gabriel.research.candidate_decision` (permanent,
  one per final ranked candidate; replaces `gabriel.ranking.excluded`,
  whose historical rows remain queryable).
- Investigation: `gabriel.investigation.plan_created/plan_resolved/execution_started/completed/interrupted/selection_failed`.
- Dossier: `gabriel.dossier.started/completed/handoff_ready`
  (`handoff_ready` explicitly means suggested, not sent; no
  `handoff_sent` exists yet because no agent messaging does),
  `gabriel.dossier.verdict` (permanent, one per final dossier;
  replaces `gabriel.dossier.excluded`, whose historical rows remain
  queryable).
- Intake: `gabriel.intake.accepted/rejected` (field names, counts, and
  listing URL only — never the full submission).

`property_id` appears only when reliably known (intake outcomes);
research candidates use identity keys, never invented UUIDs.
Diagnostic/activity boundaries are nonfatal: sink failures degrade
silently-safe via `emit_nonfatal`, and validation errors propagate
as code bugs. Research/dossier verdict batches instead fail closed
via `require_durable_batch` (atomic SQLite acceptance plus
idempotent PostgreSQL delivery; total persistence failure returns
HTTP 503 rather than an unaudited success). `emit_nonfatal`
returns an `EmitOutcome` distinguishing PostgreSQL, outbox,
file-only, degraded, and failed results (see
Phase 3B for the durable path). Gabriel research keeps the legacy
nonfatal path without an outbox. No extra Brave calls, no
ranking/geography changes, no new endpoints or migrations.

### Phase 3A — Durable SQLite outbox (storage foundation)

`app/observability/outbox.py` (`Outbox`) is the first stage of durable
event recovery: a transactional SQLite store for permanent events that
survives application restarts when PostgreSQL is unavailable. Phase 3B
connects it to the emitter (`emit_nonfatal(..., outbox=...)`,
`require_durable()`) and adds the recovery worker below; Gabriel
research itself keeps the legacy nonfatal path.

- **Configuration:** `OBS_OUTBOX_PATH` (default
  `outbox/events-outbox.sqlite3` for development). Ubuntu:
  `/var/www/growup/shared/observability/events-outbox.sqlite3`,
  outside versioned releases. The module never creates the database on
  import; schema setup is an explicit `initialize()` call. Production
  use requires an explicitly configured absolute path outside the
  release tree: `Outbox(..., production=True)` (which the Phase 3B
  worker must use) rejects relative paths and anything resolving
  through symlinks under `/var/www/growup/releases/` or
  `/var/www/growup/current/`, failing closed with `OutboxConfigError`.

### Phase 3B — Recovery worker and fail-closed durability

Permanent events take the outbox-first path when an outbox is
supplied: file trace copy → SQLite commit → immediate PostgreSQL
delivery → mark delivered. `EmitOutcome` distinguishes
`persisted_pg`, `persisted_outbox`, `file_copy`, `degraded`
(outbox-durable, PostgreSQL pending), and `failed` (nowhere durable).
JSONL is never durability. `require_durable()` raises
`DurabilityError` when neither durable store accepts the event —
consequential operations must abort on it. The legacy helpers
(`emit`, `log_decision`/`log_evidence`/`log_handoff`, `emit_nonfatal`
without an outbox) remain PostgreSQL-only and never touch the outbox;
only `require_durable` with an explicitly supplied outbox takes the
durable outbox-first path. Gabriel research keeps the legacy nonfatal
path (no outbox); no current workflow is wired fail-closed, and none
was invented for demonstration.

- **Recovery:** `python -m app.observability.recovery` runs one
  bounded cycle (≤100 events): deliver idempotently by `event_id`
  (`inserted`/`already_present`/`conflict`; conflicts never
  overwrite), mark only after confirmation, continue past individual
  failures, back off retries (30s exponential, 1h cap), then purge
  deliveries older than `OBS_DELIVERED_RETENTION_DAYS` (default 30;
  pending rows can never match). Crash-safe: redelivery after a
  restart confirms idempotently. Prints one metrics line
  (`status/pending/attempted/delivered/already/failed/conflicts/`
  `retries/purged/oldest_age_s`); exit 0 ok, 1 partial, 2 bad config,
  3 nothing delivered (events stay pending/retryable), 4 outbox
  unreachable. No FastAPI import, no Brave calls, no business reruns.
- **Scheduling:** `deployment/growup-outbox-recovery.{service,timer}`
  run the cycle 30s after each previous run completes
  (`OnUnitInactiveSec`, not a fixed wall-clock cadence) as
  `growup:growup` with `flock` overlap prevention and hardening that
  preserves SQLite/log/loopback-PostgreSQL access. The timer is NOT
  enabled by deployment; see
  `deployment/README-DEPLOYMENT.md` for manual install, activation,
  verification, and rollback. Never activate against a second
  environment sharing one outbox file.
- **Guarantees and limits:** a returned durable outcome means the
  event is committed in SQLite and/or PostgreSQL; a crash cannot
  strand an accepted event untracked. This is NOT transactional
  consistency with business tables or external systems — future
  consequential workflows need coordinated state transitions,
  idempotency keys, and recovery design. SQLite commits depend on
  filesystem reliability, not physical disk survival. Manual recovery:
  inspect pending rows (`count_pending`, `pending`), fix PostgreSQL
  or the outbox path, and let the timer drain; conflicts
  (`pg_content_conflict`) need human comparison of the two payloads
  before deleting either side.
- **Schema:** one row per event (`event_id` primary key, complete
  validated/redacted JSON payload, event + enqueue timestamps,
  `pending`/`delivered` status, attempt count, last attempt/error,
  next retry time), with a pending-retrieval index.
- **Durability:** WAL mode, `synchronous=FULL`, busy timeout, explicit
  transactions. `accept()` returns only after commit; any failure
  raises and the event is NOT accepted. Commits depend on filesystem
  reliability — SQLite does not survive physical disk failure.
- **Identity:** the existing `event_id`; identical resubmission is an
  idempotent no-op, conflicting payloads raise `OutboxConflictError`.
  Nonpermanent events are rejected. Payloads are re-validated and
  re-scrubbed on accept.
- **Security:** created directories `0700`, database and WAL sidecars
  `0600`, service-account owned; pre-existing admin paths are never
  altered; error categories are short codes, never tracebacks.
- **Lifecycle:** `accept` → `pending` (retry-due, oldest first) →
  `mark_delivered` / `record_attempt` (attempt count + next retry).
  The Phase 3B worker forwards pending rows to PostgreSQL and marks
  them delivered; delivered rows older than the retention window are
  purged, pending rows are never purged.

### Phase 3C-1 — Operation classification and durability enforcement

`app/observability/policy.py` is the enforcement contract for future
consequential workflows. It classifies operations explicitly (enum
members, never name matching) and routes audit events through the
existing emitter with class-appropriate failure handling:

- **PRELIMINARY** (discovery, intake, enrichment, research,
  planning, dossiers): `emit_nonfatal` — audit failures never raise.
  Validation, provenance, and existing persistence failures keep
  their current behavior.
- **CONSEQUENTIAL_INTERNAL** (approvals, commitments, deal-state
  changes): `require_durable` — `DurabilityError` unless PostgreSQL
  or the outbox accepts the audit event.
- **CONSEQUENTIAL_EXTERNAL** (outreach, contracts, external
  instructions): same fail-closed rule; durable authorization must
  precede execution.

Future agents invoke it as
`audit_event(event, OperationClass.CONSEQUENTIAL_INTERNAL, outbox=...)`
and abort on `DurabilityError`. The interface only requests audit
acceptance — it executes and mutates nothing. Durable acceptance is
NOT atomicity with a business transaction: same-transaction
business+audit PG commits are deferred to Phase 3C-2, and the
authorization/attempt/outcome lifecycle with idempotency for
external actions to Phase 3C-3. No existing workflow calls this
module; Gabriel stays on the legacy nonfatal path.

### Phase 3C-2 — Transaction-safe consequential auditing

`app/observability/transactions.py::run_consequential_transaction`
commits one PostgreSQL business mutation and its permanent audit
event atomically: both commit on one coordinator-owned connection,
or both roll back. `store.insert_event_in_transaction(conn, event)`
is the join-the-transaction primitive — it executes the audit INSERT
on the supplied connection and never commits, rolls back, opens
another connection, or touches SQLite/JSONL.

- **Durable acceptance vs atomicity:** Phase 3C-1 proves an audit
  event reached a durable store; it says nothing about whether a
  separate business write committed. Phase 3C-2 proves business and
  audit committed together — or neither did.
- **When to use which:** standalone audit records (decisions already
  made, handoffs, evidence notes) use 3C-1 `audit_event`. A
  consequential PostgreSQL state change whose audit must be
  inseparable from the change uses 3C-2.
- **Why SQLite can't substitute:** the outbox lives outside
  PostgreSQL, so accepting an audit there can never join the
  business transaction. On PostgreSQL failure the coordinator fails
  closed instead of pretending an outbox row is atomic with a
  rolled-back mutation.
- **Callback contract:** `mutate(conn)` receives the open
  connection and must only write through it — no commit, rollback,
  close, replace, stash, or side transactions. The coordinator
  cannot prevent a misbehaving callback from touching its own
  connection object, so misuse voids the guarantee by definition.
- **Still deferred (3C-3):** for external effects, 3C-2 commits the
  audit record only. Distinguishing durable authorization from
  attempted execution and confirmed outcome, with idempotency and
  ambiguous-failure recovery, remains Phase 3C-3 work.

No production consequential workflows exist yet; Gabriel's paths are
unchanged. Two PostgreSQL-backed integration tests prove rollback
and commit end to end but skip unless `GROWUP_PG_INTEGRATION=1`
with a configured database.

### Phase 3C-3A — External-action ledger foundation

`migrations/005_external_actions.sql` adds the `external_actions`
ledger; `app/external_actions_db.py::record_authorization` is its
internal-only writer. The ledger records durable AUTHORIZATION of
consequential external actions — it never executes them.

- **Authorization vs execution:** a ledger row proves an action was
  authorized, never that it ran. Creation (`AUTHORIZED`) is the only
  business operation; the six-state transition policy
  (`AUTHORIZED → PENDING → ATTEMPTING → SUCCEEDED/FAILED/UNKNOWN`)
  is defined and validated but has no transition writer yet.
  `UNKNOWN` is terminal-until-reconciled: requeueing is prohibited
  because the action may already have executed, so the only exits
  are verified reconciliation to `SUCCEEDED`/`FAILED`.
- **Idempotency contract:** each authorization carries a unique
  `idempotency_key` plus a `request_fingerprint` (`sha256:` hash
  over canonical type/agent/property/request plus the authorizing
  principal and reference). Same key + identical request,
  principal, and reference replays the existing action with no
  duplicate audit; any difference raises `IdempotencyConflictError`,
  so one principal's submission can never silently replay
  another's. Safety comes from the PostgreSQL UNIQUE constraint
  and `ON CONFLICT DO NOTHING`, never check-then-insert. The raw
  request is never stored — only its hash.
- **Atomic audit:** a new authorization and its permanent
  `external_action.authorized` audit event commit in one PostgreSQL
  transaction (via `insert_event_in_transaction`); audit failure
  rolls the authorization back.
- **Principal-verification limit:** `authorized_by` is a CLAIMED
  principal string — no authenticated-principal verification exists
  yet, so this interface must not be exposed through any API route
  or presented as a fully secure authorization system. The claimed
  principal is bound into the fingerprint (a different principal
  conflicts instead of replaying), but keys must still be
  unguessable per authorization, since key squatting can force a
  legitimate submission to conflict.
- **Why SQLite can't authorize:** an outbox row cannot join the
  PostgreSQL authorization transaction, so on PostgreSQL failure
  the call fails closed instead of recording authorization
  anywhere else.
- **Deferred to 3C-3B:** execution workers, attempt/outcome
  recording, retries, `attempt_count` advancement, UNKNOWN
  resolution (reconciliation to a definitive outcome requires
  independently verified evidence, never a re-attempt), retry of
  FAILED actions (requires explicit retry authorization plus proof
  of definitive failure), and verified principal binding.

Six PostgreSQL-backed integration tests (atomicity, replay,
conflict, rollback, CHECK constraints, property-delete history
preservation) skip unless `GROWUP_PG_INTEGRATION=1` with migration
005 applied.

### Phase 3C-3B-1 — Transaction-safe external-action lifecycle

`app/external_actions_db.py` gains `claim_action` and
`transition_action`; `migrations/006_external_action_lifecycle.sql`
adds the append-oriented `external_action_attempts` table (one row
per claim: attempt id, action, 1-based number, claimed-at,
executor, outcome + recorded-at with a pairing CHECK, unique
per action). This phase is database safety infrastructure only —
no external requests, workers, retries, or routes.

- **Atomic transitions:** every state change locks its action row,
  validates the policy transition, updates the ledger, and inserts
  a permanent audit event in one PostgreSQL transaction; either
  side failing rolls both back. No SQLite/JSONL fallback.
- **Atomic claiming:** `claim_action` is one conditional
  `UPDATE … WHERE state = 'PENDING'`, so concurrent workers
  serialize on the row and exactly one wins. The winner gets its
  attempt row (numbered from the atomically incremented
  `attempt_count`) and claim audit in the same commit; losers get
  `claimed=False` and no authorization. A claim permits a FUTURE
  execution phase — nothing executes here.
- **Outcomes:** `ATTEMPTING → SUCCEEDED/FAILED/UNKNOWN` requires
  the currently open attempt id; unknown, mismatched, superseded,
  or already-closed attempts raise `StaleAttemptError`. Timeouts
  are never interpreted — callers record what they observed.
- **GRW-EXT-001 (no automatic re-execution):** enforced in code.
  `ATTEMPTING` exits only via recorded outcomes, `UNKNOWN` has no
  executable exits, `FAILED` has no requeue path, and
  `PENDING → ATTEMPTING` exists only through `claim_action`.
- **Reconciliation boundary:** the policy still lists
  `PENDING → FAILED`, `UNKNOWN → SUCCEEDED/FAILED`, and
  `FAILED → PENDING`, but the executing layer withholds them
  (`DeferredTransitionError`). `FAILED` records an
  execution-attempt outcome only, so pre-execution
  cancellation/rejection needs a separately designed protocol;
  UNKNOWN exits will require independently verified reconciliation
  evidence; FAILED retry will require an explicit retry protocol.
- **Deferred:** verified executor/principal binding, reconciliation
  execution, the retry and cancellation protocols, actual external
  execution, and any worker or scheduler.

Five PostgreSQL-backed integration tests (full lifecycle, threaded
single-winner claim, claim rollback, UNKNOWN freeze, attempt
constraints) skip unless `GROWUP_PG_INTEGRATION=1` with migrations
005–006 applied.

### Phase 3C-3B-2A — Verified authorization and executor identity

`app/identity.py` is the authentication + policy boundary in front of
the ledger. Bare identity strings no longer confer anything:
`record_authorization` and `claim_action` verify proofs themselves
and derive the stored `authorized_by` / `executor_id` server-side
before any database mutation. No migration, no routes, no workers.

- **Humans (asymmetric):** each approval carries an Ed25519
  signature over the exact idempotency key, request fingerprint,
  principal, reference, issuance, and expiry. Operator private keys
  live offline with the human — never in the app environment —
  while `AUTHORIZER_PUBLIC_KEYS` holds enrolled `name:hex-pubkey`
  verification material (not secret). Because minting needs a key
  the process never holds, human approvals are unforgeable even
  in-process. Lifetime rules: both timestamps timezone-aware,
  expiry strictly after issuance, signed lifetime ≤ 24h, issuance
  no more than 60s in the future, expiry enforced with a 60s skew
  allowance for signer/verifier clock difference (not scheduling).
  Tampering with either timestamp invalidates the signature, and a
  grant binds a single authorization, so capture buys nothing else.
- **Executors (symmetric service credentials):** `EXECUTOR_KEYS`
  holds `id:secret` pairs verified with constant-time compare
  (dummy compare for unknown ids). These are reusable bearer
  secrets: they prove which enrolled service calls, but do not
  independently authorize any specific action type (per-type
  scopes arrived in Phase 3C-3B-2B below); executor isolation and
  execution safety remain prerequisites for real external actions.
- **Policy separation:** authentication answers *who*
  (`AuthenticationFailedError`); `ensure_may_authorize` /
  `ensure_may_claim` answer *may* (`AuthorizationDeniedError`) —
  humans authorize, executors claim, agents (credential-less) do
  neither. Principal objects are data representations, not
  credentials: `isinstance` checks establish type only, policy
  functions must receive verifier-produced instances, and the
  ledger verifies proofs itself — constructing a principal object
  cannot reach any ledger path.
- **Preserved:** idempotency (canonical lowercase identities keep
  fingerprint replays stable; a different verified principal still
  conflicts), atomic audit transactions, GRW-EXT-001, lifecycle
  restrictions, and the internal-only posture (tests fail if any
  route references the ledger or identity surface). Proofs and
  signatures never appear in audit details.
- **Trust-boundary honesty:** executor secrets live in the process
  environment like all service secrets here, so executor
  verification holds against external and credential-less callers
  but not against arbitrary malicious code inside this same
  process. Service-process separation (executor runs with its own
  env, app verifies) is the documented future hardening, alongside
  Wondrue-mediated human approvals replacing direct signature
  submission. Rotation is per-entry (replace key/secret + restart);
  revocation is entry removal.
- **Operator recipe (offline):** generate Ed25519 (`cryptography`;
  32-byte raw keys, hex), keep the private key in operator-only
  storage (0600 file or prompter — never the app env), enroll
  `name:hex-pubkey` in `AUTHORIZER_PUBLIC_KEYS`, and sign each
  approval with `sign_approval()` over the exact authorization
  fields. Tests generate throwaway keys at runtime; no real key
  material is committed anywhere.

### Phase 3C-3B-2B — Scoped enqueue, scoped claim, verified outcomes

Three gates, three identities: humans authorize WHAT (unchanged
Ed25519 approvals), orchestrators schedule WHEN (`AUTHORIZED →
PENDING`), executors claim and record outcomes. No migration, no
routes, no workers, no break-glass.

- **Orchestrator identity:** `ORCHESTRATOR_KEYS` holds
  `id:secret` pairs in a namespace separate from `EXECUTOR_KEYS`
  — an orchestrator credential never verifies as an executor and
  vice versa, even for identically spelled ids. Separation is by
  secret value, not by name: any value enrolled in both maps is
  rejected before either role can authenticate (neither role
  works until the overlap is removed), while identical names
  with distinct secrets remain permitted. Role policy
  (`may_enqueue` / `ensure_may_enqueue`) accepts only
  verifier-produced `OrchestratorPrincipal` values.
- **Exact-match scopes:** `EXECUTOR_SCOPES` and
  `ORCHESTRATOR_SCOPES` hold `id:type|type,...` grants matched
  byte-for-byte against the action's stored `action_type`
  (case-sensitive). No wildcards (a bare `*` is rejected at
  parse), no default-allow, no inference from agent names:
  missing, empty, malformed, or non-granting maps deny with
  `AuthorizationDeniedError`.
- **Scoped enqueue:** `transition_action(..., to_state="PENDING")`
  requires orchestrator credentials plus a scope grant for the
  actual type read from the locked row. Denial raises before
  anything is written; success records `enqueued_by` in the
  permanent transition audit.
- **Scoped claim:** `claim_action` authenticates, runs the
  conditional `UPDATE … WHERE state = 'PENDING'`, then checks
  the scope against the returned row's type BEFORE inserting the
  attempt row. A denial rolls the whole transaction back: the
  action stays `PENDING` with its original `attempt_count`, no
  attempt row, and no audit event.
- **Verified outcomes:** `ATTEMPTING → SUCCEEDED/FAILED/UNKNOWN`
  requires the claiming executor's credentials plus an exact
  match between the verified id and the open attempt's recorded
  `executor_id`. Stale, mismatched, closed, and foreign attempts
  still raise `StaleAttemptError`; non-owners raise
  `AuthorizationDeniedError`. Success records `recorded_by` in
  the permanent audit. There is deliberately no human
  break-glass or administrative override in this phase.
- **Credential rotation rule:** rotation must account for
  outstanding attempts. If credentials are revoked while an
  attempt is unresolved, the attempt stays unresolved for future
  human reconciliation — never automatically reassigned,
  replayed, or resolved.
- **Preserved:** the Ed25519 approval payload and lifetime rules,
  the state-transition matrix and all deferrals, GRW-EXT-001,
  idempotency, atomic audit transactions, and the internal-only
  posture (route and credential-confinement guards extended to
  the new surface; the ledger still defines no execution or
  retry surface). Proofs never appear in audit details.

Four PostgreSQL-backed integration tests (unauthorized enqueue,
denied-claim rollback with permitted reclaim, concurrent
permitted/denied claims, non-owner outcome denial) skip unless
`GROWUP_PG_INTEGRATION=1` with migrations 005–006 applied.

### Phase 3C-3B-3A — Read-only external-action recovery foundation

`app/external_action_reads.py` is a dedicated internal read module
over the existing ledger tables — SELECT-only, no commits, no
audit writes, no state changes, no execution. It does not import
the ledger mutation module. No migration, no routes, no workers.

- **Three read-access levels:** the orchestrator id named by
  `WONDRUE_READER_ID` whose credential verifies reads across
  action types (read-only by design, no state authority);
  other verified orchestrators read only types their
  `ORCHESTRATOR_SCOPES` grants cover (exact match, empty means
  nothing); verified executors read only their own attempts
  (SQL-side owner filter) and actions carrying them. Every
  check runs before any data returns; cross-role credentials
  fail by namespace separation.
- **Five internal functions:** `get_action` (one row or None),
  `list_attempts` (oldest-first, bounded), `list_own_open_attempts`
  (executor-only, with action state/type/count context),
  `find_stale_attempting` (open `ATTEMPTING` attempts older than
  the threshold, oldest first, with computed age), and
  `stale_summary` (bounded count plus oldest claim age, None —
  never zero — when empty). All listings are explicitly
  limited (default 50, clamped to 200); all SQL is parameterized.
- **Staleness is detection only:** the default 15-minute
  threshold flags actions that "may require investigation."
  Staleness never implies failure, never authorizes a retry,
  and never moves state — GRW-EXT-001 is preserved, and
  recovering a record authorizes no execution.
- **Boundaries:** no reconciliation writes, no transitions, no
  Wondrue conversational interface (this phase establishes only
  the authorization and read interfaces she will consume), and
  no public routes (route-surface guards extended to the new
  module). Structural tests pin the SELECT-only posture and the
  mutation-module import ban.

Four PostgreSQL-backed integration tests (system-wide vs scoped
reads, executor ownership, stale scan/summary, read-only proof)
skip unless `GROWUP_PG_INTEGRATION=1` with migrations 005–006
applied.

### Phase 3C-3B-3B-1 — Reconciliation schema, identity, evidence validation

Foundation for human-approved, evidence-gated reconciliation of
`UNKNOWN` outcomes. No executable path exists yet: no record
function, no transitions, no writes of any kind.

- **Migration 007** (`migrations/007_reconciliation_decisions.sql`,
  applied on the Ubuntu development server): append-only
  `reconciliation_decisions` with decision UUID, action/attempt
  foreign keys, one-decision-per-attempt uniqueness, definitive
  outcome plus matching asserted outcome, prior outcome pinned
  to `UNKNOWN`, principal and case reference, Ed25519 approval
  signature with signing public key and SHA-256 fingerprint
  (rotation-proof historical verification), approval
  timestamps, evidence kind/source/timestamp/digest/summary,
  mandatory artifact reference, optional provider/storage
  references, and tier pinned to `human_reviewed`. No existing
  table is modified.
- **Reconciliation identity:** separate `growup-reconcile-v1:`
  signing domain and `RECONCILER_PUBLIC_KEYS` enrollment, so
  authorization signatures can never verify as reconciliation
  approvals and authorizers do not automatically reconcile.
  Approvals bind the exact action, attempt, outcome, and
  evidence digest with the established lifetime and skew rules
  under an independent TTL constant.
- **Evidence contract** (`app/external_action_reconcile.py`):
  immutable provider-receipt / provider-status-reference model
  with canonical digest binding, concrete artifact references,
  outcome consistency, bounded fields, and timing guards
  (no future observations; evidence must predate approval).
  `system_verified` claims are rejected: Grow Up authenticates
  the human and protects evidence integrity but does not
  independently establish provider truth.
- **Frozen replay contract** (documented for 3B-3B-2): identical
  replay (same attempt, principal, reference, outcome, digest)
  returns the existing decision and writes nothing; renewed
  approvals by the same principal/reference discover it;
  anything else conflicts fail-closed.

### Phase 3C-3B-3B-2 — Transaction-safe human-approved reconciliation

Internal `record_reconciliation(...)` in
`app/external_action_reconcile.py` resolves one `UNKNOWN` action
to `SUCCEEDED` or `FAILED`. No execution, retry, requeue,
worker, or public route: the function signature accepts only
reconciliation inputs, so no executor or orchestrator credential
can substitute for human approval.

- **Pre-connection gates:** identifier shape, evidence parsing
  and outcome consistency, canonical digest binding,
  observation timing, and Ed25519 approval verification
  against `RECONCILER_PUBLIC_KEYS` all pass before any
  database connection opens.
- **One indivisible transaction:** lock the action and attempt
  rows (`SELECT ... FOR UPDATE`), confirm the attempt belongs
  and is latest, replay an existing decision if one is
  committed, and otherwise — only after the action and attempt
  both validate as `UNKNOWN` — insert the decision, flip the
  action to the definitive outcome, and insert the permanent
  `external_action.reconciled` audit via
  `insert_event_in_transaction`, then commit. Any failure
  rolls everything back; the SQLite outbox is never a
  fallback here. The attempt row is never written: its
  `UNKNOWN` outcome is the preserved history of an uncertain
  execution, while the decision row carries the reviewed
  outcome.
- **Replay:** an identical resubmission (a renewed valid
  approval may carry fresh timestamps) returns the committed
  decision and writes nothing — no new decision, audit, or
  transition — after confirming the decision matches, the
  action sits in its terminal state, and the attempt still
  carries `UNKNOWN`. Replay still requires a currently valid
  approval; a different principal, reference, outcome, or
  digest raises `ReconciliationConflictError`.
- **Concurrency:** row locks plus `UNIQUE(attempt_id)` admit
  exactly one winner; simultaneous identical requests yield
  one decision and one audit, rivals fail closed.
- **Preserved restrictions:** no `ATTEMPTING` reconciliation,
  no requeue to `PENDING`/`ATTEMPTING`, no `FAILED` retry;
  the deferred generic transitions stay deferred, and
  GRW-EXT-001/GRW-EXT-002 hold throughout.

Tests: `tests/test_external_action_reconcile.py` covers the
record path against a fake backend mimicking PG row locks and
`ON CONFLICT` replay, plus live-PostgreSQL tests gated by
`GROWUP_PG_INTEGRATION=1` (migrations 005–007 applied; each
test removes its own rows).

### Phase 3C-3B-3B-3A — Read-only reconciliation review queue

Two orchestrator-only scans in `app/external_action_reads.py`
report actions whose state is exactly `UNKNOWN` — uncertain
outcomes awaiting human review. No migration, no routes, no
writes, no audit events, no reconciliation decisions.

- **What it reports:** `list_unknown_actions` returns queued
  actions oldest-first by the latest attempt's
  `outcome_recorded_at` (stable ID tie-break, default limit
  50, clamped to 200), each with its latest attempt's ID,
  number, executor, recorded outcome, and timestamps;
  `unknown_summary` reports the visible count, oldest
  unresolved timestamp (`min(outcome_recorded_at)`), and its
  age in one aggregate query (None — never zero — when
  empty).
- **Who may inspect:** the `WONDRUE_READER_ID` designee reads
  all types; other verified orchestrators read only types
  their exact scope grants cover (empty means nothing);
  executor credentials are rejected. All filtering is
  SQL-side and parameterized.
- **`UNKNOWN` vs stale `ATTEMPTING`:** staleness asks whether
  an open attempt is taking too long; the queue lists recorded
  uncertain outcomes. An unresolved action is awaiting review,
  never presumed failed.
- **Inspection authorizes nothing:** a queued entry confers no
  authority to retry, execute, or reconcile (GRW-EXT-001/
  GRW-EXT-002); only the separate human-approved
  reconciliation path resolves `UNKNOWN`. Every entry must
  carry a latest attempt still `UNKNOWN` with a recorded
  outcome timestamp — a missing or mismatched attempt
  history, or a missing `outcome_recorded_at`, fails the
  call closed rather than hide.

Tests: fake-backend queue/summary coverage plus gated
live-PostgreSQL tests in `tests/test_external_action_reads.py`
(`GROWUP_PG_INTEGRATION=1`, migrations 005–006 applied; no
reconciliation rows are created, and cleanup removes each
test's actions, attempts, and audit events).

### Phase 3C-3B-3B-3B-1 — Read-only reconciliation case dossier

Internal `get_reconciliation_case(...)` in
`app/external_action_reads.py` returns one action's dossier:
the action record, its attempt history, the reconciliation
decision if one exists, the matching permanent lifecycle
audits, and a conservative interpretation. Strictly
informational — no routes, no execution, no retry, no
reconciliation, no writes of any kind.

- **Authorization:** orchestrator-only, reusing the scan
  rules: the `WONDRUE_READER_ID` designee reads any type,
  other orchestrators only their exact scope grants
  (enforced inside the action SELECT), executor credentials
  rejected. Nonexistent and unauthorized actions both read
  as `None`.
- **Consistent snapshot:** action, attempt page, latest
  attempt, an unbounded attempt count, decision, and audits
  are read on one connection in a single `REPEATABLE READ
  READ ONLY` transaction, which always rolls back. Attempts
  and audits are explicitly bounded with truncation
  reported; audits are permanent lifecycle events matched
  by correlation key plus `details.action_id`,
  chronological, metadata only. The count plus latest
  attempt prove the full 1..N history complete (a
  truncated page alone never implies corruption), and
  `generated_at` marks dossier assembly time, not the
  instant PostgreSQL acquired the snapshot.
- **Decision subset:** identifiers, outcomes, principal,
  reference, evidence fields, tier, and decision time.
  Approval signatures, signing keys, and other verification
  internals are never selected, and `human_reviewed` is
  never presented as independently verified.
- **Interpretation:** `unresolved_unknown`, `attempting`,
  `stale_attempting` (elapsed time is not failure and
  never permits retry), `reconciled`, `terminal`,
  `authorized`, `pending`. The attempt's `UNKNOWN` history
  is never presented as rewritten; contradictory ledger
  records fail closed.

Tests: fake-backend dossier coverage plus gated
live-PostgreSQL tests in `tests/test_external_action_reads.py`
(`GROWUP_PG_INTEGRATION=1`; migrations 005–006, plus 007 for
the reconciled-dossier test, which removes its decision row
before the shared cleanup).

### Phase 3C-3B-3B-3B-2 — Human review workflow and evidence presentation

Pure `build_reconciliation_review(dossier)` in the new
`app/external_action_review.py` transforms one dossier from
`get_reconciliation_case()` into a JSON-compatible human
review report. No database, network, filesystem, routes,
authorization, execution, or reconciliation capability —
and no imports capable of those. Authorization and snapshot
guarantees stay with the dossier reader.

- **Six sections:** case summary (identifiers, agent, type,
  state, interpretation, review reason), authorization
  (principal, reference, timestamp — never credentials),
  execution history (attempts with open/succeeded/failed/
  unknown categories and honest descriptions), evidence
  assessment, audit timeline (chronological, truncation
  flagged), and review guidance (missing information,
  permissible steps, standing prohibitions).
- **Evidence:** `documented` / `reported` /
  `human_reviewed` / `missing` classification kept separate
  from `complete` / `partial` / `missing` completeness.
  Credibility comes only from the recorded review tier:
  recorded-but-unreviewed evidence is conservatively
  `reported` however complete its references (the dossier
  carries no signal distinguishing documented from reported
  claims, so `documented` stays reserved for a future
  signal); `human_reviewed` is never presented as
  independently provider-verified, and referenced artifacts
  are cited, never assumed inspected. When a truncated
  history omits the resolved attempt, the report explicitly
  discloses that the decision-to-attempt linkage cannot be
  confirmed from the displayed history.
- **Readiness:** `needs_evidence` /
  `ready_for_human_review` / `review_completed` /
  `not_applicable`, derived conservatively from present
  fields; ready-for-review never authorizes reconciliation.
- **Defensive:** explicit field allowlist (unexpected keys,
  including smuggled secrets, are dropped), strict type
  coercion, state/status/decision cross-checks plus attempt
  and decision linkage validation (identifiers, ordering,
  count bounds, terminal-outcome agreement, UNKNOWN-history
  preservation; truncated pages skip only what they cannot
  prove), input never mutated; malformed input raises
  `ReviewInputError`.

Tests: `tests/test_external_action_review.py` covers every
readiness status and evidence classification, truncation,
malformed dossiers, sensitive-field exclusion,
immutability, determinism, and structural purity guards —
no PostgreSQL needed.

### Phase 3B-3 — Controlled human review access

One authenticated, read-only endpoint in the new
`app/routes/external_actions.py` (registered in `app/main.py`):

`GET /external-actions/{action_id}/review`

- **Authentication:** requires `X-Orchestrator-ID` and
  `X-Orchestrator-Proof` headers, verified with the
  existing orchestrator mechanism; executor credentials
  never authenticate here. Missing, empty, or invalid
  credentials return 401 before any dossier retrieval.
- **Authorization:** unchanged reader scope rules —
  Wondrue's designee reads all types, others only their
  grants. Missing and unauthorized actions both read as
  404 and are indistinguishable.
- **Response:** 200 with only the sanitized
  `build_reconciliation_review()` dictionary; the raw
  dossier, credentials, and signatures never leave the
  server. Errors use generic identifiers: 401
  `unauthorized`, 404 `review_not_found`, 409
  `review_conflict`, 503 `database_not_configured` /
  `database_unavailable`.
- **No-store everywhere:** a path-scoped ASGI middleware
  (`app/middleware.py`, registered in `app/main.py`) stamps
  `Cache-Control: no-store` on every response for the exact
  review path — route responses and framework 401/404/405/
  422 rejections alike, valid or malformed UUID (malformed
  UUIDs never reach the database). Unrelated endpoints are
  untouched. One honest boundary: 500s from unhandled
  exceptions are emitted outside all user middleware by
  Starlette itself and cannot be stamped without
  reimplementing framework error handling; the route maps
  every known failure to a handled, stamped response.
- **Read-only:** no authorize/claim/execute/retry/
  reconcile capability; GRW-EXT-001 and GRW-EXT-002
  preserved. No new environment variables or services.

Tests: `tests/test_external_action_review_route.py`
exercises auth, scoping, indistinguishability, the full
error map, header presence, sanitization, middleware
path-matching and body preservation, and the thin/
read-only structural posture with a mocked reader;
existing endpoints are re-verified in the same file.

---

## 🤝 Coordinated MVP Workflow (Gabriel → Raphael → Onesimus → Michael → Uriel → Priscilla)

One durable workflow connects six processing agents, with Wondrue as the
sole cockpit and conversational interface. Two demonstration paths are
supported: genuine real-property evaluation and an isolated synthetic
demonstration (see [Demonstration](#-demonstration)).

### Gabriel — evidence, dossiers, and identity association

Gabriel (`app/agents/gabriel/`, orchestrated by `app/mvp.py`) acquires
evidence within the run's Brave bound, persists research artifacts,
dossier versions, and deterministic qualifications. A caller-supplied
`property_id` is targeting context — a search target is not identity
proof. A dossier is associated with the operational property only when
its own normalized address uniquely supports the target: exactly one
dossier in the set matches on street/city/state, and the target
address is unambiguous in storage (checked with the existing
`find_by_address`, which never selects among multiple matches).
Unrelated dossiers are preserved without that association; genuine
ambiguity (several dossiers matching, or several stored properties
sharing the address) records a pending `identity_review` row with the
reason and candidates instead of guessing, and custody prefers the
associated dossier — or blocks honestly when none exists.
Review artifacts stay visible through the established workflow: the
custody-blocked stop names the pending `identity_review` row
(`dossier-association`, with its reason), and the same persisted row is
readable via the Wondrue `inspect_review` intent — no parallel queue.

Persisted dossiers carry category and county attribution built from
the grouped evidence through the normal production path: member
category observations with their origins, source references, and
statuses (the same shared observations the verification producer
reads), plus county claims bound to their evidence. Only
observations demonstrably belonging to the candidate contribute: a
lone prop is attributable to its group, but in a multi-property
result only props at the candidate's own address count —
addressless or other-address category signals stay unattributed
rather than cross-assigning or manufacturing a contradiction.
Search targeting (category, county, city, bounding box) is context,
never proof — observed or corroborated category must not become
verified category, and no vacancy, distress, or legal-suitability
claim is ever inferred from building category. Qualification
rejects malformed or unsupported attribution rather than trusting
a status label: excluded or deferred QC outcomes never qualify;
every selected category (observed, corroborated, or verified)
needs a known value with at least one backing observation
normalizing to that same value, and a verified value outside
verified status is rejected; county needs backing claims with a
normalizable value. A caller-supplied verified label without
supporting observations establishes nothing. Pilot cell credit
additionally requires *verified* county attribution backed by a
Census collector claim, and the generic evaluators cap at
corroborated by design — see the Census county-admission
section and MVP limitations.

### Raphael — verification and authorization

Raphael (`app/agents/raphael/`) loads persisted, server-owned dossier
versions plus their supporting artifacts and applies the corrected shared
deterministic policy. Outcomes: `authorized` (sufficient evidence for a
narrowly scoped preliminary simulation), `needs_evidence` (explicit unmet
requirements — a legitimate fail-closed result), or `rejected`
(documented reason with supporting evidence). A category decision never
establishes structural safety, zoning permission, tenant relocation
rights, agricultural feasibility, or profitability.

A narrowly scoped acquisition/replay path retains source artifacts,
collection provenance, content digests, and explicit identity binding;
actual acquisition requires authorization, and the per-run attempt bound
(default 9, hard ceiling 18) applies. Trusted registry entries require
reviewed curation — the production registry starts empty, so
authorization fails closed until curation exists. Human-supplied or
reviewed evidence keeps that provenance and is never relabeled
`adapter_collected`.

### Acquisition budgets, evaluation ordering, and the first-run bound

Every coordinator-originated Brave request (`app/mvp.py` →
`app/agents/raphael/acquire.py`) carries the same guards on both
entry paths (Wondrue `start_real_evaluation` and the discovery
runner's `evaluate_property`, which share one audited wrapper):

- Explicit approval is required; there is no ambient authority.
- Exactly one shared monthly unit is reserved through the existing
  `research_budget` accounting (`app/research_budget_db.py`,
  `BRAVE_MONTHLY_CEILING`, default 100) **before** transmission,
  and the durable per-run bound (`brave_attempts`, default 9, hard
  ceiling 18) is checked first — either exhaustion refuses the
  provider call with nothing transmitted. Reservations are never
  refunded, so ambiguous and failed transmitted attempts count
  conservatively. Reservation happens in `begin_attempt` only:
  artifact replay and the research-run path (which reserves for its
  own transmissions) never double-charge the same request.
- Ambiguous outcomes (timeouts, transport/provider failures) persist
  as `attempt="ambiguous"` artifacts and are never silently
  retried: the same assignment id is terminal and a new id is
  required.
- One assignment is claimed atomically: under the locked run row,
  `begin_attempt` rechecks the stored artifact and started-marker
  before charging either budget, returning `claimed`, `replayed`
  (completed evidence already stored — no new charge), or raising
  a refusal (already-started/ambiguous id, stopped run, exhausted
  bound, exhausted month). Only the claiming caller transmits, and
  the lock is never held during HTTP — two racers yield exactly
  one provider invocation and one monthly/per-run charge.

Fresh evaluations run in committed phases so no business
transaction ever spans an external call.
`start_evaluation_audited` (1) commits the run — durably visible
before any other connection accounts an attempt; (2) commits the
evidence artifact — budget charges and the started-marker commit
before the provider transmits, transmission happens with no
transaction open, and the artifact commits immediately after; then
(3) runs the remaining chain inside the coordinated transaction,
where the Gabriel stage replays the committed artifact instead of
transmitting. The permanent `wondrue.evaluation.completed` audit
event commits if and only if the coordinated chain commits, so it
always describes actually committed stages. Crash/resume: a
completed artifact replays without a new charge; a started-marker
without an artifact (an uncertain provider request) refuses the
same id; a pre-transmit refusal (unapproved, over bound, monthly
exhausted, stopped run) charges nothing and stays retryable. A
resumed run keeps its persisted limit — re-invocation never widens
it. The owned path (`ensure_artifact_owned` → locked
`begin_attempt` claim → transmit → persisted artifact) is the only
supported acquisition entry point: the legacy in-transaction
helpers were deleted outright because a bare started-marker could
not prove a monthly reservation, so no remaining helper transmits
without the locked claim and no application path treats a marker
alone as permission to transmit. Accurate crash/ambiguity limits:
a crash between the monthly reservation and the per-run commit
over-counts the month by one (conservative, never refunded); a
crash after the marker commits but before the artifact persists
leaves that assignment id terminal — resume refuses it and a new
id is required, never a blind retry; a transmitted ambiguous
attempt keeps its persisted artifact and is likewise terminal for
that id.

The coordinator annotates every ranked candidate with the existing
shared verification policy before dossier persistence
(`build_candidate_verifications`, the same producer the research
path uses — no second verification engine): each persisted dossier
version carries the policy's explicit `candidate_verification`
result (`outcome`, `reasons`, `rationale`), including
`not_verified` with its failure reasons where appropriate.
Annotations attach only over genuinely adapter-collected evidence
(the replayed artifact's own recorded collection provenance);
anything else persists without the stamp, and Raphael still
decides authorization from evidence alone — weak directory-style
snippets yield `not_verified` and cannot authorize Onesimus.
Raphael consumes that stamp through the shared verification model
itself: only the policy's canonical serialized contract counts —
lowercase `verified` with a `pathway`, a `rationale`, and
qualifying voices. Legacy uppercase spellings fail validation as
malformed and yield `needs_evidence`, never authorization, so a
genuine policy success passes Raphael while weak evidence cannot.
Listing claims and intake categories are never converted into
authenticated facts by this wiring.

Extraction attributes location only within each address's own
segment, and the city match excludes the matched address span
itself: street tokens are never consumed as part of the city
("2310 Chalk Hill Rd Dallas, TX" yields city Dallas, not
"Hill Rd Dallas"), while genuine multiword cities, unpunctuated
forms, and unknown-when-ambiguous fields are preserved. No city
is hardcoded or inferred from the query or a neighboring property.

Bounded DCAD evidence admission covers exactly one approved
source: a Dallas Central Appraisal District commercial-account
page (`AcctDetailCom.aspx?ID=<account>`). Collection refuses
before claiming or transmitting unless explicitly approved,
enabled (`DCAD_COLLECTION_ENABLED=1`), and allowlisted
(`DCAD_ALLOWED_ACCOUNTS`); the URL is server-constructed, so
callers supply no URL and uploads cannot impersonate the minted
`adapter_collected` provenance. A committed exclusive claim
serializes transmission per attempt-scoped run key: concurrent
identical calls permit at most one HTTPS GET (12-second
timeout, 256 KiB cap, no redirects, no retries, no secondary
fetches, no proxy environment, TLS verified, destination
resolved and refused unless public), completed evidence replays
without a request, and an uncertain started claim cannot
silently retransmit — recovery is a separately approved new
attempt (`attempt="2"`). No business
transaction stays open during HTTP. The bounded response bytes
are retained with their SHA-256, URL, timestamp, account,
collector revision, and the authenticated principal before
parsing, so a parser/assessment refusal is recorded as an
inspectable refusal record without erasing the bytes; the
digest proves byte integrity, not truth or authenticity. Known
limits: a crash between transmit and retention loses the bytes
(the key stays uncertain), and resolve-then-connect is not IP
pinning (documented TOCTOU).

Parsing reads the labeled structure with the standard library
and stays inside it: the account binds to the unique
`lblPageTitle` title with exact account matching (a
nearby-account link, unlabeled text, or any other element alone
does not satisfy identity), and improvement entries come only
from rows in the `lblMainImp` section (after the heading, before
the next DCAD section header, `span.DtlSectionHdr` — the Land
marker terminates the section) whose cells carry the inline
`Desc` label. Ordinary `FieldTitle`/`FieldValue` spans, nested
labels, and `h1`–`h4` tags are never section boundaries.
Each entry keeps its description, Total Area, and Year Built
from that same row with raw values and locator; rows containing
a nested table are never entries themselves, so ancestor
aggregation cannot duplicate them, and unrelated matching rows
outside the section are ignored. The Total Area label never
matches foundation, lease, or land areas. Empty or repeated
labels skip the row (counted); missing or unparseable areas and
years stay unknown with raw values retained. Period labels
("Current 2027", "Certified ... 2026") stay literal — never
effective dates, use assertions, occupancy, or financial
inputs. The title, heading, and inline labeled-row structure
are source-derived from observed markup, and the offline
fixture reproduces them as labeled test material — that covers
parser acceptance against the observed structure, not live
collector acceptance, which still requires separately approved
owner-executed retrieval; surrounding page layout beyond the
confirmed anchors remains representative, and unexpected
structure fails closed. Alignment is supported only on exact street-address
agreement with the property record, scoped to the account
claim — street agreement never marks retained Brave voices
aligned (Phase 2A keeps them unaligned), never a building or
suite; improvement descriptions are never mapped to operating
use. The shared policy stays the sole category engine with
unchanged thresholds; DCAD evidence carries no defining use
facts, so DCAD alone cannot verify, and scope conflicts are
preserved rather than resolved by favorable picks. No registry
enrollment, blanket host trust, or publisher authentication is
added; the production registry stays empty.

A successor preview shows the intended qualification, Raphael
decision, material content changes, and per-voice alignment —
not just the DCAD-only outcome. A successor commit re-verifies
every approval binding against recomputed stored digests
(parent version/digest, collection/assessment digests and
cross-bindings, supported collector/parser revisions, custody
chain, gabriel holder, expected version), replays retained
Brave evidence without a new acquisition, evaluates cumulative
evidence through the shared policy, and persists the successor
dossier rebuilt from that same processing (with content
changes and voice alignment) — never the unevaluated prior
content. Amendment authority resolves from the
backend-authenticated principal against the enrolled-amender
allowlist (no caller-supplied name, no auto-enrollment), and
is verified before any commit or replay output: an unenrolled
caller is refused even when the run is already complete, and
replay additionally binds the original operation's principal,
so a different enrolled caller cannot retrieve another
operation's result as a replay. The whole
amend/snapshot/receipt/audit commit runs under the
per-dossier custody serialization lock, so concurrent
identical commits append exactly one version while stale or
conflicting attempts fail safely. Identical retries replay
stored outputs only after their operation bindings validate;
original history is never rewritten and pilot counts replay
without new credit. Read-only previews stay available under
their existing permissions and never amend. No schema migration is needed: the new
record kinds (`dcad_collection`, `dcad_assessment`,
`dcad_claim`, `dcad_refusal`) ride the schemaless workflow
store. Wondrue: `preview_dcad_collection` (with `attempt`) /
`preview_successor` (read-only, no network or writes),
`collect_dcad_evidence` / `commit_successor` (operator grant,
ask-mode tokens bind exact args). Live retrieval is
owner-executed after separate approval and is not proven by
fixture tests.

Bounded Census county admission covers exactly one approved
source: the documented Census geocoder structured-address
geographies endpoint (`geographies/address?street=&city=&state=
&zip=`, benchmark `Public_AR_Current`, vintage `Current_Current`,
JSON). Collection refuses before claiming or transmitting unless
explicitly approved, enabled (`CENSUS_COLLECTION_ENABLED=1`),
and presented with a stored Texas street address and an
authenticated principal; the URL is server-constructed from the
stored operational address, so callers supply no URL and uploads
cannot impersonate the minted `adapter_collected` provenance. A
committed exclusive claim serializes transmission per
property/address/attempt key: concurrent identical calls permit
at most one HTTPS GET (12-second timeout, 256 KiB cap, no
redirects, no retries, no secondary fetches, no proxy
environment, TLS verified, destination resolved and refused
unless public), completed evidence replays without a request,
and an uncertain started claim cannot silently retransmit —
recovery is a separately approved new attempt. No business
transaction stays open during HTTP. The bounded response bytes
are retained with their SHA-256, request parameters, retrieval
time, submitted/matched address, coordinates, returned
benchmark/vintage identity, collector revision, and principal
before assessment, so an assessment refusal is recorded as an
inspectable refusal record without erasing the bytes. Returned
benchmark/vintage names are preserved per retrieval; a mutable
"Current" dataset is never described as immutable across dates.
The claim commit reserves the attempt's allowance on the
evaluation run *before* transmission: only the claim winner
charges, exactly once, so ambiguous transmits, assessment
refusals, resumes, and replays can neither undercount (no crash
window between transmit and accounting) nor double-count, and
no refund path reopens the allowance. Pre-claim refusals
(disabled, unapproved, unusable address, uncertain prior,
lost race) charge nothing. Known limits: a crash
between transmit and retention loses the bytes (the key stays
uncertain, the allowance stays consumed), and
resolve-then-connect is not IP pinning.

Admission is county-only and requires exactly one supported
address match with compatible normalized house number, street,
and Texas state; city/ZIP are compared when both sides are
present (mismatches refuse, absent sides record as not
compared, never filled from targeting context); and exactly
one county geography with valid consistent identifiers
(GEOID == STATE+COUNTY, Texas FIPS 48) cross-checked between
the returned name and the embedded Texas reference table. The
result maps explicitly to the twelve canonical counties;
valid outside-territory Texas results are admitted as outside,
never rewritten to the requested county. No match, ambiguity,
malformed geography, unsupported alignment, or conflicting
retained county claims prevents admission, with all claims
retained and contradictions explained. The admitted assertion
is labeled "County of the matched Census address location —
discovery territory only." It never establishes parcel
boundaries (or whether a parcel straddles counties), a
particular building or suite, ownership, occupancy, vacancy,
distress, current use, category verification, zoning, or
agricultural permission; interpolation does not guarantee the
correct parcel, and known parcel/address disputes stay
unresolved. Only collector evidence establishes this result —
a caller-supplied "verified" status is never authority, and
both credit gates (qualification `county_credit` and pilot
cell selection) require the collector claim marker. Applying a
result additionally binds the dossier's own address to the
collected address (house number, street, and state must match;
city compares when both sides are present): evidence collected
for address B never credits a dossier describing address A,
even under the same property UUID, and unresolved dossier
addresses refuse. Agreeing retained claims are preserved with
the Census claim appended (identical collector claims dedup,
never duplicate), and every credited dossier carries a Census
evidence locator (admission run id, collection/county record
keys and digests, submitted/matched address) retrievable and
verifiable without reconstructing identities from URL text; no
verified attribution persists without its locator. Census is
never enrolled in the property-category verification registry,
and the shared verification policy is unchanged.

New evaluations collect in a committed phase before the
coordinated chain (at most one request per property, default
disabled) and apply the retained result to the associated
dossier inside the chain; a refused application is recorded
and the dossier stays county-unresolved, and Census failures
never abort the evaluation. Existing dossiers use the same
audited successor mechanism narrowly extended to Census pairs
(explicit source/parent/digest bindings, enrolled-amender
authority, custody serialization, idempotent replay, one
canonical quota identity, no duplicate credit, original
history untouched) with the ordinary fail-closed category
verdict — county admission cannot authorize Raphael or
verify category. Retained artifacts resolve through explicit
run/key/digest lineage, so a successor-of-successor (original
→ DCAD successor → Census successor) still retrieves the
original evidence with its digests verified; lineage that
disagrees with the stored rows refuses loudly. No schema
migration is needed: the new record kinds
(`census_collection`, `census_county`, `census_claim`,
`census_refusal`) ride the schemaless workflow store. Live
Census compatibility and real county results are not proven
by fixture tests; the inline JSON fixtures are labeled test
material.

The initial owner-run evaluation is a configurable single-request
bound rather than the ordinary nine-attempt allowance:
`brave_attempt_limit=1`, 12-second provider timeout, no automatic
retries, zero Meta requests (the evaluation path issues none; run
it with a structured intent and `use_live: false` and do not
follow with `compare_scenarios` or assistance intents), no
outreach (no sender exists), and no simulated conversions
(`run_simulation` stays `false`). Thread it through
`start_evaluation_audited(brave_attempt_limit=1)`, the Wondrue
`start_real_evaluation` args (`brave_attempt_limit`,
`timeout_seconds`, `property_id`), or the runner
(`--evaluation-attempt-limit 1 --evaluation-timeout 12`, or the
same Wondrue burst args); ask-mode previews state the bound
before anything executes. Direct evaluations default to no
Census county admission (`census_attempt_limit=0`); the runner
passes an explicit per-property limit only inside an explicitly
bounded burst.

Operational-property intake is a prerequisite, not a coordinator
function. The coordinator never invents a property identity or
forces an unrelated dossier onto one: the property must already
exist via the validated intake (`POST /properties`, sourced
estimated/verified pairs) or OSM discovery intake, and the
coordinator only associates at most one dossier whose own
normalized address uniquely supports the target
(`associate_dossiers_with_property` + `find_by_address`; ambiguity
records a pending `identity_review` and custody blocks honestly).
State comparison uses the shared Texas-only rule, so a dossier
saying "Texas" matches a stored "TX" (and vice versa) without
guessing across genuinely different addresses.
The runner derives a deterministic assignment
(`runner:{scope}:{property_id}`) and query from the property's own
stored address and skips address-less rows with reasons; a Wondrue
evaluation without a resolvable target stops at the custody block
instead of guessing.

### Discovery source precedence (owner-directed)

Gabriel discovers property in every new geographic jurisdiction
in this order, recorded as a validated configuration contract
(`config/discovery_sources.example.json`, loader/validator
`app/agents/gabriel/sources/precedence.py`):

1. **P1 — official government data.** Appraisal/assessor
   districts, parcel and GIS datasets, local open-data portals,
   tax/property records, zoning (direct official downloads,
   published APIs, legally permitted bulk data only; no paid
   APIs, paywalls, unauthorized scraping, or bypassed access).
   Status: `catalog_required` — no DFW discovery dataset is
   cataloged yet; cataloging (authority, coverage, access,
   licensing, update frequency, provenance, reliability) is a
   prerequisite, not an integration. The existing Census
   geocoder and DCAD account-page adapters are official
   *evidence* adapters used after discovery, not listing
   discovery.
2. **P2 — SearXNG.** An approved instance for supplemental web
   discovery and evidence enrichment, integrated into the
   existing engine with budgets, deduplication, provenance, and
   structured output preserved. Status: `planned`, not
   integrated.
3. **P3 — Brave Search API.** The existing integration,
   demoted to third-priority fallback
   (`existing_fallback`). No automatic paid usage without
   standing budget authorization; free discovery is exhausted
   first.

Qwen is excluded from Gabriel entirely (reserved for Wondrue
and Uriel); any declaration naming it is refused. The current
Overpass/OSM geographic index is retained for the DFW pilot
window only; new jurisdictions follow the tier order. All
sources feed the one canonical property pipeline — no separate
agent workflows. This contract authorizes no live collection,
spending, or provider integration by itself.

GUADE status: the retained November 2 activation audit records a completed
Baseten Super-Coach (`meta-models/muse-spark-open-1026`) -> Wonder -> tests ->
review cycle. The exhausted activation ledger is preserved. Further execution
requires separate owner approval, an explicit task list and a new bounded
ledger; see `guade/OPERATING.md`. Development and GUADE execution stay on Windows;
Git/GitHub transfers owner-approved revisions to Ubuntu for application and
PostgreSQL verification. No campaign or provider call was run during the runtime
correction task.
M03A (the first 144-dossier cohort) prerequisites still open:
an owner-approved exact first acquisition envelope (the Dallas
units and envelope file are design-approved, not launched), a
cataloged P1 discovery dataset for DFW counties, per-property
Census collection authority at cohort scale, cohort identity
tracking (the per-cell pilot ledger exists; cohort ids do
not), and re-verified Brave monthly headroom at launch. These
are launch prerequisites, not implementation gaps in accepted
functionality.

Focused checks (mocked providers, disposable data; PG-gated tests
skip without `GROWUP_PG_INTEGRATION=1`):

```bash
python -m pytest -q tests/test_coordinator_attribution.py tests/test_extractor.py tests/test_grouping_identity.py tests/test_ranking.py tests/test_dossier.py tests/test_verification_producer.py tests/test_mvp_flow.py
python -m pytest -q tests/test_acquisition_budget.py tests/test_mvp_flow.py tests/test_raphael.py
python -m pytest -q tests/test_dcad_admission.py
python -m pytest -q tests/test_census_admission.py
python -m pytest -q tests
git diff --check
```

Local suite in this tree: **2,184 passed, 146 skipped** (133
pre-existing skips plus 13 PG-gated checks), 17
warnings, with legacy-helper coverage preserved on the supported
owned entry point, plus the 16-check sanitized attribution suite
(`tests/test_coordinator_attribution.py`, derived from the
retained real snippets: TX/Texas identity, per-address ZIPs,
directory-city attribution, coordinator verification
persistence, shared-policy success contract, fail-closed
association), the 79-check DCAD admission suite
(`tests/test_dcad_admission.py`: source-derived nested parser
with title binding, labeled-row association, and the actual
`span.DtlSectionHdr` section boundary, section/account
isolation, collector claim/retention/refusal boundaries,
public-destination refusal, admission honesty, claim-scoped
alignment, successor rebuilt content/idempotency/binding
validation/history, successor-of-successor run/key/digest
lineage, custody lock behavior, principal-resolved
amendment authority, Wondrue approvals), and the 57-check
Census admission suite (`tests/test_census_admission.py`:
server-constructed URL, collector claim/retention/refusal
boundaries, crash-safe claim-time charging without
double-counting, no-match/ambiguous/incompatible handling,
identifier and reference-table validation,
outside-territory admission, forgery/binding refusal,
dossier-address binding, agreeing-claim preservation,
evidence-locator retrieval, in-stage application, successor
county application without category authorization, forged
county-credit refusal at both credit gates).
Pending owner execution on the disposable database
(`growup_mvp_gate`) after `python -m app.migrate`: the seven
`GROWUP_PG_INTEGRATION`-gated tests in
`tests/test_acquisition_budget.py` (uncommitted-run
invisibility demonstration, committed fresh-run acquisition,
monthly exhaustion blocks transmit, single-request bound across
assignment ids, two-connection race with one charge, recheck under
a held lock, started-marker blocks uncertain replay), the three
PG-gated tests in `tests/test_dcad_admission.py`
(successor-commit end-to-end amendment with authorized replay,
unenrolled-replay refusal, history preservation, no duplicate
pilot credit, and rollback atomicity; two-thread collect race
asserting one mocked transmission; and two-thread successor
race asserting exactly one v2 with its intended receipts), the
three PG-gated tests in `tests/test_census_admission.py`
(two-thread collect race asserting one mocked transmission;
successor-commit end-to-end county credit with replay,
history preservation, and no duplicate credit; and a new
evaluation running evidence to dossier to qualification to
cell credit with mocked providers),
plus a full regression and health checks. Those mutating checks
verify they are connected to `growup_mvp_gate` before any write
and fail rather than run elsewhere. Cleanup deletes disposable
workflow, pilot-event, snapshot, and clearly named test-property
rows; committed `dossier_versions` and `custody_receipts` rows
are append-only by production trigger and are deliberately
retained, keyed by fresh test UUIDs (receipts issued by
`pg-test`, successor actor `pg-test-owner`) so repeat runs and
other tests stay independent. Separately approved live DCAD
retrieval is not executed here. Mocked success is not proof of
PostgreSQL behavior, and skipped PG tests are not acceptance.

Real-data boundary: the original first real run, its artifacts,
dossier versions, decisions, snapshots, and receipts are preserved
unchanged — no keys rewritten, no records merged, no budgets
reset, no automatic replay of the completed/stopped run. The
sanitized fixture replays prove the corrected code paths only; a
passing corrected replay must not be presented as proof of
real-property qualification. Any future corrected reprocessing is
a separately reviewed owner decision and must retain its
relationship to the original evidence and history.

### Owner first-acquisition configuration (proposed, not executed)

No acquisition is authorized until the owner completes code review,
deployment, and this configuration. Run this block on the Ubuntu
host. It provisions a custody HMAC key **only if absent** (an
existing valid key is preserved; a present-but-short key stops for
an explicit owner decision — ordinary setup never silently rotates),
enables the workflow only after the owner confirms the reviewed code
is deployed, keeps live Meta spending disabled, and restarts only
`growup.service` with readiness verification. Lengths and statuses
are printed; values never are. Both confirmations read from the
controlling terminal (`/dev/tty`) — never `input()`, whose stdin
carries the program itself — the paste-back uses hidden terminal
input, and the shown key is copied only into the owner's own
private records.

```bash
sudo -v
sudo python3 <<'PYEOF'
import getpass, os, re, secrets, stat, subprocess, sys, tempfile, time
ENV = "/var/www/growup/shared/growup.env"
SERVICE = "growup.service"
HMAC_KEY = "CUSTODY_HMAC_KEY"
WORKFLOW_KEY = "MVP_WORKFLOW_ENABLED"
SPEND_KEY = "META_SPEND_APPROVED"
ALLOW_KEY = "INFERENCE_TOKEN_ALLOWANCE"
MIN_HMAC_CHARS = 16
def stop(message):
    print("STOP: " + message)
    raise SystemExit(1)
def ask(prompt):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with open("/dev/tty") as tty:
        return tty.readline().rstrip("\n")
def sysctl(*args):
    try:
        return subprocess.run(["systemctl"] + list(args), capture_output=True, text=True)
    except OSError as exc:
        stop(f"cannot run service manager: {exc}")
def split_assignment(line, key):
    match = re.match(rf"^(export\s+)?{key}\s*=\s*(.*?)\s*$", line.rstrip("\n"))
    if not match:
        return None
    body = match.group(2).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]
    return (match.group(1) or "", body.strip())
# --- preflight: service identity and environment safety (no values printed) ---
unit = sysctl("cat", SERVICE)
if unit.returncode != 0:
    stop(f"{SERVICE} not found ({unit.stderr.strip()}); refusing to guess layout")
match = re.search(r"^EnvironmentFile=\s*(\S+)", unit.stdout, re.M)
if not match or match.group(1).lstrip("-") != ENV:
    stop("service EnvironmentFile is not the expected path; review the unit file first")
if os.path.islink(ENV):
    stop("environment path is a symlink; refusing to follow it")
lst = os.lstat(ENV)
if not stat.S_ISREG(lst.st_mode):
    stop("environment path is not a regular file")
orig_mode = stat.S_IMODE(lst.st_mode)
if orig_mode & 0o077:
    stop(f"environment file permissions {oct(orig_mode)} grant group/other access; refusing")
orig_uid, orig_gid = lst.st_uid, lst.st_gid
fd = os.open(ENV, os.O_RDONLY | os.O_NOFOLLOW)
try:
    raw = os.read(fd, 1 << 20).decode("utf-8")
finally:
    os.close(fd)
lines = raw.splitlines(keepends=True)
values = {}
for key in (HMAC_KEY, WORKFLOW_KEY, SPEND_KEY, ALLOW_KEY):
    hits = [i for i, line in enumerate(lines) if split_assignment(line, key) is not None]
    if len(hits) > 1:
        stop(f"duplicate {key} assignments (lines {[i + 1 for i in hits]}); dedupe manually first")
    values[key] = split_assignment(lines[hits[0]], key)[1] if hits else ""
# --- custody key state: absent -> provision; valid -> preserve; short -> stop ---
hmac_state = "NOT configured" if not values[HMAC_KEY] else (
    "configured" if len(values[HMAC_KEY]) >= MIN_HMAC_CHARS else "TOO SHORT")
print(f"[info] {HMAC_KEY}: {hmac_state} (minimum {MIN_HMAC_CHARS} characters)")
if values[HMAC_KEY] and len(values[HMAC_KEY]) < MIN_HMAC_CHARS:
    stop("a short custody key is configured; replacing it would invalidate existing "
         "receipt MACs, so replacement needs an explicit owner decision, not this block")
fresh_hmac = ""
if not values[HMAC_KEY]:
    if ask("Generate a NEW custody HMAC key now? Type YES to continue: ").strip() != "YES":
        stop("aborted by owner; nothing changed")
    generated = secrets.token_urlsafe(32)
    print("=" * 70)
    print("OWNER ONLY - copy this value now into your own private records, kept")
    print("where only you can retrieve it, as the Grow Up custody HMAC key.")
    print("This value is shown once and is not for sharing.")
    print("=" * 70)
    print(generated)
    pasted = getpass.getpass("Paste the value back to install it: ")
    if not pasted or pasted != generated or len(pasted) < MIN_HMAC_CHARS:
        stop("paste-back did not match; nothing was written")
    fresh_hmac = pasted
else:
    print("[ok] existing custody key preserved (no rotation)")
# --- workflow enablement only after review + deployment; spend stays off ---
if values[SPEND_KEY] == "1":
    stop("live Meta spending is enabled (META_SPEND_APPROVED=1); refusing")
if (values[ALLOW_KEY] or "0").strip() not in ("", "0"):
    stop("an inference token allowance is granted; reset it to 0 first")
print("[ok] live Meta spending is disabled (no allowance)")
enable = ask("Enable the real-property workflow (MVP_WORKFLOW_ENABLED=1)? "
              "Type YES only if the reviewed code is deployed: ").strip() == "YES"
if not enable:
    stop("workflow left disabled by owner choice; nothing changed")
def install_lines(content, dest, mode, uid, gid):
    data = "".join(content).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".growup.env.", dir=os.path.dirname(ENV))
    try:
        os.write(tmp_fd, data)
        os.fchmod(tmp_fd, mode)
        os.fchown(tmp_fd, uid, gid)
        os.close(tmp_fd)
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.close(tmp_fd)
        except OSError:
            pass
        try:
            if os.path.lexists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
backup = f"{ENV}.bak-{stamp}"
install_lines(lines, backup, 0o600, 0, 0)
print(f"[ok] restricted root-owned backup: {backup}")
updates = {WORKFLOW_KEY: "1"}
if fresh_hmac:
    updates[HMAC_KEY] = fresh_hmac
out = []
seen = set()
for line in lines:
    replaced = False
    for key, value in updates.items():
        parsed = split_assignment(line, key)
        if parsed is not None:
            out.append(f"{parsed[0]}{key}={value}\n")
            seen.add(key)
            replaced = True
    if not replaced:
        out.append(line)
if out and not out[-1].endswith("\n"):
    out[-1] = out[-1] + "\n"
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}\n")
install_lines(out, ENV, orig_mode, orig_uid, orig_gid)
print("[ok] environment file updated atomically (custody key only if absent; workflow enabled)")
def service_active():
    probe = sysctl("is-active", SERVICE)
    return probe.returncode == 0 and probe.stdout.strip() == "active"
def probe(path):
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://127.0.0.1:8001{path}"],
        capture_output=True, text=True)
    return result.stdout.strip() == "200"
def wait_ready(timeout_s=60):
    end = time.time() + timeout_s
    while time.time() < end:
        if service_active() and probe("/health") and probe("/ready"):
            return True
        time.sleep(2)
    return False
restart = sysctl("restart", SERVICE)
if restart.returncode != 0 or not wait_ready():
    print("RESTART/READINESS FAILED; restoring the original environment file")
    bfd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        backup_lines = os.read(bfd, 1 << 20).decode("utf-8").splitlines(keepends=True)
    finally:
        os.close(bfd)
    install_lines(backup_lines, ENV, orig_mode, orig_uid, orig_gid)
    sysctl("restart", SERVICE)
    stop("rolled back to the ORIGINAL environment; setup did not complete")
print("[ok] service active; GET /health -> 200; GET /ready -> 200")
print("DONE: workflow enabled with spending disabled. Next: verify the monthly "
      "budget row and ceiling (read-only), then run the pending test gates.")
PYEOF
```

Verify without spending (read-only; prints counts only):

```bash
sudo -u growup psql -d growup_dev -c \
  "SELECT research_month, used, ceiling FROM research_budget \
   WHERE research_month = to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM');"
sudo -u growup psql -d growup_dev -c \
  "SELECT count(*) AS runs FROM workflow_runs; SELECT count(*) AS records FROM workflow_records;"
```

Expected before the first acquisition: the month row reads `0`
used (created on first reservation) against ceiling `100`, and
both workflow counts read `0` after the intentional reset. Any
`used > 0` needs an owner explanation before proceeding.

### Onesimus — preliminary engineering (no money)

Onesimus (`app/agents/onesimus/`) evaluates one crop profile (butterhead
lettuce) under two passive, non-circulating Kratky configurations:
`bench` (single-level benches, individual containers) and `rack3`
(three-tier stacked racks, shared reservoirs per tier). Every
consequential input carries value, unit, provenance, and classification
(verified, estimated, experimental, assumed, unknown).

Inspectable Python computes usable growing area, planting sites,
reservoir volume, root-zone air-space and structural-load estimates,
lighting electricity, and water/nutrient quantities. Prices, capex,
revenue, operating costs, and payback are Michael's — Onesimus outputs
carry no money. Missing critical inputs return incomplete results —
never silent zeros — and AI never supplies computed outputs.
Real-property scenarios validate the persisted Raphael authorization
against the exact dossier version/digest; demo-only or stale
authorizations are rejected. Tenant consolidation (relocating tenants
upward, converting from the lowest feasible level) starts enabled as an
explicit hypothetical assumption; observed occupancy is preserved
separately and consent, leases, access, and relocation costs are flagged
as unresolved.

### Michael — deterministic financial authority

Michael (`app/michael.py`) is the single financial authority: he consumes
Onesimus engineering outputs plus classified financial inputs and computes
capex, opex (electricity, water, nutrients, labor, HVAC, other),
revenue, operating result, sensitivities, and simple payback or
break-even only where defined and supported. Every consequential input
identifies value, unit, currency, period, source, observation date,
classification, scenario, and uncertainty; unknown costs never silently
become zero — incomplete bundles return an `insufficient_evidence`
verdict naming the gaps. Verdicts are `favorable_under_assumptions`,
`unfavorable_under_assumptions`, or `insufficient_evidence`: a completed
calculation is not automatically favorable, and projected revenue is not
actual revenue. Money uses Decimal cents with explicit rounding; model
proposals may tune only cataloged financial parameters, and AI never
supplies computed outputs or approves unsupported conclusions.

### Meta inference adapter

`app/model_adapter.py` fronts Meta behind a stable interface. Meta
interprets Wondrue commands and proposes configuration comparisons from
the approved `bench`/`rack3` catalog; structured output is validated, and
deterministic Python performs all calculations and authorization checks.
Live calls require `META_SPEND_APPROVED=1` (plus `META_API_URL`,
`META_API_KEY`, `META_MODEL`; token ceiling `META_MAX_TOKENS`, default
1024) and are recorded with provider, model, live/replay label, usage,
and digests. Replays are always labeled and can never be mistaken for
live calls. Credentials stay server-side.

### Wondrue interface and access modes

- `GET /wondrue` — compact results page; real and synthetic targets use
  unmistakable banners, and live/replay model output gets distinct badges.
- `POST /wondrue/command` — one JSON command
  (`mode`, `text`, `target`, `approval_token`, `use_live`, `intent`).
  Supported real-target intents: `inspect_candidate`,
  `inspect_evidence`, `explain_decision`, `start_real_evaluation`,
  `stop`, `compare_scenarios`, `inspect_packages`,
  `preview_simulation`, `run_simulation`, `inspect_simulation`,
  `inspect_discovery`, `start_discovery`, `resume_discovery`,
  `stop_discovery`, `assist_explain`, `analyze_evidence`,
  `explain_economics`, `draft_outreach`, `inspect_inference_usage`,
  `preview_dcad_collection`, `collect_dcad_evidence`,
  `preview_successor`, `commit_successor`,
  `help`; plus `start_demo` for the synthetic target.
- `POST /wondrue/assist/extract` — extract unverified claims from
  caller-supplied pages (`doc_id`, `source_ref`, `pages` with inline
  PNG/JPEG/WebP images or extracted text, optional `run_id` /
  `dossier_id` association). Consequential: read-only refused, ask
  previews with an approval token, full executes within the shared
  inference budget. Never fetches URLs or reads the filesystem.
- `POST /wondrue/demo/reset` — resets only the demo store; requires the
  operator grant.

Authentication is the `X-Wondrue-Key` header against two enrolled
credentials: `WONDRUE_OPERATOR_KEY` grants the full mode range,
`WONDRUE_READER_KEY` grants read-only. Enrolling server credentials
is an owner-operated setup step; with both absent or blank the API
fails closed with `wondrue_not_configured`, and no default
credential exists. The cockpit labels its masked field **Wondrue
access key** — it takes the operator or reader key, never the
Baseten API key — stores nothing from it, and explains the three
failure shapes inline above the preserved raw response: server
credentials not configured, missing/invalid access key, and
insufficient permission. The grant is derived
server-side from the credential, and the requested mode is capped by
it: supplying `"mode": "full"` confers no authority by itself, and an
unknown grant fails closed. Modes are enforced server-side:
**read-only** inspects/explains with no business mutations, no live
calls, and no external calls; **ask** returns a concrete preview
(operations, inputs, effects, cost bounds) plus a single-use approval
token before anything consequential executes; **full** executes
permitted operations within explicit scopes and limits. Read-only
intents (`inspect_*`, `preview_simulation`, `preview_dcad_collection`,
`preview_successor`, `explain_decision`, `help`) never mutate
records or issue metered calls; consequential intents (`start_*`,
`resume_discovery`, `stop*`, `run_simulation`,
`compare_scenarios`, `assist_explain`, `analyze_evidence`,
`explain_economics`, `draft_outreach`, `extract_document`,
`collect_dcad_evidence`, `commit_successor`) need ask-with-token
or full, and the DCAD/successor pair additionally requires the
operator grant. Discovery bursts
through Wondrue are request-safe (at most 5 passes / 900 seconds);
longer campaigns use repeated bursts or the runner CLI. Bursts run
free OSM discovery only unless `acquire: true` is passed: acquisition
is disabled by default, permission derives server-side from the
operator grant (requested-but-unauthorized bursts are refused, never
silently downgraded), ask mode previews the operation with its Brave
bound (passes × evaluations-per-pass × 9 per-evaluation attempts),
and `max_evaluations` (0–5 per pass) configures the cap. Read-only
and unapproved requests make no acquisition calls. Paid
inference always needs spending approval. Outreach remains disabled.
The command route owns its transaction: it commits only when the
command succeeds, rolls back when the command fails (including a
command that writes and then raises), and returns 503 when the commit
itself fails — persistence failures are never swallowed, and read-only
commands never open the write transaction at all.

After the first evidenced 144-dossier pilot (36 cells, ≥144 persisted
qualified dossiers), ordinary `start_demo` selection through Wondrue is
refused: synthetic upstream workflows become a restricted diagnostic
fallback. Automated tests keep working, and emergency diagnostic
execution requires explicit owner authorization
(`EMERGENCY_DIAGNOSTIC_DEMO=1`). Provider failure never substitutes
synthetic results automatically.

### Owner-operated Wondrue credential setup

Grants: `WONDRUE_OPERATOR_KEY` → full mode range,
`WONDRUE_READER_KEY` → read-only, both presented on the
`X-Wondrue-Key` header and compared with `hmac.compare_digest`.
Both absent/blank fails closed with 503
`wondrue_not_configured`; a missing/unknown key returns 401
`unauthorized`; a reader attempting an operator action is refused
with 403 before the action executes. No default credential
exists, and keys never appear in logs or error responses. There
is no code defect here: provisioning is purely operational, so
no migration, Nginx change, deployment, or dependency is needed
— only a `growup.service` restart to load the new environment.

Run this block on the Ubuntu host. Preflight completes before
anything is generated or written: it verifies the service unit,
its effective user/group, and its environment-file path;
parses the environment with correct quote/whitespace/`export`
handling (never exact-line grep); refuses unsafe permissions,
symlinks, duplicates, enabled live spending, identical
operator/reader values, partial configuration, and any existing
configured pair — all without displaying values. Only then does
it provision two distinct 32-byte random credentials, keep a
root-owned restricted backup, update only the two assignments
atomically, and restart plus readiness-check the service.
Temporary and backup files are created mode `0600` from the
outset via `mkstemp`; secrets are never written to a normally
created file. Secrets travel through the controlling terminal
only (the heredoc feeds the script, `/dev/tty` feeds the
secrets) — never command-line arguments, shell history, chat,
Git, or temporary plaintext files. There is no rotation bypass
in this block: existing, partial, or identical credentials stop
the run for owner review, and ordinary setup can never silently
become rotation.

```bash
sudo -v
sudo python3 <<'PYEOF'
import getpass, os, re, secrets, stat, subprocess, sys, tempfile, time
# WONDRUE_ENV_FILE_OVERRIDE / WONDRUE_SYSTEMCTL / WONDRUE_SYSTEMCTL_SCRIPT
# exist for offline testing only; unset, the production paths apply.
ENV = os.environ.get("WONDRUE_ENV_FILE_OVERRIDE", "/var/www/growup/shared/growup.env")
SYSCTL = os.environ.get("WONDRUE_SYSTEMCTL", "systemctl")
SYSCTL_SCRIPT = os.environ.get("WONDRUE_SYSTEMCTL_SCRIPT", "")
SERVICE = "growup.service"
ACCOUNT = "growup"
READY_TIMEOUT_S = 60
OP_KEY = "WONDRUE_OPERATOR_KEY"
RD_KEY = "WONDRUE_READER_KEY"
SPEND_KEY = "META_SPEND_APPROVED"
def stop(message):
    print("STOP: " + message)
    raise SystemExit(1)
def ask(prompt):
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with open("/dev/tty") as tty:
        return tty.readline().rstrip("\n")
def sysctl(*args):
    cmd = [SYSCTL] + ([SYSCTL_SCRIPT] if SYSCTL_SCRIPT else []) + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        stop(f"cannot run service manager: {exc}")
def split_assignment(line, key):
    match = re.match(rf"^(export\s+)?{key}\s*=\s*(.*?)\s*$", line.rstrip("\n"))
    if not match:
        return None
    body = match.group(2).strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
        body = body[1:-1]
    return (match.group(1) or "", body.strip())
def owner_name(uid, gid):
    try:
        import grp, pwd
        return f"{pwd.getpwuid(uid).pw_name}:{grp.getgrgid(gid).gr_name}"
    except (ImportError, KeyError):
        return f"{uid}:{gid}"
# --- preflight: service identity (no values printed) ---
unit = sysctl("cat", SERVICE)
if unit.returncode != 0:
    stop(f"{SERVICE} not found ({unit.stderr.strip()}); refusing to guess layout")
match = re.search(r"^EnvironmentFile=\s*(\S+)", unit.stdout, re.M)
if not match or match.group(1).lstrip("-") != ENV:
    stop("service EnvironmentFile is not the expected path; review the unit file first")
ident = sysctl("show", SERVICE, "-p", "User,Group")
props = dict(line.split("=", 1) for line in ident.stdout.splitlines() if "=" in line)
if props.get("User", "") != ACCOUNT or props.get("Group", "") != ACCOUNT:
    stop(f"service user/group is {props.get('User')}:{props.get('Group')}, expected growup:growup")
print(f"[ok] {SERVICE} runs as growup:growup and uses {ENV}")
# --- preflight: environment file safety ---
if os.path.islink(ENV):
    stop("environment path is a symlink; refusing to follow it")
lst = os.lstat(ENV)
if not stat.S_ISREG(lst.st_mode):
    stop("environment path is not a regular file")
orig_mode = stat.S_IMODE(lst.st_mode)
if orig_mode & 0o077:
    stop(f"environment file permissions {oct(orig_mode)} grant group/other access; refusing")
orig_uid, orig_gid = lst.st_uid, lst.st_gid
print(f"[info] env file {owner_name(orig_uid, orig_gid)} {oct(orig_mode)} (will be preserved)")
fd = os.open(ENV, os.O_RDONLY | os.O_NOFOLLOW)
try:
    raw = os.read(fd, 1 << 20).decode("utf-8")
finally:
    os.close(fd)
lines = raw.splitlines(keepends=True)
# --- preflight: configuration state ---
values = {}
for key in (OP_KEY, RD_KEY, SPEND_KEY):
    hits = [i for i, line in enumerate(lines) if split_assignment(line, key) is not None]
    if len(hits) > 1:
        stop(f"duplicate {key} assignments (lines {[i + 1 for i in hits]}); dedupe manually first")
    values[key] = split_assignment(lines[hits[0]], key)[1] if hits else ""
if values[SPEND_KEY] == "1":
    stop("live spending is enabled (META_SPEND_APPROVED=1); disable it before credential setup")
print("[ok] live spending is not enabled")
print(f"[info] {OP_KEY}: {'configured' if values[OP_KEY] else 'NOT configured'}")
print(f"[info] {RD_KEY}: {'configured' if values[RD_KEY] else 'NOT configured'}")
if values[OP_KEY] and values[RD_KEY]:
    if values[OP_KEY] == values[RD_KEY]:
        stop("operator and reader credentials are identical; owner review required")
    print("[ok] A distinct configured pair already exists; nothing changed.")
    print("This block has no rotation bypass; rotation needs a separately approved procedure.")
    raise SystemExit(0)
if bool(values[OP_KEY]) != bool(values[RD_KEY]):
    stop("only one key is configured; owner review required (no silent completion)")
# --- provisioning (only reached with neither key configured) ---
if ask("Generate two NEW 32-byte credentials now? Type YES to continue: ").strip() != "YES":
    stop("aborted by owner; nothing changed")
fresh = {}
for label, key in (("OPERATOR", OP_KEY), ("READER", RD_KEY)):
    generated = secrets.token_urlsafe(32)
    print("=" * 70)
    print(f"OWNER ONLY - SAVE NOW in your password manager as Grow Up {label} key.")
    print("This value is shown once and is not for sharing.")
    print("=" * 70)
    print(generated)
    pasted = getpass.getpass(f"Paste the {label} value back to install it: ")
    if not pasted or pasted != generated:
        stop("paste-back did not match; nothing was written")
    fresh[key] = pasted
if fresh[OP_KEY] == fresh[RD_KEY] or len(fresh[OP_KEY]) < 32 or len(fresh[RD_KEY]) < 32:
    stop("credentials must be distinct and at least 32 characters; nothing was written")
def install_lines(content, dest, mode, uid, gid):
    data = "".join(content).encode("utf-8")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".growup.env.", dir=os.path.dirname(ENV))
    try:
        os.write(tmp_fd, data)
        os.fchmod(tmp_fd, mode)
        os.fchown(tmp_fd, uid, gid)
        os.close(tmp_fd)
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.close(tmp_fd)
        except OSError:
            pass
        try:
            if os.path.lexists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise
stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
backup = f"{ENV}.bak-{stamp}"
install_lines(lines, backup, 0o600, 0, 0)
print(f"[ok] restricted root-owned backup: {backup}")
out = []
seen = set()
for line in lines:
    replaced = False
    for key in (OP_KEY, RD_KEY):
        parsed = split_assignment(line, key)
        if parsed is not None:
            out.append(f"{parsed[0]}{key}={fresh[key]}\n")
            seen.add(key)
            replaced = True
    if not replaced:
        out.append(line)
if out and not out[-1].endswith("\n"):
    out[-1] = out[-1] + "\n"
for key in (OP_KEY, RD_KEY):
    if key not in seen:
        out.append(f"{key}={fresh[key]}\n")
install_lines(out, ENV, orig_mode, orig_uid, orig_gid)
print("[ok] environment file updated atomically (only the two Wondrue assignments added)")
def service_active():
    probe = sysctl("is-active", SERVICE)
    return probe.returncode == 0 and probe.stdout.strip() == "active"
def probe(path):
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"http://127.0.0.1:8001{path}"],
        capture_output=True, text=True)
    return result.stdout.strip() == "200"
def wait_ready():
    end = time.time() + READY_TIMEOUT_S
    while time.time() < end:
        if service_active() and probe("/health") and probe("/ready"):
            return True
        time.sleep(2)
    return False
restart = sysctl("restart", SERVICE)
if restart.returncode != 0 or not wait_ready():
    print("RESTART/READINESS FAILED; restoring the original environment file")
    try:
        bfd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            backup_lines = os.read(bfd, 1 << 20).decode("utf-8").splitlines(keepends=True)
        finally:
            os.close(bfd)
        install_lines(backup_lines, ENV, orig_mode, orig_uid, orig_gid)
        print("[ok] original environment file restored")
    except OSError as exc:
        stop(f"RECOVERY FAILED to restore {ENV} from {backup}: {exc}; restore manually, then restart {SERVICE}")
    sysctl("restart", SERVICE)
    if wait_ready():
        stop("new credentials rolled back; service recovered on the ORIGINAL environment - setup did not complete")
    stop(f"RECOVERY FAILED: service not ready after rollback; backup preserved at {backup} - investigate before retrying")
print("[ok] service active; GET /health -> 200; GET /ready -> 200")
print("DONE: credentials installed. Next: run the validation block, then the browser steps.")
PYEOF
```

Recovery: the timestamped root-owned `growup.env.bak-*`
backup restores with `sudo cp -a <backup>
/var/www/growup/shared/growup.env` followed by `sudo systemctl
restart growup.service`; the setup block also rolls back
automatically if restart or readiness fails, and always
preserves the backup. There is deliberately no rotation bypass:
existing, partial, or identical credentials stop the run for
owner review.

Validate with this second owner-run block. It runs as the
`growup` service account, takes keys through hidden
controlling-terminal input only, and prints statuses and
PASS/FAIL — never credentials or contents. Routing follows the
Ubuntu layout: TCP to `127.0.0.1:443` (Nginx), TLS server name
`securecodeusa.local`, `Host: securecodeusa.local`, and the
existing `/etc/nginx/ssl/dev-local.crt` as the explicit trust
anchor with hostname verification and the partial-chain flag
matching the established HTTPS checks (no unverified context; a
certificate mismatch fails closed). Every request sets
`use_live: false` explicitly. Operations are harmless
preview/help calls only: previews may create one ephemeral
in-memory approval token (single-use, expiring), but nothing
executes — no dossier changes, workflow stages, outreach,
inference, discovery, synthetic events, or demo reset. The block
never sources the server environment, so no spend or acquisition
flags are inherited.

```bash
sudo -u growup python3 <<'PYEOF'
import getpass, http.client, json, os, socket, ssl, sys
HOST = "securecodeusa.local"
PORT = 443
DEST = "127.0.0.1"
CAFILE = "/etc/nginx/ssl/dev-local.crt"
if not os.path.isfile(CAFILE) or not os.access(CAFILE, os.R_OK):
    print(f"STOP: trust anchor {CAFILE} missing or unreadable by growup")
    raise SystemExit(1)
CTX = ssl.create_default_context(cafile=CAFILE)
CTX.verify_mode = ssl.CERT_REQUIRED
CTX.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
CTX.check_hostname = True
results = []
def call(method, path, key, payload):
    body = json.dumps(payload).encode() if payload is not None else None
    raw = socket.create_connection((DEST, PORT), timeout=15)
    try:
        sock = CTX.wrap_socket(raw, server_hostname=HOST)
    except Exception:
        raw.close()
        raise
    conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
    conn.sock = sock
    headers = {"Host": HOST}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["X-Wondrue-Key"] = key
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()
def check(name, ok):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name)
operator = getpass.getpass("Operator Wondrue key (hidden): ")
reader = getpass.getpass("Reader Wondrue key (hidden): ")
probe_bad = "wrong-key-probe-0001"
ask_demo = {"mode": "ask", "target": "demo", "use_live": False,
            "intent": {"intent": "start_demo", "args": {}}}
help_read = {"mode": "read-only", "target": "demo", "use_live": False,
             "intent": {"intent": "help", "args": {}}}
bodies = []
try:
    status, body = call("POST", "/wondrue/command", operator, ask_demo)
except (OSError, ssl.SSLError) as exc:
    print(f"STOP: TLS/connection failure: {exc}")
    raise SystemExit(1)
bodies.append(body)
check(f"operator ask-preview succeeds (HTTP {status})",
      status == 200 and '"preview"' in body)
status, body = call("POST", "/wondrue/command", reader, help_read)
bodies.append(body)
check(f"reader read-only help succeeds (HTTP {status})", status == 200)
status, body = call("POST", "/wondrue/command", None, help_read)
bodies.append(body)
check(f"missing key denied (HTTP {status})", status == 401)
status, body = call("POST", "/wondrue/command", probe_bad, help_read)
bodies.append(body)
check(f"invalid key denied (HTTP {status})", status == 401)
status, body = call("POST", "/wondrue/command", reader, ask_demo)
bodies.append(body)
check(f"reader escalation refused pre-execution (HTTP {status})", status == 403)
status, body = call("GET", "/wondrue", None, None)
check(f"cockpit page serves (HTTP {status})", status == 200)
sanitized = all(
    operator not in item and reader not in item and probe_bad not in item
    for item in bodies)
check("responses sanitized (no credential echo)", sanitized)
print("RESULT:", "ALL PASS - nothing executed (previews/help only; one ephemeral token at most)"
      if all(results) else "FAILURES PRESENT - review statuses above")
sys.exit(0 if all(results) else 1)
PYEOF
```

Browser steps at `https://securecodeusa.local:8443/wondrue`:
paste the operator key into the masked **Wondrue access key**
field for ask/full work, or the reader key with **read-only**
mode for inspection; a harmless first command is `help` in
read-only mode on the synthetic target. Keep **live inference
unchecked**. Both keys are Wondrue credentials, distinct from
the Baseten API key, and the cockpit never saves them. Live
inference itself remains separately authorized and is untouched
by credential setup.

### Shared inference capabilities (Baseten)

Meta runtime work flows through one shared boundary
(`app/inference.py`): natural-language interpretation, catalog
proposals, result/evidence/economics assistance, document extraction,
and Uriel drafting. Provider transport stays in
`app/model_adapter.py`; capability briefs and validators live in
`app/assist.py` plus per-capability validators in the adapter;
deterministic engineering, financial, and verification logic is never
reimplemented. `META_API_URL` accepts the Baseten API base
(`https://inference.baseten.co/v1`) or the full chat-completions
endpoint (`.../v1/chat/completions`); other chat-path spellings are
refused outright, and any other base resolves by appending the
suffix (a wrong base then fails closed at request time). The
hackathon model is `meta-models/muse-spark-open-1026`; no other model
or provider is substituted. The API key stays server-side in
`META_API_KEY` and never enters prompts, responses, logs, or files.

Enforcement is a competition-wide token-unit allowance
(`INFERENCE_TOKEN_ALLOWANCE`, default `0` = live disabled, never
auto-reset) — not a monetary ceiling. Every live call reserves a
conservative bound before any provider request, computed from the
exact bytes the transport will send (one shared request builder
sizes budgets and HTTP alike, so the two cannot diverge): one unit
per UTF-8 byte of the serialized request body (directive, context,
labels, and chat framing; image data URLs ride in the transmitted
body but are excluded from the sized bytes), plus max_tokens for
the completion, plus the
explicitly configured per-image allowance for each supplied image
(`INFERENCE_TOKENS_PER_IMAGE`, no default — live media without it
is refused). The byte bound is a stated conservative assumption,
not a proven tokenizer ceiling for the configured model:
byte-pair-style tokenizers typically stay far below one token per
byte, but no guarantee is claimed without tokenizer support;
honesty comes from reconciliation, not from the bound itself —
reported usage past the reservation is charged back as excess, so
underestimation cannot hide spend. Request limits come from one
centralized throttle profile (`INFERENCE_PROFILES`, default
`medium`), shared by every capability and requesting agent:

| Setting | Profile | Max completion tokens | Max serialized input bytes |
|---|---|---|---|
| Low | `low` | 1,024 | 8,192 |
| Medium | `medium` | 2,048 | 16,384 |
| High | `high` | 4,096 | 32,768 |
| Extra High | `extra_high` | 8,192 | 60,000 |

These are application resource profiles, not measures of
intelligence or accuracy — larger settings permit more context and
completion space but guarantee nothing about result quality. The
cockpit offers one selector (request-scoped, saved immediately
to browser-local memory on change; only recognized values are
restored, anything else falls back to Medium, and the page keeps
working when storage is unavailable); API callers pass one
validated `profile` field. A separate `Inference setting`
indicator names the active profile next to the model badge,
independent of the synthetic/real target banner. Unknown profiles are rejected server-side before any
reservation. The complete serialized prompt/directive/framing bytes
count against the input limit; oversized context is refused with
guidance (narrow the request or choose a larger setting) — never
silently truncated, never auto-upgraded, never retried. Profiles
never grant permission or enable spending, and read-only mode
still cannot make paid calls. Administrator ceilings
(`META_MAX_TOKENS`, `INFERENCE_MAX_PROMPT_CHARS`) can only reduce
profile limits; requested-vs-effective is reported whenever a cap
bites, and the transport clamps through the same completion
ceiling. Reservation is one atomic
conditional update so concurrent
callers cannot overspend; HTTP runs outside long-held
workflow/custody transactions (reads happen before, writes after,
and no workflow/custody locks are held across the provider call).
Reported usage is reconciled afterward, and overruns are charged
back to the same allowance — excess reported units never disappear
from budget totals (`excess_units` is persisted per row).
Consumption states stay distinct: bound, reserved, actual, and
unresolved. Billing exposure follows the failure taxonomy, not the
error text: confirmed pre-provider refusal (connection refused, DNS,
TLS — the request never reached the provider) releases its
reservation at zero spend; provider rejections (HTTP 4xx) fail with
reported usage preserved; timeouts (detected by type, never by
message sniffing), mid-stream interruptions, response-size aborts,
malformed envelopes, HTTP 5xx, and semantically rejected output
with missing usage stay explicitly `unresolved`, keeping the
reservation plus any available request IDs, reported usage, and
cost estimates — never assumed zero, never auto-retried, and a
ledger warning discourages retrying under a fresh key without
reviewing for double charges. Only pre-provider refusals
(including the concurrency bound) release; the concurrency bound
is process-local, protecting one worker, not a multi-worker
deployment. Pricing is a required owner-configured **provisional**
basis: reference input/output rates per million tokens plus a
safety multiplier (`INFERENCE_USD_PER_1M_INPUT=1.25`,
`INFERENCE_USD_PER_1M_OUTPUT=4.25`,
`INFERENCE_PRICING_MULTIPLIER=2.0`, status/source/date), taken
from the standard Muse Spark Open reference prices ($1.25/M
input, $4.25/M output at
`https://www.baseten.co/library/muse-spark-open/`, checked
2026-10-30) — planning rates are therefore $2.50/M input and
$8.50/M output. The hackathon model stays
`meta-models/muse-spark-open-1026`; the provisional rate is not a
guaranteed upper price, and the advertised $100 is neither a
verified remaining balance nor permission to consume it.
Estimates use separate input/output rates when reported usage
permits, else conservatively price the reported total at the
higher output rate with a labeled fallback; reasoning tokens are
never added on top of completion usage. All arithmetic is
deterministic `Decimal`. The retired blended-rate variable
(`INFERENCE_USD_PER_1K_TOKENS`) must stay unset — its presence
fails closed rather than silently overriding the new basis. Four
figures stay distinct: reserved token-unit exposure,
provider-reported usage, application cost estimate, and actual
provider charges (unconfirmed). Consumed-fraction
thresholds: 50% warns with usage by agent/capability; 75% defers
optional requests (audience variants) visibly instead of spending;
90% requires an ask-mode approval token for large batches and
protects the demonstration reserve (`INFERENCE_DEMO_RESERVE`);
100% refuses further spending until the owner raises the
allowance. Every live call and idempotent replay is recorded in
the `inference_usage` request-status ledger (migrations 016, 017)
with request/idempotency identity, principal, agent, capability,
dossier/run/package association, provider, model, prompt/schema
versions, digests, profile plus requested/effective limits,
bound/reserved/reported/excess units, estimated cost with pricing
provenance, outcome, live/replay status, and timestamps; an
observability activity event is emitted per call. Before
execution, previews disclose the setting, effective limits, max
calls for that capability (one), maximum reservation and
planning-cost exposure, pricing status, and remaining allowance
with threshold warnings; after execution, responses carry
reported usage, estimate plus status, unresolved exposure, and
live/replay state. Whole-request previews separately report
whether interpretation already ran and the combined call ceiling:
a structured capability call spends at most one provider call,
while a natural-language request may spend one more for command
interpretation. When images are supplied but the per-image bound
is missing or invalid, previews mark media execution unavailable
and reservation/cost totals unknown rather than presenting a
complete-looking estimate that excludes image consumption;
execution stays fail-closed. Dossier
association is deterministic: an explicitly supplied, validated
`dossier_key` wins; otherwise an unambiguous stored
run→dossier association is selected; ambiguity requires the
caller to choose (`assist_explain` and extraction accept
`dossier_key`); null is recorded only when no stored association
genuinely exists; versions derive from stored lineage, never
invention. Extraction verifies a caller-supplied dossier link
against stored records — an unverified assertion is labeled
caller-asserted, not written as ledger fact. Rows transition
`reserved` → `completed`/`failed`/`unresolved`/`rejected`/
`released` exactly once — the ledger tracks status, it is not
append-only. Replays are labeled `replay`, record zero new usage,
and link the original spend through `replay_of`; reuse binds the
full original identity (principal, capability, model, versions,
profile plus effective limits, dossier context, payload digest),
not the payload alone — a changed profile needs its own key.
Ask-mode approval binds the full inference specification for
assistance commands: intent, target, approved arguments,
live/replay selection, canonical profile, effective completion
and input limits, and the authenticated principal. Redemption
re-resolves the current spec and rejects any material drift —
including effective limits that shifted because administrator
configuration changed after preview — before provider execution.
Tokens stay single-use; extraction keeps its canonical-spec
comparison.
Usage summaries report consumed units (reserved plus excess,
excluding releases) separately from released units, reconciling
with the singleton allowance across reserve, release, replay,
failure, and overrun.

| Capability | Wondrue path | Inputs | Limits |
|---|---|---|---|
| Conversational assistance | `POST /wondrue/command` text (no `intent`), `assist_explain` | run id, record key, optional question | validated commands only; read-only never live; stored facts/estimates/assumptions/unknowns labeled |
| Evidence analysis | `analyze_evidence` | run id | citations must name supplied evidence with matching excerpts; suggestions only, no verification or auto-research |
| Engineering/finance assistance | `compare_scenarios`, `explain_economics` | run id, scenario | catalog-validated model proposals; deterministic Onesimus/Michael math only; explanations cite figure labels |
| Document extraction | `POST /wondrue/assist/extract` | doc id, source ref, inline pages (PNG/JPEG/WebP ≤5MB with magic-byte check, or extracted text), optional run/dossier link | ≤12 pages, ≤6 images, 8MB aggregate; ask-mode approval binds the full digest-pinned spec; originals retained in DB; no URL fetching, no PDFs, no floor-plan approval inference; claims unverified |
| Uriel drafting | `draft_outreach` | eligible package entry, draft kind | prepared/reviewed genuine packages only; drafts never send, publish, or automate; website/video/image-review kinds excluded |
| Budget inspection | `inspect_inference_usage` (read-only) | none | allowance state plus usage by agent/capability |

Every live request carries a versioned directive (instructions,
explicit output schema, approved catalog where needed) alongside the
untrusted context; validators enforce the transmitted contracts, and
a mocked transport assertion pins the outgoing shape. Evidence
conflicts and gaps must cite supplied evidence verbatim
(research questions may be uncited); economics commentary cites
figure labels while the server renders authoritative values, and
money-like figures in model prose are rejected; draft briefs carry
unknowns/blockers with explicit omission counts. Extraction approval
binds identities, page digests, association, live selection,
profile plus effective limits, and idempotency key — resubmitted
operations must match exactly.
Page originals persist in `extraction_originals` under database
access control with conflict-safe retention: each page inserts
atomically, identical resubmission reuses the retained original
(zero new inserts), and conflicting content at the same locator is
refused — racing writers cannot both appear successful, and claims
never attach to a different retained original. Run/dossier links
are checked against stored identity (stored, caller-asserted, or
none). Successful run-scoped results attach as
new `wondrue_assistance` workflow records plus `model_execution`
provenance; authoritative dossier contents and historical snapshots
are never mutated, and nothing becomes a dossier revision. Provider
usage and request metadata are extracted before output parsing, so
truncation, malformed JSON, and refusals still reconcile billing
evidence; responses stream through a mid-read size bound.
Always-on model reasoning can consume completion space: a truncated
response fails safely and never automatically triggers a larger
profile. Budget,
provider, or validation failure changes no verification state,
advances no custody, and authorizes nothing. Responses are JSON
only; the static results page echoes no model text. Image-vision
support depends on the deployed model and is verified by a
separately authorized live image probe, not assumed; text pages are
the supported fallback.

### Shared dossier custody

`app/custody.py` + `app/custody_db.py` give every operational property one
shared dossier identity (a deterministic UUID derived from the property
UUID — no registry, no merges). Versions append monotonically and are
never rewritten; corrections arrive as permission-checked amendments
(enrolled `CUSTODY_AMENDERS` authorizer + reason, chain re-verified
before writing) that supersede the latest version. Before any handoff,
the version is frozen into an immutable snapshot; the handoff itself is
an HMAC-authenticated receipt chained to its predecessor. Forward,
return, and human-review handoffs share one contract with different
routing rules (downstream, upstream, escalate-to-`human_review`); a
forward naming a superseded version is refused as stale, while returns
and reviews on older versions are flagged as not-latest. Tampered bytes
fail verification loudly.

The canonical pipeline order is Gabriel → Raphael → Onesimus → Michael
→ Uriel → Priscilla (Wondrue coordinates; she holds no custody).
Receipts carry a pipeline version (`mvp-1` for pre-canonical history,
`canonical-1` for new handoffs) so old rows keep their original meaning;
issuing requires the holder to match the sender principal, enforced
server-side. The shared HMAC proves integrity within the trust boundary,
not origin — sender identity and custody ownership are checked
separately.

Chain order is authoritative from **predecessor links only**: chain
validation, latest-receipt/tip selection, current-holder derivation, and
pending-review selection all walk `prev_receipt` from the chain root.
Timestamps and receipt-UUID ordering are never consulted — within one
PostgreSQL transaction every receipt shares `CURRENT_TIMESTAMP`, and
random UUIDs order arbitrarily. Validation requires a unique root and a
single connected chain; forks, cycles, missing predecessors, and
cross-dossier prev-links are rejected as tampering. Receipt issuance is
serialized per dossier with a transaction-scoped advisory lock
(`pg_advisory_xact_lock` on `custody:{dossier_id}`) taken before the
holder read and the append, so concurrent issuers cannot race the tip.
No schema change was needed: existing rows already carry the
predecessor links and MACs, and historical records were not rewritten.

Owner-run validation against disposable PostgreSQL 16 database
`growup_mvp_gate` initially surfaced five custody failures (11 passed):
tied timestamps plus random UUIDs broke chain ordering, produced
wrong-holder assertions downstream, and an archival fixture tried to
`UPDATE` an immutable snapshot row. The ordering/serialization fixes
above and an aged-snapshot test fixture (inserted historical, never
updated) resolve them; the confirmed `2aebc93` checkpoint passed
full regression (2,097 passed, zero failures/skips),
migrations 001–017 on both databases, and operational health
checks with the cockpit preference/status work deployed; HTTPS
routing works and owner browser acceptance passed. This tree
holds only the credential procedure (no code changes): owner
credential setup, service restart, validation, and the live
smoke test are **pending owner execution**.

Snapshots older than 90 days move to the archive table via
`archive_snapshots()`; retrieval checks the hot table first, then the
archive, and archived snapshots stay verifiable. Versions, receipts,
packages, and conversion events are permanent and are never moved or
deleted — database triggers enforce this.

### Historical pilot ledger (retired as an operating workflow)

`app/pilot.py` tracks the 12-county × 3-category pilot (four qualified
dossiers per cell, 144 total). The ledger is wired to actual processing:
Gabriel persistence records discovered/qualified, Raphael decisions
record assessed plus authorized/rejected/incomplete, Onesimus scenarios
record engineering_assessed, Michael assessments record
financially_assessed, Uriel packages record package_prepared, and
Priscilla events record tracked_actual/tracked_simulated — each under
its own counter (`michael_ready` is a retained legacy counter). Each
(dossier, transition) counts at most once, so replays and resumed runs
cannot inflate counts; unknown-county dossiers stay researchable but earn
no cell credit, and synthetic identifiers are never counted. Category
weights start equal and only order tied cells. Planning never executes
or spends: it orders open cells by truthful shortfall and annotates each
candidate action with its estimated Brave cost and a monthly-budget
verdict. The aggregate shortfall sums per-cell deficits, so an
overfilled cell never conceals another cell's shortfall.

Quota credit keys on the canonical operational dossier identity
(`canonical_quota_id`, derived from `dossier_id_for_property`) — never
on run-scoped derivations — and only rows marked `identity_kind =
'canonical'` count. Pilot success requires **all 36 cells** to each
hold at least four distinct canonical qualified operational dossiers;
144 records concentrated in too few cells, or cross-run duplicates of
the same property, do not satisfy it. Discovery, qualification,
Raphael authorization, engineering, financial, package, and
simulated/actual tracking stages stay separate: a completed discovery
quota is not proof that 144 outreach packages qualify.

```bash
python -m app.pilot status          # totals, stages, shortfalls, budget
python -m app.pilot plan --max 12   # ordered next actions (planning only)
python -m app.pilot stop            # halt planning; history preserved
python -m app.pilot resume          # continue exactly where it stopped
```

Recording completed outcomes is allowed while stopped; only planning is
gated. The pilot ledger itself never launches work — the bounded
discovery runner (below) executes bursts and reads the ledger for
cell ordering and stop conditions, while the Gabriel/Raphael/Onesimus
/… hooks record what actually happened.

### Historical bounded discovery configuration (superseded by source-first contract)

`app/discovery_runner.py` runs repeated bounded discovery bursts
through the existing OSM adapter, budget controls, and Gabriel
persistence — one runner, two policies. **Pilot** serves unmet
36-cell qualification requirements first and stops when the quota is
satisfied or a limit is reached; **timed** keeps
discovering/reassessing appropriate real candidates within an approved
duration and budget even after the quota is met. Controls: one active
worker per scope (partial unique index; a second claimer gets `busy`
and does no work), durable per-pass checkpoints with idempotent
resume, per-invocation pass/duration/created bounds, provider pacing
with bounded backoff, abort on authentication failure or repeated
provider failures, no blind retries of ambiguous charged attempts,
ownership fencing (owner token + heartbeat; a stalled row is adopted
only after its lease lapses, and stop requests keep the slot so no
second worker overlaps an in-flight pass), checkpointed evaluations
that are never re-driven once terminal, and a Sabbath guard that
refuses/pauses inside an owner-configured blackout evaluated in
`America/Chicago` (`SABBATH_BLACKOUT`, `SABBATH_START`, `SABBATH_END`;
defaults Fri 18:00–Sat 20:00 Chicago). The window is a configured
approximation, not an astronomical sundown calculation — adjust it for
the season, and set `SABBATH_BLACKOUT=0` only with explicit operator
authorization. Without `tzdata` the guard fails closed.

Every created id is enqueued in a durable evaluation backlog
(checkpoint `backlog`, saved before any per-pass cap applies), and
the backlog head — never a later pass's rediscovery — is what resume
drives through the existing coordinator (`evaluate_property` →
`start_evaluation_audited` with a deterministic
`runner:{scope}:{property_id}` assignment for the discovered property)
when acquisition is approved and the workflow is enabled. Terminal
outcomes (`evaluated`, `blocked`, `skipped`) leave the backlog;
`error` stays queued for resume. Reports keep `created` (property
rows) separate from `evaluated` and `qualified_dossiers`, and truthful
`needs_evidence` results are the normal outcome, not a failure.
Address-less, missing, or invalid properties are skipped with reasons;
ambiguous prior attempts block rather than retry with the same
assignment id. Each pass checkpoints the backlog before evaluating,
so a failed checkpoint write recovers through the finish-path re-save
instead of losing candidates; a process crash between discovery's own
commit and that save is closed by durable pass attribution: every
pass stamps a deterministic discovery run id and saves it as the
checkpoint's `pending_pass` *before* discovery runs. On resume the
runner folds that exact run's permanent decision events back into the
backlog — `created` outcomes with a `COMMITTED` mutation and a valid
property UUID only, event-store reads, no provider calls and no new
property records — and records the folded run in `discovery_runs` so
it is never folded twice. The fold restores the pass's total,
per-cell, and per-unit creation counts plus its pass sequence and
request accounting (units resolve from the stamped unit key, or
unambiguously from the cell for older checkpoints), so a crash
after two committed creations under a two-property unit limit
cannot permit a third after restart; only newly enqueued ids
restore counts, so a re-fold cannot double-count. A discovery
call that raises folds its partial committed creations through
the same accounting and consumes its sequence, so the next
pass never reuses the failed run id. Discovery limits
(creation ceiling, pilot
quota) stop new discovery only: a pending backlog keeps draining in
bounded rounds under the same duration, stop-request, and
per-pass-evaluation limits, so a creation ceiling never strands queued
work. If the event store is unreadable, recovery aborts rather than
proceeding blind.

```bash
python -m app.discovery_runner preview --scope pilot-dfw \
    --envelope config/dallas_opening_envelope.example.json
python -m app.discovery_runner run --scope pilot-dfw --policy pilot \
    --cells cells.json --max-passes 3 --max-duration 1800 --max-created 100 \
    --max-overpass-requests 3
python -m app.discovery_runner status --scope pilot-dfw   # latest run + checkpoint
python -m app.discovery_runner stop --scope pilot-dfw     # pause at next pass boundary
```

Cells are operator-supplied `(county, category, bbox, source)` JSON
with canonical county/category names and finite south<north,
west<east windows; placeholders are never silently expanded and
non-canonical cells or degenerate windows are refused at validation,
never widened or reinterpreted. City names in cell metadata are
targeting notes, not proof of city coverage. The approved Dallas
opening probe ships as two reviewable examples: the unit list
(`config/dallas_opening_units.example.json`) and one complete
envelope (`config/dallas_opening_envelope.example.json`) pinning
units plus every bound — three units (`DFW-DAL-001-H/C/W`)
sharing one downtown window (32.770/-96.810/32.790/-96.785)
across the three categories with equal priority, at most 3
passes, 6 created (2 per unit), 1 discovery attempt per unit, 3
Overpass / 6 Brave / 6 Census requests, 12-second timeouts, no
retries, zero Meta and zero outreach — a bounded coverage
probe, not complete Dallas coverage, and not installed or
activated anywhere. The envelope loader is strict (unknown
keys, nonzero Meta, or enabled outreach refuse), and `preview`
and `run` load through the same function, so the shown
envelope is the enforced envelope; only scope, resume,
created-by, and the acquisition grant ride the command line.
The `preview` subcommand validates and prints the effective
envelope without claiming, transmitting, or writing. The same
bursts are available through Wondrue (`start_discovery` /
`resume_discovery` / `stop_discovery` / `inspect_discovery`,
request-safe caps, including the per-unit attempt bound). A proposed
two-hour timed preset (`TIMED_TWO_HOUR_PRESET`: 7200 s, 48 passes,
400 created) exists in code but is **not launched** — duration and
any spending approval are separate from implementation approval, and
launch additionally requires owner-verified cells. Coordinator
evaluations need explicit acquisition approval: the
`--acquire-approved` CLI flag, or a Wondrue burst with `acquire: true`
from the operator grant (ask mode previews the bounds first; see
below). Without it the runner creates property records and reports
`evaluations_precondition` instead of driving them. Each evaluation
shares the coordinator's budget guards (one shared monthly unit per
transmitted request plus the per-run bound, default 9); narrow them
with `--evaluation-attempt-limit 1..18` and `--evaluation-timeout
SECONDS` (or the Wondrue burst args `brave_attempt_limit` /
`timeout_seconds`) — the first owner-run evaluation uses limit 1
with a 12-second timeout (see the acquisition-budget section
above).

Discovery progresses durably through the configured cells instead of
repeating the same query: each search unit is a cell plus its exact
bounding box and source, and checkpoints record per-unit pass
counts, created totals, and exhaustion with the window limit that
produced it. A zero-create pass exhausts that exact query (the same
request deterministically returns the same window and the dedup
store only grows, so a repeat could never create more); only a
wider window reopens the unit, and any created property clears
exhaustion. Under the pilot policy, units whose cell already holds
four credited dossiers are skipped; timed reassessment attempts
configured units regardless. No burst expands scope, subdivides
boxes, or switches providers on its own. There is no
self-launching loop: progression persists across owner-launched,
approval-gated bursts, and each burst stops at its own bounds. The
pilot ledger's stop gates planning only and the runner's stop pauses
the worker — neither stops the other.

Request budgets are stated up front and enforced separately per
provider: Overpass discovery is capped by `--max-overpass-requests`
(Wondrue `max_overpass_requests`; default one per pass, failed
attempts spend the budget too), and coordinator evaluations halt
before exceeding `max_passes × max_evaluations_per_pass ×
evaluation_attempt_limit` Brave attempts, checked between
evaluations alongside the duration and stop-request checks
(in-flight evaluations run to their own timeout and may land just
past a bound). Census county admission is capped by
`--max-census-requests` (Wondrue `max_census_requests`, at most
25 per Wondrue burst) with at most `--census-attempt-limit`
(`0..1`, default 1) per evaluated property — and unlike
Overpass it has no per-pass default: county admission transmits
only inside an explicitly set envelope. Evaluations past the
Census budget run without county admission (reported as
`census_preempted`) rather than transmitting. Each discovery
pass is also clamped to the remaining creation allowance and,
when `--max-created-per-unit` is set (Wondrue
`max_created_per_unit`), to the unit's remaining cap; capped
units are skipped under every policy with an explicit reason.
`--max-discovery-attempts-per-unit` (Wondrue
`max_discovery_attempts_per_unit`) caps discovery attempts per
unit absolutely — no wider window, exhaustion reset, or unmet
quota reopens a spent unit, and failed attempts count. Reports
state the computed `request_bounds`,
actual `overpass_requests`, `brave_requests`, and
`census_requests` spend,
created/evaluated/qualified counts, newly credited dossiers,
per-cell credited totals and deficits, aggregate `remaining_quota`
(summed from per-cell deficits, so an overfilled cell never hides
another cell's shortfall), Raphael decision tallies, exhausted
units, per-unit states (`units`: attempts, created, exhaustion,
reason — no unit is silently skipped or repeated beyond its
allowance), and unresolved items (queued backlog depth, blocked
and error counts). The final report always re-reads canonical
counts from the same source before tallying, so credit earned by
the last allowed round is included; if that read fails, quota
progress is reported unavailable (no per-cell rows,
`remaining_quota` and `credited_new` null) rather than stale
counts presented as current. Reaching a pass limit is reported
as a limit, never as pilot completion.

### Uriel — preparation-only packages

`app/uriel.py` prepares genuine opportunity/campaign packages: dossier
lineage, evidence references, Raphael decisions, engineering comparisons,
Michael economics, unknowns, blockers, and next actions. Preparing
requires a custody receipt handing the exact dossier version to Uriel;
each dossier version gets one append-only outcome row (`draft`,
`reviewed`, or `prepared`, decided at insert). Packages are genuine in
both demo modes and never labeled synthetic; execution state stays
`not_attempted` because no sender exists. Inspect with
`python -m app.uriel list [--state S]` /
`python -m app.uriel show <entry_id>`. There is no outreach function to
call: sending, scheduling, or approving external action is not built.

### Priscilla — simulated vs actual tracking

`app/priscilla.py` records delivery, engagement, response, and conversion
events against Uriel packages. Simulated demonstration events are allowed
only with `DEMO_MODE=yes`, must be labeled simulated, and may reference
real package IDs without implying real recipients responded. Actual
events require explicit evidence references and must not carry simulated
labels. Metrics report simulated and actual counts in strictly separate
buckets — no combined total is offered. Switching modes replays nothing
and converts nothing: rows are append-only and keep their kind.

Deterministic cohort simulation (`priscilla-sim-1`) freezes one cohort
of eligible genuine prepared packages (custody, verification,
engineering, and financial handoff rules satisfied; blocked, draft,
synthetic, and duplicate records excluded with reasons), then derives
outcomes by fixed decimal arithmetic: `responses = floor(N × 0.10)`,
`customers = round_half_up(responses × 0.36)` capped at responses, with
reproducible seeded selection (`seeded-sha256-prefix`) over the frozen
package order — outcomes never reroll on refresh or retry. For N=144:
144 simulated outreach events, 14 responses (≈9.72%), 5 customers
(≈35.71%). Smaller cohorts simulate against their actual size and
disclose the shortfall; a changed cohort or configuration needs a new
run. Each run persists its simulation id, idempotency key, frozen
cohort, assumptions, and status. Idempotency binds the key to the
material inputs (cohort label, frozen package selection, seed,
configuration): identical retries replay the stored run, conflicting
reuse is rejected, and `ON CONFLICT DO NOTHING` keeps concurrent
insertion safe under PostgreSQL semantics — the loser re-reads the
winner's row and runs the same validation. The unique index over
(simulation, package, kind) additionally makes event-row writes
non-duplicating. A simulation never changes dossier facts, financial
results, actual metrics, or Uriel execution state.

The unique-lead funnel counts each lead once per stage no matter how
many event rows it has: cohort packages → simulated outreach →
simulated responses → simulated customers (conversions ⊆ responses ⊆
cohort, verified by `subset_consistent`), alongside cohort-scoped
actual leads and exclusions — actual and simulated stay separate.
Preview with `preview_simulation` (read-only: cohort, assumptions,
expected counts), execute with `run_simulation`, inspect with
`inspect_simulation`; Wondrue also lists eligible packages via
`inspect_packages`. `preview_simulation` and `run_simulation` accept
an optional `package_entries` allowlist only as a JSON-encoded list
string — intent args are scalar-only, so native nested lists are
rejected.

### DEMO_MODE

`DEMO_MODE=yes` (the default) means prepare and analyze using real data,
but simulate downstream external execution: Gabriel, Raphael, Onesimus,
and Michael stay genuine, Uriel prepares genuine packages without
sending, and only Priscilla may record explicitly simulated events.
`DEMO_MODE=no` additionally forbids simulated events; it still cannot
send anything, because no sender is implemented. Unset or blank means
`yes`; any other value is rejected loudly. The switch affects
simulated external execution only — Gabriel, Raphael, Onesimus, and
Michael behave identically in both modes (covered by acceptance
tests), and live mode alone grants no spending, sending, or execution
authority. The synthetic upstream harness remains a separately
selected diagnostic fallback (restricted after the first evidenced
pilot; see above). Retiring demo entry points is a future
owner-authorized release decision; historical simulation labels,
audit records, and regression fixtures are preserved.

### MVP limitations

No self-launching autonomous execution: discovery progresses durably
across owner-launched, approval-gated bursts (see bounded discovery
above). No exhaustive city searches, OAuth/admin systems, outreach
execution, additional agents, advanced underwriting, or detailed
permitting. Synthetic records never count toward the 144-dossier
target. `michael_ready` pilot counter and pre-canonical `mvp-1` receipt
rows are retained for history, not live use. Positive real-property
authorization still needs reviewed registry curation plus aligned
identity evidence. Pilot cell credit requires verified county
attribution backed by a Census collector claim (bare verified
labels earn nothing at either credit gate); listing-derived
county evaluation stays conservative and the generic evaluator
caps at corroborated. Live Census compatibility and real county
results are unvalidated — only separately approved
owner-executed retrieval proves them. Known residuals:
pending-pass recovery depends on reading the interrupted run's
permanent decision events from the event store; an unreadable
store aborts the burst rather than proceeding blind (see
bounded discovery above); a crash between Census transmit and
retention loses the bytes under an uncertain key (the
allowance stays consumed); and resolve-then-connect is not IP
pinning. Census allowance accounting is crash-safe (reserved
in the claim commit before transmission — no undercount, no
double-count, no reopening). Validation gates
(each needs
its own owner approval): owner Wondrue credential setup and
service restart (procedure below; checkpoint `2aebc93` was fully
green: 2,097 full with zero failures/skips, health checks
passed, HTTPS routing works, browser acceptance passed); the
acquisition-budget tree in this worktree (targeted rerun, full
regression, the seven `GROWUP_PG_INTEGRATION`-gated acquisition
tests, owner first-acquisition configuration, and health checks —
all pending); a bounded live smoke test proving meaningful Meta
runtime inference (see Demonstration); real discovery runs
(the two-hour timed preset is defined but not launched); the
pilot-readiness tree (attribution wiring, durable progression,
per-provider request bounds, aggregate-shortfall fix,
property-bound category observations, verified-category
qualification, fresh final quota reporting); and the Census
county-admission tree in this worktree (controlled collection,
county-only assessment, dossier/successor integration, Dallas
opening units, enforced opening envelope, plus the six-finding
correction pass: dossier-address binding, crash-safe claim
charging, creation-accounting recovery, successor lineage,
claim/evidence preservation, complete envelope — full offline
suite green at 2,184 passed / 146 skipped / 17 warnings, owner
PostgreSQL/Ubuntu validation pending including the three
Census PG checks).

## 🎥 Demonstration

Three-minute walkthrough: evidence → actual/simulated decision →
configurations → deterministic economics and unknowns. Open
`GET /wondrue` (ask mode, synthetic target) or drive
`POST /wondrue/command` directly with structured `intent` payloads.

**A. Synthetic demonstration (always available, no database needed).**
Send `start_demo` (full mode, or ask mode with the approval token) to run
the fictional Fiction Tower fixtures end to end: Gabriel stages evidence,
Raphael records a simulated authorization, Onesimus evaluates `bench`
vs `rack3` with invented parameters clearly marked, and economics come
back computed with explicit unknowns. Every response and export carries
**SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY; VERIFICATION AND
AUTHORIZATION ARE SIMULATED.** Reset with `POST /wondrue/demo/reset`;
real records are untouched.

**B. Real-property evaluation (needs PostgreSQL + approvals).** With
`MVP_WORKFLOW_ENABLED=1`, send `start_real_evaluation` with an
`assignment_id` and planner-issued `query` (optional
`brave_attempt_limit`, `timeout_seconds`, and `property_id` args;
the first acquisition uses limit 1 with a 12-second timeout —
see the acquisition-budget section). Gabriel acquires evidence
within the run's Brave bound, persists artifacts/dossier/qualification,
and Raphael applies the real policy: expect `needs_evidence` until
reviewed registry curation and aligned identity evidence exist — that
stop is the correct fail-closed outcome. Use `inspect_*` /
`explain_decision` (read-only safe) to review persisted records, `stop`
to halt a run. `compare_scenarios` is a hypothetical what-if comparison
(model proposes catalog options, deterministic Python prices them);
because it can issue a Meta call it is consequential (ask-with-token
or full, never read-only). Persisted Onesimus scenarios are the
artifacts that require a Raphael authorization covering the exact
dossier version. Once genuine packages exist, `inspect_packages` →
`preview_simulation` → `run_simulation` → `inspect_simulation` walks
the deterministic cohort funnel (for N=144: 14 responses, 5 customers);
simulation writes simulated rows only and changes no dossier, finance,
actual-metric, or execution state.

**C. Assisted capabilities (needs PostgreSQL + approvals; mockable).**
With a persisted run, send `assist_explain` (stored brief plus why the
property stopped), `analyze_evidence` (citation-checked conflicts, gaps,
research questions), and `explain_economics` (labeled Michael figures
explained); with a prepared package, send `draft_outreach` (draft copy
that never sends); with owner-supplied pages, post
`POST /wondrue/assist/extract` (page-validated claims, unverified).
All are consequential (ask-with-token or full, never read-only) and
spend at most one model call each from the shared budget; without
`use_live`, labeled replays rehearse the same shapes for free.
`inspect_inference_usage` (read-only) shows allowance state and spend
by agent/capability.

Model output is labeled `live` or `replay` in every response; read-only
mode never issues live calls. Live Meta, Brave, and Census calls were not
executed during implementation (pending explicit owner authorization;
the only Census contact was reading the public API documentation and
the static county reference file while implementing).
Mocked success is not proof of live compatibility: the bounded live
smoke test below stays owner-executed.

**Bounded live smoke test (proposed, not executed).** After migration
017 and regression are green, the owner enables `META_SPEND_APPROVED=1`
with an explicit allowance and runs, in order, one
`interpret_command` ("help me"), one `assist_explain` over a stopped
run, one `analyze_evidence`, one `explain_economics`, one
`draft_outreach` (`intro_letter`), and one text-page
`extract_document` — six live calls, all at the `medium` profile
(2,048 completion / 16,384 input bytes), each with a distinct
idempotency key, then one repeated key proving replay-without-spend.
Each reservation is transmitted text bytes + 2,048 completion
units (no images in these six). Measured over representative
fixtures with the actual request builder: interpret_command 3,657;
assist_explain 3,426; analyze_evidence 4,414; explain_economics
3,643; draft_outreach 3,559; extract_document 4,330 — **23,029
units total**. These are representative measurements, not
worst-case limits: a medium call can reserve at most 18,432 units
(16,384 + 2,048). Suggested ceiling: **50,000 token units** —
2.2× the measured sum, with per-call overruns and retained
unresolved exposure charged back to the same allowance. Maximum
planning exposure if every reserved unit billed at the higher
planning rate is 23,029 × $8.50/M ≈ **$0.20**; the allowance is a
token-unit spending gate, not a bill, and only provider-reported
usage is ground truth for spend. **Stop conditions:** halt on the
first failure or unresolved billing outcome, on any refusal, or
if cumulative reservations approach the ceiling; review the ledger
before any retry (a fresh key may double-charge). Expected: six
`completed` ledger rows with reported usage and provisional cost
estimates, one zero-spend `replayed` row linked through
`replay_of`, all outputs schema-valid, `wondrue_assistance`
artifacts attached, and no verification, custody, or
execution-state changes. A single one-image extraction follows as
a **separately authorized** probe only after the owner sets
`INFERENCE_TOKENS_PER_IMAGE` (live media is refused without it),
with a top-up sized to the configured media bound plus ≈4K
text/completion units; vision is confirmed only if the model
answers it, otherwise text pages remain the supported path.

**Demonstration limitations.** The walkthroughs above show software
behavior, not findings: the synthetic path uses fictional fixtures with
simulated verification, and the real path is expected to stop at
`needs_evidence` until reviewed registry curation exists. Neither path
executes discovery autonomously, spends budget, performs outreach, or
counts toward the 144-dossier target (only qualified operational
dossiers recorded in the pilot ledger do that). Custody, pilot,
Michael, Uriel, Priscilla, funnel, runner, coordinator, and shared
inference flows are exercised by the acceptance suites
(`tests/test_custody.py`, `tests/test_pilot.py`,
`tests/test_michael.py`, `tests/test_uriel.py`,
`tests/test_priscilla.py`, `tests/test_priscilla_sim.py`,
`tests/test_modes.py`, `tests/test_wondrue.py`,
`tests/test_wondrue_ops.py`, `tests/test_discovery_runner.py`,
`tests/test_mvp_flow.py`, `tests/test_funnel_acceptance.py`,
`tests/test_inference_assist.py`); live-PostgreSQL variants run only
where `GROWUP_PG_INTEGRATION=1` and migrations 001–017 are applied
to a disposable database (never the operational records). The
cockpit preference/status rerun, owner browser acceptance, the
bounded live smoke test, and real discovery launches remain
unexecuted owner-approval gates.

---

## 🔭 Future Vision

Beyond the initial hackathon, Grow Up may expand to support:

- Additional geographic markets
- More sophisticated property discovery and analysis
- Real-world conversion project management
- Advanced financing and redevelopment opportunities
- Vacant-lot agricultural projects and food forests
- Purpose-built agricultural buildings

Planned (not implemented) agent capabilities: a Regulatory & Permitting
researcher, an Underwriting evaluator, Simeon (deal coordination and
owner-authorized closing steps only — no autonomous contracts, financing
commitments, securities offerings, ownership transfers, or construction
execution), a Construction coordinator, Gabriella (vacant-lot discovery
reusing Gabriel's infrastructure), and outdoor food-forest configurations
for Onesimus. Named robotics vendors, if any appear in planning notes,
are prospective examples, not partners. Asset management, accounting/tax,
insurance/liability, and legal-support agents stay deferred.

These are longer-term goals, not claims about current functionality.

---

## ⚠️ Important Disclaimer

Grow Up provides exploratory analysis and hypothetical agricultural-conversion scenarios.

Its outputs are not substitutes for professional engineering assessments, structural inspections, zoning determinations, financial due diligence, or legal approvals.

Property-specific recommendations require independent verification before real-world implementation.

---

## 📄 Project Information

**Project:** Grow Up

**Event:** Meta Global AI Developer Hackathon 2026

**Development period:** October 26 – November 4, 2026

**Repository:** Danny-WanKenobi/growUP

**Status:** Active development

---

*Grow Up — Helping cities grow more with the space they already have.*

GUADE source-pilot engineering: every dispatch/review is bound to the versioned runtime architecture contract; source work includes the shared qualification/discovery regression gates. Coach review sees the full assignment, actual diff and engineer report. Retained controller labels from early passes do not close milestones that independent acceptance tests reject. See `guade/OPERATING.md` for inherited limits and live-pilot prerequisites.

### Source and inspection checkpoint verification (development)

The all-DCAD Dallas envelope now computes **0 Overpass / 0 Brave / up to 6 Census** provider requests, zero Meta calls and no outreach. Local file reads are recorded as `local_discovery_passes`; pass, creation and per-unit limits still apply. Missing or unavailable retained official evidence blocks inspection without a Brave fallback. Mixed improvement classes stay contradictory. Official admission passes any explicitly collected Census binding through the existing shared county-admission policy; absent evidence earns no county-cell credit. Exact run/event/property/digest bindings are pinned in the evaluation limits and cannot change on replay. A completed same-key inspection reuses its dossier, records and receipts.

Run `python -m pytest -q -p no:cacheprovider tests/` locally. Ubuntu must separately run targeted source/official-record/custody tests and the full PostgreSQL regression against **growup_mvp_gate** before closure. Two new PostgreSQL source-store tests in `tests/test_source_discovery.py` check durable cold-cache replay, single-winner claims and interruption accounting with fake HTTP; they guard the isolated database name and role and clean up only their unique workflow assignments. Fixture-only tests do not prove live provider compatibility or production readiness. The execution ledger preserves stopped out-of-scope dispatches; root-reviewed corrections do not retroactively mark those dispatches successful.


## Per-dossier inspection and conceptual engineering completion

The 144-dossier milestone and further Gabriel development are deferred. Raphael
never waits for a batch before promoting an individually authorized dossier.
The existing verification policy remains unchanged. No new discovery engine,
provider, migration, or GUADE work is introduced here.

### Reports, custody and operator release

`app/agents/raphael/inspection.py` builds a preliminary inspection report containing
verification reasons, physical assumptions, missing evidence and constraints.
Structural/electrical capacity, permitted agricultural use, water quality and
actual vacancy remain unknown without supporting authenticated evidence.
An operator's `verified` label on a dimension is downgraded to an assumption.

`app/dossier_reports.py` verifies received custody, current holder, latest version
and digests, then appends the report to the inherited dossier without changing
property facts. It creates an immutable successor and complete snapshot. Normal
successful new flows have Gabriel version 1, Raphael report version 2, Onesimus
report version 3, and receipts to Raphael, Onesimus and Michael. Authorization
binds version 2; engineering consumes its frozen physical inputs. Needs-evidence
or rejected dossiers return to Gabriel without engineering authorization.
Existing 90-day archival functionality is reused; automatic archival scheduling
is not installed or confirmed by this task.

Inspection-only discovery runs retain `stop_after_raphael`. Wondrue's new
consequential `release_engineering` intent accepts `args` containing `run_id` and
optional classified `site_inputs`. Use authenticated `POST /wondrue/command` with
`intent_override`, `target: "real"`, `use_live: false` and mode `ask` for a preview;
redeem its approval token as the same operator. Full mode requires operator
access; readers cannot release dossiers. No acquisition or inference occurs.
The retained approval binds principal and exact inputs, allowing idempotent replay
without altering original run limits. A historical authorized hold lacking a
report may receive explicit scenario assumptions. Already frozen inputs cannot
be silently changed; changed inputs require a new assessment, whose automatic
orchestration is not implemented.

### Physical inputs and conceptual demonstration

Onesimus uses passive non-circulating Kratky containers with root-zone air space.
Bench and three-tier racks are alternatives, never additive capacity. Inputs
contain `value`, `unit`, `classification` and optional `provenance`; classifications
are verified, estimated, assumed, experimental or unknown. Unknown is not zero.

For example, assumed usable area 100 m2, room length/width 10 m and clear height
4 m fit 15 assumed 2m x 1m racks and 2,250 sites across three tiers at 0.2m plant
spacing. Default aisle 1m, perimeter 0.5m, tier pitch 0.9m and ceiling gap 0.3m
are catalog assumptions, not safety standards. Exceeding area/height or fitting
no whole grid produces an incomplete layout. Area-only inputs produce estimates,
not validated rack layouts. `SITE_UNITS` and `CROP_UNITS` in
`app/agents/onesimus/engineering.py` list accepted units. Conversions include
ft2 to m2 and ft/cm/mm to m; nonfinite values and incompatible units refuse.

Reports retain formulas, units, assumptions and missing quantities. Annual water
includes initial fill every crop cycle, top-up and quantified cleaning. Nutrient
solution and concentrate volumes are separate. Physical HVAC energy, nutrient
concentrate and labor remain unknown without inputs. Michael computes finances
after receiving the enhanced dossier. Physical labor/HVAC quantities replace
legacy area-based allowances rather than double-count them; missing prices or
quantities cannot establish profitability.

`engineering_options` optionally supplies rainwater, lighting, tenant_consolidation
and food_forest inputs. Reports consider undershelf/top-tier lighting, DLI,
daylight/roof windows, reflective finishes, qualified worker access, rainwater,
municipal backup, groundwater and experimental condensate reuse. No loose Mylar
flooring, water availability, safety or energy saving is automatically approved.
Tenant scenarios preserve actual occupancy and flag consent/lease/access/cost
constraints. The owner-selected Kratky guide and Food Forest Abundance are retained
references alongside primary/extension sources; this is not model training or
expert certification. Food-forest outputs remain conceptual, without fabricated
yields. Outdoor deployment and construction-ready engineering remain deferred.

Repeatable offline checks (fixture data are not real-property verification):

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:GROWUP_PG_INTEGRATION = '0'
$env:META_SPEND_APPROVED = '0'
.\.venv\Scripts\python.exe -B -m pytest tests/test_funnel_acceptance.py tests/test_engineering_completion.py tests/test_engineering_reports.py tests/test_onesimus.py tests/test_michael.py tests/test_wondrue_ops.py -q -p no:cacheprovider
```

Owner Ubuntu PostgreSQL tests, full regression and deployed demonstration remain
pending. No provider spending, credentials or deployment configuration changed.

Local Windows validation for this task: 2,483 passed, 148 skipped, 17 warnings
(full offline run). PostgreSQL-gated tests were not executed locally.


### Earlier first-authorization planning notes (superseded by the source-first contract)

The owner has resumed discovery to obtain one genuine dossier for downstream
pipeline testing, independently of the deferred 144-dossier quota. The runner
accepts `--stop-on-authorized` with `--cells`, or JSON boolean
`"stop_on_authorized": true` inside an envelope. Default false preserves existing
runs. Use policy `timed` when quota completion must not terminate discovery.
The same configured request, duration, creation, per-unit and failure ceilings,
operator stop and Sabbath checks still apply. A limit or exhausted source pauses
or finishes honestly; it is not permission to expand geography or spending.

Only an `evaluated` coordinator result with decision exactly `authorized` and
valid operational property/evaluation UUIDs satisfies this stop. Qualification,
quota credit, needs-evidence, rejection and failed evaluations never substitute.
The runner persists the result and remaining backlog, reports `authorization_match`
(property_id and run_id), and ends before evaluating another candidate or making
another discovery pass. A resumed checkpoint retaining that result stops without
further acquisition. This historical stop marker is not a new permission grant;
held-dossier release revalidates current authorization and custody.

In this mode the standard coordinator is requested to hold at Raphael. Engineering
and finance can then be exercised using the separately reviewed release command.
No synthetic fallback, verification relaxation or automatic outreach is introduced.

Preview example (no acquisition or database writes):

```powershell
.\.venv\Scripts\python.exe -B -m app.discovery_runner preview --scope first-authorized-dallas --policy timed --cells config/dallas_opening_units.example.json --max-passes 12 --max-duration 3600 --max-created 30 --max-evaluations 5 --evaluation-attempt-limit 1 --stop-on-authorized
```

This is a control preview, not a launch instruction or approved source envelope.
**Launch blocker:** official/generic retained discovery admission intentionally
creates observed claims without candidate verification. Repeating that path alone
cannot authorize a dossier. The DCAD page collector also retains an explicit
account allowlist. A source-bound authenticated admission path and its exact
account/network authorization must be established before launching this campaign;
no discovery job has been launched by adding this control. Dynamic account
admission is not authorized by a source's filename or an AI-generated claim.

Local focused runner checks cover stopping mid-backlog, checkpoint resume,
qualification-versus-authorization separation, malformed/failed results and
strict boolean envelope parsing. PostgreSQL/deployed validation remains pending
for this new runner option. No migrations or new environment variables.


Owner approval received for a proposed DCAD admission extension: at most 30
account pages over one hour, no Brave/Meta/Census/outreach. This extension is
**not implemented or launched** by the stop-control change. Current public
robots-guidance retrieval failed: Windows TLS timeout and development VM
connection timeout. The existing bulk-adapter documentation also records a
restriction on account-page crawling. Access and permitted collection must be
confirmed before implementing/launching automatic per-account admission; the
approval does not silently override the publisher's restrictions. No account
pages were requested in these checks. Focused runner/coordinator regression:
130 passed, 6 PostgreSQL-gated skips. No deployment or schema changes.
