"""Version-one downstream roles and executable intake contracts.

These are deterministic preparation capabilities, not trained specialist models
or external-action permissions. The owner is the only human user. Manny is a
software role and cannot grant itself the owner's legal or operating authority.
"""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    role: str
    required_reports: tuple[str, ...]
    required_inputs: tuple[str, ...]
    deliverable: str
    deferred: tuple[str, ...]
    existing_runtime: str = ""


SPECS = (
    AgentSpec("uriel", "Uriel", "Opportunity Development & Outreach",
        ("raphael", "onesimus", "michael"), ("owner_contact", "channel_permission"),
        "Outbound-only opportunity/campaign preparation; never owns inbound conversations; reuse existing packages.",
        ("Autonomous authenticated delivery, suppression/rate-limit enforcement and delivery receipts",
         "Visual campaign design and contact-channel research"), "app.uriel"),
    AgentSpec("priscilla", "Priscilla", "Lead Conversion Tracking", ("uriel",),
        (),
        "Lead conversion tracking only; no human conversation ownership, drafting, transport or delivery.",
        ("Human communications are outside Priscilla's role",
         "Richer property timeline UI and authenticated provider reconciliation"), "app.priscilla"),
    AgentSpec("nicole", "Nicole", "Regulatory, Permitting, and Licensing",
        ("raphael", "onesimus"), ("jurisdiction", "proposed_use", "official_requirements"),
        "Property-bound regulatory evidence matrix, contradiction screening and owner-review conditions; no clearance assertion.",
        ("Current code/permit research and authenticated jurisdiction adapters", "Applications and permit submissions"), "app.agents.nicole"),
    AgentSpec("theresia_keys", "Theresia Keys", "Underwriter and Final Contract Prep",
        ("michael", "nicole"), ("ownership_and_liens", "financing_terms", "contract_terms"),
        "Evidence-linked funding/debt planning and unsigned conditional preparation brief; no financing or execution approval.",
        ("Authenticated title/lien checks, lender rules and financing reconciliation", "Final legal instruments and signing"), "app.agents.theresia"),
    AgentSpec("simeon", "Simeon", "Deal Closer", ("theresia_keys",),
        ("counterparty_consent", "approved_terms", "signing_authority"),
        "Primary inbound-only conversation owner plus terms-bound closing preparation and exception routes; no delivery/signing authority.",
        ("Negotiation channels, closing integrations and authenticated signatures", "Power-of-attorney verification"), "app.agents.simeon"),
    AgentSpec("solomon", "Solomon", "Construction Coordinator and Foreman", ("onesimus", "nicole", "simeon"),
        ("approved_design", "permits", "contractor_and_safety_plan", "construction_budget"),
        "Evidence-linked preconstruction planning, two-way communication drafts and isolated vendor simulation; no build authority.",
        ("Live contractor/robot procurement and authenticated two-way channels", "Construction orders, physical supervision and maintenance automation"), "app.agents.solomon"),
    AgentSpec("elmer", "Elmer", "Accountant and Tax Expert", ("michael", "solomon"),
        ("accounting_period", "transactions", "entity_and_tax_jurisdiction"),
        "Evidence-linked project-cost intake separating estimates, pending changes, reported commitments and expenses; no payments or tax liability.",
        ("Double-entry ledger, bank feeds and reconciliations", "Current tax-rule adapters, filings and payments"), "app.agents.elmer"),
    AgentSpec("yoseph", "Yoseph", "Insurance and Liability Expert", ("raphael", "onesimus"),
        ("risk_inventory", "existing_policies", "coverage_requirements"),
        "Coverage/risk evidence checklist; policy existence is not proof that a loss is covered.",
        ("Policy-wording analysis, carrier quotes and coverage validation", "Binding coverage and claims submission")),
    AgentSpec("deborah", "Deborah", "Legal Expert", (),
        ("jurisdiction", "legal_question", "governing_documents"),
        "Legal issue register and document-review intake; no assertion of legal compliance or enforceability.",
        ("Current-law research and legal reasoning with authoritative citations", "Legal opinions, filings and representation")),
    AgentSpec("manny", "Manny", "Owner Agent and Asset Manager", ("michael","elmer"),
        ("asset_ownership", "operating_mandate", "asset_obligations"),
        "Asset/obligation intake and owner-exception brief; represents software coordination, not the human owner.",
        ("Verified asset register, performance monitoring and maintenance lifecycle", "Delegated transaction authority and asset transfers")),
)
ADVISER_IDS = ('yoseph','deborah')
PIPELINE_IDS = ('gabriel','raphael','onesimus','michael','uriel','priscilla','nicole','theresia_keys','simeon','solomon','elmer','manny')
AGENT_SPECS = {spec.id: spec for spec in SPECS}
PIPELINE_SPECS = {key:spec for key,spec in AGENT_SPECS.items() if key not in ADVISER_IDS}
NEW_AGENT_ROLES = {spec.id: f"{spec.name} {spec.role}" for spec in SPECS[2:]}

HANDOFF_CONTRACT = (
    "Consume the existing property UUID and canonical dossier; never create a parallel dossier or database.",
    "Load the latest workflow dossier and custody link; verify chain, digest and current holder server-side.",
    "Append only the agent's structured report to a full inherited dossier copy; preserve all prior facts/reports.",
    "Use app.dossier_reports.append_agent_report_in_transaction and the existing immutable snapshot store.",
    "Use app.custody_db.issue_receipt_in_transaction for forward/return/review; never manufacture a receipt.",
    "A forward transfer names the latest version; evidence gaps may return upstream or request human_review.",
    "Every pipeline agent may use app.agent_consultations for two-way Yoseph/Deborah review without custody transfer or permission override.",
    "Preparation, handoff, legal permission and external execution are distinct; no report grants execution authority.",
)


def describe_agents():
    return {
        "version": "downstream-agents-1", "single_owner": True,
        "normal_operation": "Autonomous internal preparation within configured backend permissions; owner queue for unresolved exceptions and sensitive actions.",
        "workers_started": False, "external_execution_enabled": False,
        "handoff_contract": list(HANDOFF_CONTRACT),
        "pipeline": list(PIPELINE_IDS), "shared_advisers": list(ADVISER_IDS),
        "agents": [dict(asdict(spec), participation="shared_adviser" if spec.id in ADVISER_IDS else "pipeline", version=1, capability="preliminary_intake_and_report",
                        live_property_handoff_proven=False, entrypoint="app.agent_consultations.exchange_audited" if spec.id in ADVISER_IDS else "app.agents.stages.prepare_audited",
                        input_source="current canonical stored dossier only") for spec in SPECS],
    }
