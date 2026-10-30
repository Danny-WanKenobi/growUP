# 🌱 Grow Up

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
| Opportunity & Outreach | **Uriel** | Prepares opportunity and outreach campaign packages; executes outreach only when implemented and explicitly authorized. |
| Conversion Tracker | **Priscilla** | Tracks delivery, engagement, responses, and lead conversion, distinguishing actual from simulated events. |

The agents share a common property record and coordinated workflow rather than operating as seven independent applications. Processing order is Gabriel → Raphael → Onesimus → Michael → Uriel → Priscilla, with Wondrue coordinating throughout (not a duplicate pipeline).

**MVP scope:** the coordinated MVP implements the full six-stage pipeline plus the Wondrue cockpit. Gabriel assembles evidence-backed dossiers; Raphael performs deterministic authorization over persisted dossier versions (preliminary simulation scoping only); Onesimus evaluates two passive-Kratky engineering configurations; Michael owns all financial arithmetic and feasibility verdicts; Uriel prepares genuine campaign packages (no sender exists); Priscilla tracks conversion events with simulated and actual evidence strictly separated. Wondrue enforces read-only / ask / full access modes server-side. Dossiers move between agents under shared operational custody (append-only versions, immutable snapshots, authenticated receipts); a resumable 36-cell pilot ledger tracks the 144-dossier target. No outreach execution exists.

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

**Project status:** Coordinated MVP implemented with two demonstration paths — real-property evaluation (fail-closed; a candidate may legitimately stop at `needs_evidence`) and an isolated synthetic demonstration that runs the full software workflow on fictional fixtures. Deferred: autonomous 144-dossier execution, exhaustive city searches, OAuth/admin systems, outreach, additional agents, advanced underwriting, and detailed permitting.

---

## 🔧 Installation and Development

Prerequisites: Python 3.12+ and PostgreSQL (for the real-property path; the synthetic demonstration needs no database).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell)
pip install -r requirements.txt
copy .env.example .env          # then fill in local values; .env is git-ignored
python -m app.migrate           # apply SQL migrations in filename order
uvicorn app.main:app --port 8001
```

Configuration lives in environment variables (see `.env.example`): `DATABASE_*` for PostgreSQL, `INTAKE_API_KEY` / `RESEARCH_API_KEY` for route authentication, `BRAVE_SEARCH_API_KEY` and `BRAVE_MONTHLY_CEILING` for research spend, `MVP_WORKFLOW_ENABLED=1` to switch on the real-property workflow entry points (off by default), `META_API_URL` (Baseten base or full chat-completions endpoint) / `META_API_KEY` / `META_MODEL` (`meta-models/muse-spark-open-1026`) / `META_SPEND_APPROVED` / `META_MAX_TOKENS` / `META_TIMEOUT_SECONDS` / `META_MAX_RESPONSE_BYTES` for Meta inference, plus `INFERENCE_TOKEN_ALLOWANCE` (default `0` = live disabled) / `INFERENCE_USD_PER_1M_INPUT` / `INFERENCE_USD_PER_1M_OUTPUT` / `INFERENCE_PRICING_MULTIPLIER` / `INFERENCE_PRICING_STATUS` / `INFERENCE_PRICING_SOURCE` / `INFERENCE_PRICING_DATE` (required provisional pricing basis; the retired `INFERENCE_USD_PER_1K_TOKENS` must stay unset) / `INFERENCE_DEMO_RESERVE` / `INFERENCE_LARGE_BATCH_TOKENS` / `INFERENCE_TOKENS_PER_IMAGE` / `INFERENCE_MAX_PROMPT_CHARS` / `INFERENCE_MAX_CONCURRENT` for the shared inference budget (live calls refused unless spending is approved, allowance granted, and pricing configured), `DEMO_MODE` (`yes` default / `no`; anything else rejected) for Michael-stage-and-later external behavior, `EMERGENCY_DIAGNOSTIC_DEMO` for the restricted post-pilot synthetic fallback, `CUSTODY_HMAC_KEY` for receipt authentication, `CUSTODY_AMENDERS` for the amendment authorizer allowlist (empty refuses all amendments), `WONDRUE_OPERATOR_KEY` / `WONDRUE_READER_KEY` for cockpit credentials (operator grants the full mode range, reader grants read-only), and `SABBATH_BLACKOUT` (default `1`) for the discovery runner's Friday-evening-to-Saturday-evening pause window.

Run the local suite with `python -m pytest -q tests` (PostgreSQL-gated tests skip without `GROWUP_PG_INTEGRATION=1`).

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
Confirmed deployed checkpoint `0ec9e18` (migrations 001–017 on
both databases): full regression reached **2,097 passed, zero
failures/skips** (17 deprecation warnings); operational health
checks passed for both databases, application health/readiness,
PostgreSQL availability, and the service; HTTPS `/wondrue`
routing now works. Earlier runs had surfaced (and corrected)
five custody-ordering failures, two test-contract mismatches,
two route-test ambient-database assumptions, the
throttle-profile review gaps, and the unrendered cockpit
template braces. The cockpit preference/status work in this tree
is newer than that checkpoint: immediate validated profile
persistence, a separate inference-setting indicator, plain-text
banner punctuation, masked Wondrue-access-key labeling, and
actionable authentication explanations. It needs a targeted
rerun, full regression, and health checks — all **pending owner
execution** — plus owner browser acceptance, which scripted
checks cannot establish. No Nginx change is part of this fix.

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
intents (`inspect_*`, `preview_simulation`, `explain_decision`,
`help`) never mutate records or issue metered calls; consequential
intents (`start_*`, `resume_discovery`, `stop*`, `run_simulation`,
`compare_scenarios`, `assist_explain`, `analyze_evidence`,
`explain_economics`, `draft_outreach`, `extract_document`) need
ask-with-token or full. Discovery bursts
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
updated) resolve them; the confirmed `0ec9e18` checkpoint passed
full regression (2,097 passed, zero failures/skips),
migrations 001–017 on both databases, and operational health
checks with the cockpit template fix deployed; HTTPS routing
works. The cockpit preference/status work in this tree (local
suite green: 1,964 passed, 133 skipped — 131 PG-gated, 2
POSIX-only — 17 warnings, mocked providers only; served script
syntax- and behavior-checked under Node with a DOM stub, not
browser-accepted) needs a fresh validation cycle (targeted
rerun, full regression, health checks, owner browser
acceptance, live smoke test) — **pending owner execution**.

Snapshots older than 90 days move to the archive table via
`archive_snapshots()`; retrieval checks the hot table first, then the
archive, and archived snapshots stay verifiable. Versions, receipts,
packages, and conversion events are permanent and are never moved or
deleted — database triggers enforce this.

### Pilot ledger

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
verdict.

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

### Bounded continuous discovery

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
it is never folded twice. Discovery limits (creation ceiling, pilot
quota) stop new discovery only: a pending backlog keeps draining in
bounded rounds under the same duration, stop-request, and
per-pass-evaluation limits, so a creation ceiling never strands queued
work. If the event store is unreadable, recovery aborts rather than
proceeding blind.

```bash
python -m app.discovery_runner run --scope pilot-dfw --policy pilot \
    --cells cells.json --max-passes 3 --max-duration 1800 --max-created 100
python -m app.discovery_runner status --scope pilot-dfw   # latest run + checkpoint
python -m app.discovery_runner stop --scope pilot-dfw     # pause at next pass boundary
```

Cells are operator-supplied `(county, category, bbox, source)` JSON;
placeholders are never silently expanded. The same bursts are
available through Wondrue (`start_discovery` / `resume_discovery` /
`stop_discovery` / `inspect_discovery`, request-safe caps). A proposed
two-hour timed preset (`TIMED_TWO_HOUR_PRESET`: 7200 s, 48 passes,
400 created) exists in code but is **not launched** — duration and
any spending approval are separate from implementation approval, and
launch additionally requires owner-verified cells. Coordinator
evaluations need explicit acquisition approval: the
`--acquire-approved` CLI flag, or a Wondrue burst with `acquire: true`
from the operator grant (ask mode previews the bounds first; see
below). Without it the runner creates property records and reports
`evaluations_precondition` instead of driving them.

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
Michael-stage and later external behavior only — upstream agents behave
identically in both modes (covered by acceptance tests), and the
synthetic upstream harness remains a separately selected diagnostic
fallback (restricted after the first evidenced pilot; see above).

### MVP limitations

No autonomous 144-dossier execution, exhaustive city searches, OAuth/admin
systems, outreach execution, additional agents, advanced underwriting, or
detailed permitting. Synthetic records never count toward the 144-dossier
target. `michael_ready` pilot counter and pre-canonical `mvp-1` receipt
rows are retained for history, not live use. Positive real-property
authorization still needs reviewed registry curation plus aligned
identity evidence. Known residual: pending-pass recovery depends on
reading the interrupted run's permanent decision events from the
event store; an unreadable store aborts the burst rather than
proceeding blind (see bounded discovery above). Validation gates
(each needs
its own owner approval): a targeted rerun, full regression, and
final database health checks for the cockpit preference/status
work in this tree (local suite: 1,964 passed, 133 skips — 131
PG-gated, 2 POSIX-only — 17 warnings, mocked providers;
checkpoint `0ec9e18` was fully green: 2,097 full with zero
failures/skips, health checks passed, HTTPS routing works);
owner browser acceptance of the cockpit (scripted checks cannot
establish it); a bounded live smoke test proving meaningful
Meta runtime inference (see Demonstration); and real
discovery runs (the two-hour timed preset is defined but not
launched).

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
`assignment_id` and planner-issued `query`. Gabriel acquires evidence
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
mode never issues live calls. Live Meta and Brave calls were not executed
during implementation (pending explicit spending authorization).
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