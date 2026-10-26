# Grow Up — Repository Engineering Rules

## Project and Mission

**Project:** Grow Up (`growUP`)
**Hackathon:** Meta Global AI Developer Hackathon, October 26–November 4, 2026
**Development:** Windows 11, VS Code, Muse Code, Python, Git, Github; local Ubuntu VM for testing

Grow Up is an AI-powered platform for discovering underused commercial real estate, designing hypothetical indoor farms, assessing financial feasibility, and generating qualified opportunities for property owners. Its goal is to turn underperforming space into potentially productive agricultural space. **Never assume a conversion is profitable or legally executable without supporting evidence.**

Deliver a functioning, credible, demonstrable hackathon MVP. Favor working end-to-end behavior over speculative features or production-scale infrastructure.

## Discovery Scope and Ranking

Treat these as **equal-priority primary property categories**:

1. Skyscrapers and high-rise buildings.
2. Downtown commercial properties, including office, retail, and mixed-use space.
3. Warehouses and suitable industrial buildings.

Start discovery in major Texas markets, expanding later as appropriate. Do not assign an automatic scoring advantage to any primary property category. Rank **individual properties** using comparable evidence-based factors such as vacancy, usable space, ownership/distress signals, building suitability, estimated conversion costs, agricultural output, financial potential, and data confidence. Make category weights configurable, initially equal.

Vacant lots and food forests are a future expansion, not a hackathon requirement.

## Hypothetical Tenant Consolidation

For preliminary building simulations, **assume that existing tenants can be relocated upward and vacant space consolidated downward**, allowing agricultural conversion to begin at the lowest feasible level and expand upward through contiguous space. Do not reject a candidate merely because its ground floor is occupied.

This is an **explicit, initially enabled simulation assumption**, not a claim that relocation has occurred or can be compelled. Record existing occupancy separately from the hypothetical reorganized layout. Flag tenant consent, lease restructuring, operational suitability, access, relocation cost, and other unresolved constraints. Where relevant, estimate their effects on feasibility. A simulated layout must never be presented as an approved or executable redevelopment plan.

## Agricultural Engineering

The indoor growing method is **passive, non-circulating Kratky hydroponics**. The Agricultural Engineering Agent may optimize space utilization with individual containers, large or shared reservoirs, stacked racks, modular installations, and other configurations **provided the core passive, non-circulating principles are preserved**. Do not silently substitute circulating hydroponic systems.

Evaluate crop suitability, planting density, realistic yields, root-zone air space, water and nutrient requirements, structural loads, lighting, electrical capacity, HVAC, dehumidification, access, labor, sanitation, and operating costs. Distinguish **verified**, **estimated**, **experimental**, and **unknown** inputs. Cite or retain provenance for consequential source data; never fabricate engineering facts.

## Seven-Agent System

Build seven coordinated agents, not seven disconnected applications:

1. **Property Discovery:** Find and prioritize candidate properties.
2. **Property Inspection:** Assess property facts, occupancy, basic zoning, and conversion constraints.
3. **Agricultural Engineering:** Design and compare Kratky configurations and production scenarios.
4. **Financial Feasibility:** Calculate deterministic costs, revenues, sensitivities, and financial metrics.
5. **Opportunity & Outreach:** Rank opportunities, produce evidence-based proposals, and initiate compliant outreach within configured authorization and safety boundaries.
6. **Conversion Tracker:** Record outreach delivery, engagement, responses, and inbound lead conversion.
7. **Dashboard Agent:** Serve as the sole conversational interface for operational commands, monitoring, and corrections.

Use one shared property identity and persistent record across the pipeline. Prefer modular Python components, structured inputs/outputs, event-driven transitions where useful, idempotent processing, explicit statuses, provenance, and concise audit trails. Do not duplicate business logic across agents. The visual dashboard may provide views and filters; operational instructions and corrections go through the Dashboard Agent.

## Dashboard Access and External Actions

The Dashboard Agent supports three user-selected modes:

- **Read-Only:** Inspect, search, and explain; no mutations or external actions.
- **Ask for Permission:** Request approval before consequential actions.
- **Full Access:** Perform permitted actions within explicit backend-enforced scopes and limits, without per-action prompts where already authorized.

Enforce permissions **server-side**. An AI instruction, hidden button, or client-side setting is not an authorization boundary. Log consequential actions and outcomes. External outreach must comply with applicable law, platform/provider rules, opt-out requirements, and rate limits. Validate recipients and content, prevent duplicate sends, and support stopping or disabling outreach. Never claim a message was delivered or a lead converted without evidence.

## Technical Direction

- **Backend:** Python and FastAPI.
- **Database:** PostgreSQL, with migrations when schema changes warrant them.
- **Models:** Meta AI models must perform meaningful runtime work in the MVP; Muse Code alone does not satisfy this requirement.
- **Model access:** Keep provider-specific inference behind a small adapter to avoid unnecessary lock-in.
- **Environment:** Use `.venv`; store secrets in environment variables or local `.env` excluded from Git; maintain a sanitized `.env.example`.
- **Version control:** Git; Github is the development source of truth. Prepare an accessible public submission repository without exposing secrets.
**Primary repository:** https://github.com/Danny-WanKenobi/growUP
**Repository visibility:** Public
- **Deployment:** Local development and Ubuntu VM testing first; do not deploy to unrelated existing production/VPS systems without explicit approval.

Financial arithmetic belongs in deterministic, inspectable Python code. AI may propose assumptions or explain results, but must not invent computed outputs. Store assumptions, units, calculation inputs, and uncertainty with the results.

## Git and GitHub Rules

GitHub is the authoritative source repository for Grow Up.

Repository: https://github.com/Danny-WanKenobi/growUP

- Use Git exclusively for version control.
- Use `origin` for the GitHub remote.
- Keep the repository suitable for public inspection.
- Never commit API keys, credentials, `.env`, virtual environments, or authentication files.
- Muse may inspect Git status, history, and diffs.
- Muse may create and modify files within approved development tasks.
- Do not commit, push, force-push, merge, rebase, or delete branches without explicit authorization.
- Do not introduce Mercurial, Sapling, or Bitbucket workflows.
- Keep hackathon-developed work clearly identifiable.

## Engineering Discipline

Favor correctness, security, clarity, maintainability, minimal duplication, and explicit validation. Keep orchestration thin and business logic in clearly owned modules. Avoid duplicate pipelines, speculative abstractions, dead code, unnecessary dependencies, and broad unrelated refactors. Prefer existing dependencies or the Python standard library when practical. Keep interfaces and UI professional and easy to demonstrate.

Treat external data as untrusted. Validate inputs, escape output where appropriate, and never log or commit API keys, tokens, passwords, or private credentials. Use least-privilege access and safe defaults for database and network operations.

## Muse Code Development Authority

For an **approved implementation task**, Muse Code may independently inspect repository files and Git status/history; create and edit in-scope files; run local commands; install justified local development dependencies; perform focused checks; and fix ordinary issues within that task. **Do not repeatedly stop to request permission for routine in-scope work.**

Seek approval before major architectural departures, new consequential paid services or spending, destructive data operations, modifying secrets or security policy, production deployment, or external actions beyond previously authorized boundaries. When scope is uncertain, describe the decision briefly and ask.

**Git boundary:** Inspect freely, but do not commit, push, merge, tag, rebase shared branches, force-reset, run `git clean`, or publish a repository unless explicitly instructed. Leave changes in the working tree for owner review.

## Lean Hackathon Validation

**Do not build a comprehensive regression suite or chase coverage percentages.** Avoid tests for trivial functions, elaborate fixtures, redundant test scaffolding, and unnecessary testing dependencies.

Use the smallest checks appropriate to the change:

- Python syntax/compilation checks for modified modules.
- Targeted functional checks for new or changed behavior.
- A few focused automated tests for high-risk deterministic financial calculations, permission boundaries, and critical pipeline behavior.
- One repeatable end-to-end demo scenario covering the seven-agent workflow.
- `git diff --check` and a brief final diff review when relevant.

Tests must use disposable data and must not send real outreach or incur unapproved inference costs. Report what was checked, what remains unverified, and any material risk. **Prioritize a reliable demonstration over production-level test infrastructure.**

## Hackathon Delivery and Deferred Work

Optimize for the hackathon judging criteria: impact, meaningful Meta AI usage, technical depth, and a clear three-minute demonstration. Keep track of work developed during the hackathon and preserve reproducible setup instructions. Protect all credentials and private data before publication.

Defer full real-estate deal closing, advanced financing and ownership structures, tokenization, detailed regulatory/permit automation, food-forest deployment, and purpose-built agricultural skyscrapers until explicitly requested.

## Completion Report

At the end of each approved task, briefly state: **what changed, files affected, checks performed, important assumptions, unresolved issues, and the next sensible step**. Do not claim success without verifying the behavior relevant to the task.
