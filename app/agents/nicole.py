"""Deterministic regulatory evidence intake, not a legal clearance engine."""
from copy import deepcopy
from datetime import date
from urllib.parse import urlsplit
from uuid import UUID

TOPICS = ("zoning_use", "building_change_of_use", "fire_life_safety", "electrical",
          "water_wastewater", "food_safety", "business_licensing")
STATES = ("required", "not_required", "unknown")
DISPOSITIONS = ("pending", "claimed_approved", "conditional", "denied", "not_applicable")


def _url(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("https", "http") and bool(parsed.hostname) and not parsed.username and not parsed.password and (parsed.port is None or 1 <= parsed.port <= 65535)
    except (ValueError, TypeError, AttributeError):
        return False


def assess(property_id, supplied):
    """All source assertions stay provided_unverified, including approval claims.

    Topics are screening prompts, not a statement that a jurisdiction requires
    these permits. No source fetching, submissions, signing or permission grants.
    """
    missing, issues, records = [], [], []
    def entry(name):
        value = supplied.get(name)
        return value if isinstance(value, dict) else {}
    jurisdiction = entry("jurisdiction").get("value")
    use = entry("proposed_use").get("value")
    rows = entry("official_requirements").get("value")
    def bound(value):
        try:
            return isinstance(value, dict) and UUID(str(value.get("property_id"))) == UUID(str(property_id))
        except (ValueError, TypeError, AttributeError):
            return False
    jid = None
    jurisdiction_refs = entry("jurisdiction").get("evidence_refs")
    if not isinstance(jurisdiction_refs, list) or not jurisdiction_refs or len(jurisdiction_refs) > 100 or any(not _url(ref) for ref in jurisdiction_refs):
        missing.append("jurisdiction:source_references")
    if (not bound(jurisdiction) or any(not isinstance(jurisdiction.get(k), str) or not jurisdiction[k].strip()
            for k in ("jurisdiction_id", "authority_name", "city", "state"))):
        missing.append("jurisdiction:property_bound_authority")
    else:
        jid = jurisdiction["jurisdiction_id"]
    if (not bound(use) or use.get("method") != "passive_kratky" or use.get("scope") != "indoor_farming"):
        missing.append("proposed_use:property_bound_passive_kratky_scope")
    refs = entry("official_requirements").get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = []
    if not isinstance(rows, list) or len(rows) > 100:
        missing.append("official_requirements:bounded_structured_records")
        rows = []
    identities = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            missing.append(f"requirement:{index}:structured_record")
            continue
        topic = row.get("topic")
        try:
            date.fromisoformat(row.get("record_date", ""))
            dated = True
        except (ValueError, TypeError):
            dated = False
        if (not bound(row) or not jid or row.get("jurisdiction_id") != jid or topic not in TOPICS
                or row.get("state") not in STATES or row.get("disposition") not in DISPOSITIONS
                or not isinstance(row.get("record_id"), str) or not row["record_id"].strip()
                or not isinstance(row.get("requirement"), str) or not row["requirement"].strip()
                or not _url(row.get("source_url")) or row["source_url"] not in refs or not dated):
            missing.append(f"requirement:{index}:identity_provenance_or_date")
            continue
        item = deepcopy(row)
        item["evidence_status"] = "provided_unverified"
        item["approval_authenticated"] = False
        identity = (row["source_url"], row["record_id"])
        if identity in identities:
            if identities[identity] != row:
                missing.append(f"requirement:{topic}:conflicting_record_identity")
            continue
        identities[identity] = deepcopy(row)
        records.append(item)
    checklist = []
    for topic in TOPICS:
        items = [r for r in records if r["topic"] == topic]
        states = {r["state"] for r in items}
        conflict = len(states - {"unknown"}) > 1
        unresolved = not items or "unknown" in states or conflict
        denied = any(r["disposition"] == "denied" for r in items)
        if unresolved or denied:
            missing.append("regulatory:"+topic+(":contradiction" if conflict else ":denied" if denied else ":needs_evidence"))
        for row in items:
            if row["state"] == "required" and row["disposition"] == "not_applicable":
                missing.append("regulatory:"+topic+":inconsistent_disposition")
            if row["state"] == "required" or row["disposition"] in ("claimed_approved", "conditional", "denied"):
                issues.append({"requirement":"regulatory:"+topic+":"+row["record_id"],
                    "reason":"Resolve the retained regulatory condition; supplied approval is not authenticated.",
                    "status":"pending", "authority":"human_owner", "grants_permission":False,
                    "evidence_refs":[row["source_url"]]})
        checklist.append({"topic":topic, "screening_only":True,
            "status":"conflicting" if conflict else "needs_evidence" if unresolved else "denied" if denied else "documented_unverified",
            "record_ids":[r["record_id"] for r in items]})
    missing = list(dict.fromkeys(missing))
    return {"version":"nicole-regulatory-1", "jurisdiction":deepcopy(jurisdiction),
        "proposed_use":deepcopy(use), "records":records, "checklist":checklist,
        "missing":missing, "owner_review_requests":issues,
        "ready_for_preliminary_underwriting":not missing,
        "regulatory_clearance":False, "permit_approval_authenticated":False,
        "external_actions":0, "provider_calls":0,
        "limitations":["Screening categories are not jurisdiction-specific legal requirements.",
            "Record dates are retained, not proof of current law, source authenticity or permit validity.",
            "No compliance, occupancy, structural/electrical capacity or permitted agricultural-use assertion."]}
