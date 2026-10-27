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
| Financial Feasibility | **Priscilla** | Calculates projected costs, revenues, and financial viability. |
| Opportunity & Outreach | **Michael** | Develops opportunity assessments and prepares stakeholder outreach. |
| Conversion Tracker | **Uriel** | Tracks opportunities, engagement, and conversion progress. |
| Dashboard | **Wondrue** | Provides the central conversational interface for coordinating and monitoring the system. |

The agents share a common property record and coordinated workflow rather than operating as seven independent applications.

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
| Artificial intelligence | Meta AI models |
| AI-assisted development | Meta Muse Code |
| Version control | Git |
| Source repository | GitHub |
| Development environment | Windows 11 and Ubuntu Linux |

The architecture emphasizes modularity, deterministic financial calculations, shared data, and responsible AI integration.

AI models assist with language understanding, analysis, explanations, and opportunity development. Financial arithmetic remains in inspectable Python code.

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

**Project status:** Early development — October 2026.

---

## 🔧 Installation and Development

Installation instructions, environment configuration, and local execution commands will be documented as the application foundation is implemented.

The project will use a Python virtual environment (`.venv`) and environment-based configuration for external services.

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
       "category":"downtown_commercial",
       "building_area_sqft_estimated":50000,
       "provenance":"Dallas Central Appraisal District record, account 00001234, retrieved 2026-10-26"}'
```

Record fields: `name` (required), `street_address`, `city`, `state`,
`zip_code`, `category` (required: `high_rise`, `downtown_commercial`,
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

---

## 🔍 Gabriel — Property Discovery Agent (MVP-003)

Gabriel researches **real** commercial properties and records them as
preliminary operational records through the existing MVP-002 validation gate.
He discovers possible opportunities; he never verifies claims. Verification
belongs to Raphael (future milestone).

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

High-rise, downtown-commercial, and warehouse/industrial candidates are
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

`POST /discovery/intake` requires the `X-Intake-Key` request header matching
the `INTAKE_API_KEY` environment variable (compared in constant time). A
missing or empty server key fails closed with `503`; a missing or wrong
header returns `401`. Keys never appear in logs or error responses. Set a
long random value via your environment (see `.env.example`); never commit a
real credential. Remaining limitations: one shared secret with no rotation,
per-client scoping, or audit trail, and all other endpoints remain
unauthenticated. Submitted URLs are stored, never fetched, so intake cannot
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
assignments for Dallas, Texas. **Listings first. Value driven. Distress
focused.** It produces assignments only: no searches executed, no scraping,
no AI calls, no database writes, no network requests.

### Architecture

`app/agents/gabriel/planner.py` exposes a narrow Python interface:

- `plan_initial_searches(config)` — balanced city-wide assignment set.
- `plan_followups(parent, feedback, config)` — bounded single-step
  adaptation from structured research feedback.

`GET /discovery/search-plan` previews the initial plan (read-only;
`total`, `per_category`, and `downtown` query parameters). It performs
no live research and changes no data.

Each assignment carries a stable content-derived id, query, market,
category, strategy, priority, research intent, planner version
(`004B-1.0`), downtown flag, depth, and parent id. Same inputs always
yield the same assignments in the same order.

### Supported search strategies

`for_sale`, `high_value`, `distressed`, `foreclosure_reo`,
`receivership_bankruptcy`, `auction_liquidation`,
`vacant_underutilized`, `price_reduced`, `long_listing`. Every query
seeks actual sale opportunities across the three primary categories in
balanced round-robin allocation (4 each by default, 12 total). A query's
wording is never evidence that a property is distressed; intents say so
explicitly. Downtown Dallas appears only as a bounded secondary
refinement (every fourth slot); city-wide Dallas dominates.

### Example research assignments

```json
[
  {"query": "high-rise office building for sale in Dallas, TX",
   "category": "high_rise", "strategy": "for_sale", "priority": 1},
  {"query": "price-reduced commercial office building for sale in Dallas, TX",
   "category": "downtown_commercial", "strategy": "price_reduced"},
  {"query": "distressed industrial warehouse property for sale in Downtown Dallas, TX",
   "category": "warehouse_industrial", "strategy": "distressed"}
]
```

### Configuration and budget controls

`PlannerConfig`: `total_max_queries` (1–54, default 12),
`per_category_max` (1–18, default 4), `followup_max` (0–50, default 6),
`max_depth` (0–5, default 2), `downtown_refinement` (default true).
Invalid budgets are rejected. Follow-ups derive deterministically from
feedback outcomes (`no_results`, `duplicates_heavy`, `promising`,
`provider_failure`, `missing_pricing`, `missing_distress_evidence`):
provider failures generate zero queries (no automatic retry), depth and
follow-up caps bound growth, and known queries are never repeated.

### Limitations

- No property values are invented and no minimum price is imposed; the
  planner ranks nothing because it has seen no listings.
- No provider adapter exists yet; assignments name no endpoints or
  credentials and assume no website HTML access.
- Geographic scope is Dallas only, with no grids, radius expansion, or
  boundary processing.

### Remaining MVP-004B phases

Later phases will cover provider execution adapters, listing-result
ingestion through the intake contract, and property ranking in
MVP-004B-4.

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
3. The service selects the first assignment per primary category in
   priority order (max 3), reserves budget, executes in order, and
   returns normalized results with per-assignment errors.

Only planner-issued assignments (matching planner version) are accepted;
arbitrary caller queries are rejected.

### Request limits and monthly budget

- Max 3 Brave requests per run; no recursion.
- Monthly ceiling from `BRAVE_MONTHLY_CEILING` (default 100), tracked per
  UTC calendar month in the `research_budget` table.
- Exactly one request is reserved atomically immediately before each Brave
  call; attempts count even on timeout or provider error. Skipped
  assignments consume nothing, and the run stops immediately with a
  `budget_exhausted` error entry when the month is spent.
- Rate limits (429) and auth failures (401/403) abort the rest of the run.

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

---

## 🎥 Demonstration

A demonstration video and usage walkthrough will be added as the hackathon MVP reaches completion.

---

## 🔭 Future Vision

Beyond the initial hackathon, Grow Up may expand to support:

- Additional geographic markets
- More sophisticated property discovery and analysis
- Real-world conversion project management
- Advanced financing and redevelopment opportunities
- Vacant-lot agricultural projects and food forests
- Purpose-built agricultural buildings

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