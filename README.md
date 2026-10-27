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