"""Permanent, bounded synthetic proof of the shared Gabriel-to-Manny custody path.

Only the dedicated PostgreSQL demo realm is accepted. No property discovery,
models, outreach, contract signing, professional approvals or operational quotas.
"""
from copy import deepcopy
import json
import os
import re
from uuid import uuid5, NAMESPACE_URL

from app.agents.registry import AGENT_SPECS, PIPELINE_IDS
from app.demo import DEMO_LABEL, _demo_financial_inputs
from app.handoff_demo_domain import DATABASE, ROLE, DOMAIN, assert_database, scope
from app.workflow import digest_payload
from app.workflow_db import (WorkflowError, create_run_in_transaction, get_run_by_key_in_transaction,
    get_run_in_transaction, get_record_in_transaction, put_record_in_transaction)
from app.custody import dossier_id_for_property
from app.custody_db import (create_version_in_transaction, snapshot_in_transaction,
    issue_receipt_in_transaction, current_custody_in_transaction, verify_chain_in_transaction,
    latest_version_in_transaction)

STAGES = PIPELINE_IDS
FIXTURE_VERSION = "handoff-fixtures-14"
MAX_STEPS = 2*len(STAGES)


def marked(value):
    return {**value, "synthetic":True, "domain":DOMAIN, "demo_label":DEMO_LABEL}


def sealed(value):
    value = deepcopy(value)
    value.pop("digest",None)
    value["digest"] = digest_payload(value)
    return value


def configuration():
    from app.config import DatabaseConfig
    if os.getenv("HANDOFF_DEMO_ENABLED")!="1":
        raise WorkflowError("isolated handoff demo is disabled")
    cfg = DatabaseConfig(host=os.getenv("HANDOFF_DEMO_DATABASE_HOST",""),
        port=int(os.getenv("HANDOFF_DEMO_DATABASE_PORT","5432")),
        name=os.getenv("HANDOFF_DEMO_DATABASE_NAME",""),
        user=os.getenv("HANDOFF_DEMO_DATABASE_USER",""),
        password=os.getenv("HANDOFF_DEMO_DATABASE_PASSWORD",""))
    if cfg.name!=DATABASE or cfg.user!=ROLE or not cfg.is_configured:
        raise WorkflowError("dedicated HANDOFF_DEMO_DATABASE_* configuration is required; no operational credential fallback")
    key = os.getenv("HANDOFF_DEMO_HMAC_KEY","")
    if len(key)<32 or key==os.getenv("CUSTODY_HMAC_KEY"):
        raise WorkflowError("a distinct HANDOFF_DEMO_HMAC_KEY of at least 32 characters is required")
    return cfg, key


def _key(run_key):
    if not re.fullmatch(r"demo-handoff-[A-Za-z0-9_-]{1,96}",str(run_key)):
        raise WorkflowError("run_key must be an explicit demo-handoff- identifier")
    return str(run_key)


def _get(conn, run_key, lock=False):
    assert_database(conn)
    run = get_run_by_key_in_transaction(conn,_key(run_key))
    if run is None:
        raise WorkflowError("unknown synthetic handoff run")
    if lock:
        run = get_run_in_transaction(conn,run.run_id,for_update=True)
    if (run.limits or {}).get("fixture_version") not in ("handoff-fixtures-1", "handoff-fixtures-2", "handoff-fixtures-3", "handoff-fixtures-4", "handoff-fixtures-5", "handoff-fixtures-6", "handoff-fixtures-7", "handoff-fixtures-8", "handoff-fixtures-9", "handoff-fixtures-10", "handoff-fixtures-11", "handoff-fixtures-12", "handoff-fixtures-13", FIXTURE_VERSION) or not run.limits.get("synthetic"):
        raise WorkflowError("run is not a supported synthetic handoff fixture")
    return run


def _state(conn, run):
    record = get_record_in_transaction(conn,run.run_id,"research_artifact","handoff-state")
    if record is None or record.digest!=digest_payload(record.payload):
        raise WorkflowError("synthetic checkpoint failed integrity verification")
    return record


def _write_state(conn, run, state, version):
    state = marked(state)
    return put_record_in_transaction(conn,run.run_id,kind="research_artifact",record_key="handoff-state",
                                     version=version,payload=state,digest=digest_payload(state))


def _fixture_verification(category="warehouse_industrial", levels=40):
    from app.agents.gabriel.source_registry import SourceRegistry, RegistryEntry, SourceClass
    from app.agents.gabriel.verification import verify_category, VerificationRequest, VoiceInput, IdentityAttestation
    # Private FICTIONAL registry: never inserted into the production registry.
    registry = SourceRegistry(entries={"records.fiction.example":RegistryEntry(
        hostname="records.fiction.example",source_class=SourceClass.AUTHORITATIVE,
        publisher="Fictional demo authority",record_types=["fictional_account"])})
    if category not in ("high_rise","commercial_property","warehouse_industrial"): return None
    facts={"levels":str(levels),"building":"skyscraper"} if category=="high_rise" else {"use":"office" if category=="commercial_property" else "warehouse","use_status":"current"}
    return verify_category(VerificationRequest(claim=category,registry=registry,
        identity=IdentityAttestation(group_key="main",aligned=True,note="SYNTHETIC identity fixture only"),
        voices=[VoiceInput(url="https://records.fiction.example/fiction-warehouse",record_type="fictional_account",
                           provenance="adapter_collected",defining_facts=facts)])).model_dump(mode="json")


def _fixture(run_key, fail_at):
    prop = uuid5(NAMESPACE_URL,"growup:synthetic-handoff:"+run_key)
    inputs = {}
    for name,spec in AGENT_SPECS.items():
        inputs[name] = {field:{"value":"FICTIONAL DEMO INPUT: "+field,
            "evidence_refs":["https://records.fiction.example/"+name+"/"+field]} for field in spec.required_inputs}
    inputs['uriel']['owner_contact']['value'] = {'name':'Fictional Owner','email':'owner@fiction.example',
        'property_id':str(prop),'relationship':'owner'}
    from app.agents.nicole import TOPICS
    inputs['nicole']['jurisdiction']['value'] = {'property_id':str(prop),
        'jurisdiction_id':'fiction-city', 'authority_name':'FICTIONAL demo authority', 'city':'Fiction City', 'state':'XX'}
    inputs['nicole']['proposed_use']['value'] = {'property_id':str(prop), 'scope':'indoor_farming', 'method':'passive_kratky'}
    inputs['nicole']['official_requirements']['value'] = [
        {'property_id':str(prop), 'jurisdiction_id':'fiction-city', 'topic':topic,
         'record_id':'fiction-'+topic, 'requirement':'FICTIONAL screening record, not real law',
         'state':'not_required', 'disposition':'not_applicable', 'record_date':'2026-11-03',
         'source_url':'https://records.fiction.example/nicole/official_requirements'} for topic in TOPICS]
    inputs['theresia_keys']['ownership_and_liens']['value'] = {'property_id':str(prop),
        'owner_name':'FICTIONAL demo owner','lien_status':'none_reported','record_date':'2026-11-03'}
    inputs['theresia_keys']['financing_terms']['value'] = {'property_id':str(prop),'currency':'USD',
        'additional_project_costs_usd':'1000','contingency_usd':'1000','proposed_funding_usd':'100000000',
        'annual_debt_service_usd':'0','minimum_coverage_ratio':'1.25'}
    inputs['theresia_keys']['contract_terms']['value'] = {'property_id':str(prop),
        'parties':[{'name':'FICTIONAL owner','role':'owner'},{'name':'FICTIONAL operator','role':'operator'}],
        'scope':'FICTIONAL passive Kratky preliminary concept only','term_months':12,
        'conditions':['FICTIONAL demonstration; no actual authority, contract or financing.']}
    terms_digest = digest_payload(inputs['theresia_keys']['contract_terms']['value'])
    inputs['simeon']['counterparty_consent']['value'] = {'property_id':str(prop),'terms_digest':terms_digest,
        'parties':[{**party,'status':'consented','record_date':'2026-11-03',
            'source_url':'https://records.fiction.example/simeon/counterparty_consent'}
            for party in inputs['theresia_keys']['contract_terms']['value']['parties']]}
    inputs['simeon']['approved_terms']['value'] = {'property_id':str(prop),'terms_digest':terms_digest,
        'status':'reviewed','record_date':'2026-11-03'}
    inputs['simeon']['signing_authority']['value'] = {'property_id':str(prop),
        'principal':'FICTIONAL owner','scope':'review_only','record_date':'2026-11-03'}
    inputs['solomon']['approved_design']['value']={'property_id':str(prop),'scope':'preconstruction_planning',
        'engineering_reference':'retained_onesimus_report'}
    inputs['solomon']['permits']['value']={'property_id':str(prop),'status':'pending'}
    inputs['solomon']['contractor_and_safety_plan']['value']={'property_id':str(prop),'coordinator':'FICTIONAL Solomon coordinator',
        'site_access_plan':'FICTIONAL supervised planning only; no site entry','review_status':'pending'}
    inputs['solomon']['construction_budget']['value']={'property_id':str(prop),'currency':'USD','limit_usd':'100000',
        'line_items':[{'phase':'survey','estimated_cost_usd':'1000'},{'phase':'design','estimated_cost_usd':'2000'}]}
    inputs['elmer']['accounting_period']['value']={'property_id':str(prop),'start':'2026-11-01','end':'2026-11-30'}
    inputs['elmer']['entity_and_tax_jurisdiction']['value']={'property_id':str(prop),
        'entity_name':'FICTIONAL demo entity','jurisdiction':'FICTIONAL Texas jurisdiction; not a tax determination'}
    inputs['elmer']['transactions']['value']={'property_id':str(prop),'coverage':'none_reported','entries':[]}
    inputs['manny']['asset_ownership']['value']={'property_id':str(prop),'status':'claimed','note':'FICTIONAL claim, not verified ownership'}
    inputs['manny']['operating_mandate']['value']={'property_id':str(prop),'scope':'review_only'}
    inputs['manny']['asset_obligations']['value']={'property_id':str(prop),'coverage':'none_reported','entries':[]}
    def supplied(value): return {'value':{'property_id':str(prop),**value},'evidence_refs':['https://records.fiction.example/advisory']}
    inputs['yoseph']={
        'risk_inventory':supplied({'as_of':'2026-11-03','entries':[{'id':'water-escape','description':'FICTIONAL water escape concern','severity':4,'likelihood':3,'evidence_refs':['https://records.fiction.example/risk']}]}),
        'existing_policies':supplied({'entries':[{'id':'fiction-policy','coverage':'liability','effective_on':'2026-01-01','expires_on':'2026-12-31','currency':'USD','limit_usd':'1000000.00','deductible_usd':'5000.00','exclusions':[],'evidence_refs':['https://records.fiction.example/policy']}]}),
        'coverage_requirements':supplied({'entries':[{'coverage':'liability','minimum_limit_usd':'1000000.00','evidence_refs':['https://records.fiction.example/requirement']}]})}
    inputs['deborah']={
        'jurisdiction':supplied({'name':'FICTIONAL jurisdiction only'}),
        'legal_question':supplied({'text':'FICTIONAL preliminary scope review, not a legal opinion'}),
        'governing_documents':supplied({'entries':[{'id':'fiction-concept','version':'1','status':'signed_claimed','issues':[],'evidence_refs':['https://records.fiction.example/document']}]})}
    if fail_at in AGENT_SPECS and AGENT_SPECS[fail_at].required_inputs:
        inputs[fail_at].pop(AGENT_SPECS[fail_at].required_inputs[0])
    return sealed(marked({"version":1,"property_id":str(prop),"artifact_refs":[],
        "fixture_version":FIXTURE_VERSION,
        "candidate_verification":None if fail_at=="raphael" else _fixture_verification(),
        "dossier":marked({"name":"FICTIONAL Handoff Warehouse — not a real property",
            "tracking_policy":{"require_owner_review":fail_at=="priscilla"},
            "street_address":"1 Fiction Plaza (invented)", "city":"Fiction City", "state":"XX",
            "sources":[] if fail_at=="gabriel" else ["https://records.fiction.example/fiction-warehouse"],
            "category_attribution":{"status":"observed","observed":"warehouse_industrial"},
            "identity_disputes":{},"conflicts":[],"reports":{},"specialist_inputs":inputs})}))


def start_in_transaction(conn, *, run_key, fail_at=None, scenario_id=None):
    assert_database(conn)
    run_key = _key(run_key)
    from app.demo_catalogue import get_case,apply
    profile=get_case(scenario_id)
    if profile:
        if fail_at not in (None,profile["expected_failure"]): raise WorkflowError("catalogue failure cannot be overridden")
        fail_at=profile["expected_failure"]
    if fail_at is not None and fail_at not in STAGES:
        raise WorkflowError("fail_at must name one canonical checkpoint")
    prior = get_run_by_key_in_transaction(conn,run_key)
    if prior:
        if prior.limits.get("fail_at")!=fail_at or prior.limits.get("fixture_version")!=FIXTURE_VERSION or prior.limits.get("scenario_id")!=scenario_id:
            raise WorkflowError("conflicting demo scenario replay")
        return inspect_in_transaction(conn,run_key=run_key)
    run = create_run_in_transaction(conn,idempotency_key=run_key,stage="handoff_demo",
        limits={"synthetic":True,"fixture_version":FIXTURE_VERSION,"fail_at":fail_at,"scenario_id":scenario_id})
    # Serialize starts before writing append-only versions, including racing replays.
    locked = get_run_in_transaction(conn,run.run_id,for_update=True)
    if locked.limits.get("fail_at")!=fail_at or locked.limits.get("fixture_version")!=FIXTURE_VERSION or locked.limits.get("scenario_id")!=scenario_id:
        raise WorkflowError("conflicting concurrent demo scenario replay")
    if get_record_in_transaction(conn,run.run_id,"research_artifact","handoff-state"):
        return inspect_in_transaction(conn,run_key=run_key)
    payload = _fixture(run_key,None if profile else fail_at)
    if profile: payload=sealed(apply(payload,profile,_fixture_verification(profile["property_type"],profile.get("levels") or 40)))
    put_record_in_transaction(conn,run.run_id,kind="dossier_version",record_key="main",version=1,
                              payload=payload,digest=payload["digest"])
    did = dossier_id_for_property(payload["property_id"])
    version = create_version_in_transaction(conn,property_id=payload["property_id"],created_by="gabriel",
        payload=marked({"dossier":payload["dossier"],"dossier_digest":payload["digest"],"run_id":str(run.run_id)}))
    snapshot_in_transaction(conn,dossier_id=did,version=version.version)
    link = {"dossier_id":str(did),"custody_version":version.version,"property_id":payload["property_id"],
            "dossier_key":"main","receipts":{}}
    put_record_in_transaction(conn,run.run_id,kind="custody_link",record_key="main",version=1,payload=link,digest=digest_payload(link))
    _write_state(conn,run,{"index":0,"phase":"forward","sequence":0,"reason":"","events":[]},1)
    return inspect_in_transaction(conn,run_key=run_key)


def _site(fail=False, profile=None):
    from app.agents.onesimus.service import ClassifiedInput
    from app.agents.onesimus.engineering import SITE_UNITS
    raw = {"usable_area_m2":100,"room_length_m":10,"room_width_m":10,"clear_height_m":4,
        "rack_length_m":2,"rack_width_m":1,"aisle_width_m":1,"service_clearance_m":.5,
        "tier_pitch_m":1,"ceiling_clearance_m":.5,"hvac_w_per_m2":10,"cleaning_l_per_site_cycle":.25}
    if profile:
        raw.update(profile.get("site_overrides") or {})
        if profile.get("fault")=="missing_area": raw["usable_area_m2"]=None
    if fail:
        raw["usable_area_m2"] = None
    return {name:ClassifiedInput(name=name,value=value,unit=SITE_UNITS[name],classification="assumed",
                                provenance="SYNTHETIC invented handoff fixture") for name,value in raw.items()}


def _report(agent, payload, fail_at):
    from app.agents.raphael.inspection import build_inspection_report
    from app.agents.raphael.service import evaluate_dossier
    from app.agents.onesimus.crops import crop_profile
    from app.agents.onesimus.service import evaluate_scenario
    from app.agents.onesimus.report import build_engineering_report
    from app.agents.stages import build_report
    from app.michael import assess
    reports = payload["dossier"]["reports"]
    profile=payload["dossier"].get("demo_scenario")
    if profile: fail_at=None
    if agent=="gabriel":
        ok = bool(payload["dossier"]["sources"])
        report = {"schema_version":"gabriel-discovery-report-1","source_dossier_digest":payload["digest"],
                  "agent":agent,"missing":[] if ok else ["source_evidence"]}
    elif agent=="raphael":
        decision = evaluate_dossier(payload)
        report = build_inspection_report(payload,decision=decision,
            site_inputs={k:v.model_dump(mode="json") for k,v in _site(fail_at=="onesimus",profile).items()})
        ok = decision["outcome"]=="authorized"
        report["missing"] = [] if ok else decision["basis"]
    elif agent=="onesimus":
        crop=crop_profile()
        if profile:
            from app.agents.onesimus.service import ClassifiedInput
            from app.agents.onesimus.engineering import CROP_UNITS
            for name,value in {"ppfd_umol_m2_s":200,"labor_min_per_site_cycle":2,"nutrient_ml_per_site_cycle":10}.items():
                crop[name]=ClassifiedInput(name=name,value=value,unit=CROP_UNITS[name],classification="assumed",provenance="FICTIONAL quantified planning assumption")
        scenario = evaluate_scenario("bench",crop,_site(fail_at==agent,profile))
        scenario["dossier_digest"] = payload["digest"]
        report = build_engineering_report({"bench":scenario},inspection=reports["raphael"])
        if payload['dossier'].get('demo_room_plan'):
            from app.agents.onesimus.portfolio import plan_rooms
            rooms=[dict(room_id=r['room_id'],property_id=payload['property_id'],inspection_digest=reports['raphael']['digest'],distinct_room_claim=True,evidence_refs=['https://records.fiction.example/room'],site={k:v.model_dump(mode='json') for k,v in _site(profile={'site_overrides':r['site_overrides']}).items()}) for r in payload['dossier']['demo_room_plan']]
            report['room_portfolio']=plan_rooms(payload,rooms,crop=crop)
            report=sealed(report)
        ok = bool(scenario["complete"]) and report["status"]=="concept_ready"
    elif agent=="michael":
        scenario = reports["onesimus"]["alternatives"]["bench"]["scenario"]
        finance = _demo_financial_inputs("bench")
        if fail_at==agent or (profile and profile.get("fault")=="market_price"):
            finance.pop("price_per_kg")
        assessment = assess("bench",scenario,finance)
        ok = bool(assessment["complete"]) and (not profile or assessment.get("verdict")=="favorable_under_assumptions")
        report = {"schema_version":"michael-finance-report-1","source_dossier_digest":payload["digest"],
            "source_engineering_digest":reports["onesimus"]["digest"],"assessments":{"bench":assessment},
            "missing":assessment["missing"]}
        if reports['onesimus'].get('room_portfolio'):
            from app.agents.onesimus.portfolio import assess_room_finance
            from app.michael import FinancialInput
            bundle={key:value.model_dump(mode='json') for key,value in _demo_financial_inputs('bench').items()}
            for key,unit in [('occupancy_usd_per_m2_year','USD/m2/year'),('maintenance_usd_per_year','USD/year')]:
                bundle[key]=FinancialInput(name=key,value=0,unit=unit,source='FICTIONAL explicitly allocated zero room allowance',classification='assumed',scenario='fiction').model_dump(mode='json')
            report['room_finance']=assess_room_finance(reports['onesimus']['room_portfolio'],{r['room_id']:deepcopy(bundle) for r in reports['onesimus']['room_portfolio']['rooms']})
    else:
        report = build_report(agent,payload)
        ok = not report["missing"]
    report = marked(report)
    report.update(agent=agent,source_dossier_digest=payload["digest"],
        checkpoint="GO" if ok else "NO-GO",
        checkpoint_scope="Synthetic implemented intake/policy/calculation checks only; not external or professional approval.")
    return sealed(report), ok


def advance_in_transaction(conn, *, run_key, expected_sequence):
    run = _get(conn,run_key,lock=True)
    checkpoint = _state(conn,run)
    state = deepcopy(checkpoint.payload)
    if isinstance(expected_sequence,bool) or not isinstance(expected_sequence,int) or expected_sequence<0:
        raise WorkflowError("expected_sequence must be a non-negative integer")
    if expected_sequence < state["sequence"]:
        return inspect_in_transaction(conn,run_key=run_key)  # retry: no duplicate step
    if expected_sequence!=state["sequence"]:
        raise WorkflowError("sequence ahead of stored checkpoint")
    if state["phase"] in ("complete_go","returned_to_gabriel"):
        return inspect_in_transaction(conn,run_key=run_key)
    if run.limits.get("fixture_version") != FIXTURE_VERSION:
        raise WorkflowError("historical fixture run is read-only; create a new Manny scenario")
    source = get_record_in_transaction(conn,run.run_id,"dossier_version","main")
    if source is None or source.digest!=digest_payload({k:v for k,v in source.payload.items() if k!="digest"}):
        raise WorkflowError("demo dossier failed digest verification")
    did = dossier_id_for_property(source.payload["property_id"])
    verify_chain_in_transaction(conn,did)
    agent = STAGES[state["index"]]
    holder = current_custody_in_transaction(conn,did)
    if (holder["holder"] if holder else "gabriel")!=agent:
        raise WorkflowError("demo checkpoint disagrees with actual canonical custody")
    event = {"sequence":state["sequence"]+1,"agent":agent,"phase":state["phase"]}
    if state["phase"]=="forward":
        from app.dossier_reports import append_agent_report_in_transaction
        report, ok = _report(agent,source.payload,run.limits.get("fail_at"))
        _, latest = append_agent_report_in_transaction(conn,run.run_id,"main",agent=agent,report=report)
        event.update(checkpoint=report["checkpoint"],report_digest=report["digest"],version=latest.version)
        if not ok:
            reason = "SYNTHETIC NO-GO at "+agent+": "+", ".join(report.get("missing") or ["intake checks incomplete"])
            profile=source.payload["dossier"].get("demo_scenario")
            if profile: reason+=" | "+profile["story"]
            state.update(reason=reason[:450],phase="returning" if state["index"] else "returned_to_gabriel")
            event["reason"] = state["reason"]
        elif state["index"]==len(STAGES)-1:
            state["phase"] = "complete_go"
        else:
            target = STAGES[state["index"]+1]
            receipt = issue_receipt_in_transaction(conn,dossier_id=did,version=latest.version,kind="forward",
                from_agent=agent,to_agent=target,issued_by="synthetic-demo-controller",note="SYNTHETIC implemented checkpoint GO; no external approval")
            event.update(to_agent=target,receipt_id=str(receipt.receipt_id))
            state["index"] += 1
    else:
        target = STAGES[state["index"]-1]
        latest = latest_version_in_transaction(conn,did)
        receipt = issue_receipt_in_transaction(conn,dossier_id=did,version=latest.version,kind="return",
            from_agent=agent,to_agent=target,issued_by="synthetic-demo-controller",note=state["reason"])
        event.update(to_agent=target,receipt_id=str(receipt.receipt_id),reason=state["reason"],version=latest.version)
        state["index"] -= 1
        if state["index"]==0:
            state["phase"] = "returned_to_gabriel"
    state["sequence"] += 1
    state["events"].append(marked(event))
    _write_state(conn,run,state,checkpoint.version+1)
    verify_chain_in_transaction(conn,did)
    return inspect_in_transaction(conn,run_key=run_key)


def inspect_in_transaction(conn, *, run_key):
    run = _get(conn,run_key)
    state = _state(conn,run).payload
    source = get_record_in_transaction(conn,run.run_id,"dossier_version","main")
    if (source is None or source.payload.get("domain")!=DOMAIN
            or source.digest!=digest_payload({k:v for k,v in source.payload.items() if k!="digest"})):
        raise WorkflowError("synthetic dossier failed integrity verification")
    did = dossier_id_for_property(source.payload["property_id"])
    latest = latest_version_in_transaction(conn,did)
    if (latest is None or latest.payload.get("dossier_digest")!=source.digest
            or latest.payload.get("dossier")!=source.payload.get("dossier")):
        raise WorkflowError("synthetic display does not bind the current canonical dossier")
    verified = verify_chain_in_transaction(conn,did)
    holder = current_custody_in_transaction(conn,did)
    owner_story=get_record_in_transaction(conn,run.run_id,"research_artifact","showcase-owner-story")
    seed=get_record_in_transaction(conn,run.run_id,"research_artifact","showcase-owner-seed")
    for item in (owner_story,seed):
        if item and item.digest!=digest_payload(item.payload): raise WorkflowError("synthetic owner story failed integrity verification")
    return marked({"owner_story":owner_story.payload if owner_story else None,"owner_obligation_seed":seed.payload if seed else None,"run_key":run_key,"fixture_version":run.limits["fixture_version"],"scenario":run.limits.get("fail_at") or "all_go","scenario_id":run.limits.get("scenario_id"),
        "property_id":source.payload["property_id"],"dossier_id":str(did),"phase":state["phase"],
        "sequence":state["sequence"],"current_holder":holder["holder"] if holder else "gabriel",
        "verified_receipts":verified,"events":deepcopy(state["events"]),
        "dossier":deepcopy(source.payload),"external_actions":0,"provider_calls":0,
        "proof_scope":"Shared canonical custody plus implemented synthetic checkpoint checks; not real property verification."})


def service(action, *, run_key, fail_at=None, scenario_id=None, expected_sequence=None, principal="owner-cli"):
    run_key = _key(run_key)
    from app.properties_db import get_connection
    cfg,key = configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            try:
                if action=="start":
                    result = start_in_transaction(conn,run_key=run_key,fail_at=fail_at,scenario_id=scenario_id)
                elif action=="advance":
                    result = advance_in_transaction(conn,run_key=run_key,expected_sequence=expected_sequence)
                elif action in ("seed_obligation","owner_story"):
                    from app.demo_owner_story import exercise
                    exercise(conn,_get(conn,run_key,lock=True),full=action=="owner_story")
                    result=inspect_in_transaction(conn,run_key=run_key)
                elif action=="inspect":
                    result = inspect_in_transaction(conn,run_key=run_key)
                else:
                    raise WorkflowError("unknown demo action")
                if action!="inspect":
                    from app.observability.store import insert_event_in_transaction
                    from app.observability.events import AgentEvent, AgentId, RetentionClass
                    insert_event_in_transaction(conn,AgentEvent(agent=AgentId.WONDRUE,run_id=run_key,event_type="demo.handoff_step",
                        summary="SYNTHETIC canonical handoff demonstration only",details={"run_key":run_key,"sequence":result["sequence"],"synthetic":True,"principal":principal},
                        retention_class=RetentionClass.PERMANENT))
                    conn.commit()
                else:
                    conn.rollback()
                return result
            except Exception:
                conn.rollback()
                raise


def initialize():
    from app.properties_db import get_connection
    from app.migrate import run_migrations
    cfg,key = configuration()
    with get_connection(cfg) as conn:
        assert_database(conn)
    # Explicit init only; never automatically run during requests.
    return {"database":DATABASE,"role":ROLE,"applied":run_migrations(config=cfg),"synthetic":True}


def run_bounded_scenario(run_key, fail_at=None, scenario_id=None):
    result = service("start",run_key=run_key,fail_at=fail_at,scenario_id=scenario_id)
    for _ in range(MAX_STEPS):
        if result["phase"] in ("complete_go","returned_to_gabriel"):
            return result
        result = service("advance",run_key=run_key,expected_sequence=result["sequence"])
    raise WorkflowError("bounded demo did not reach a terminal checkpoint")


def run_suite(prefix):
    _key(prefix)
    if len(prefix)>60:
        raise WorkflowError("suite prefix must be at most 60 characters")
    results = []
    for fail_at in (None,*STAGES):
        run_key = prefix+("-go" if fail_at is None else "-nogo-"+fail_at)
        result = run_bounded_scenario(run_key,fail_at)
        expected_phase = "complete_go" if fail_at is None else "returned_to_gabriel"
        expected_holder = "manny" if fail_at is None else "gabriel"
        failed = [e for e in result["events"] if e.get("checkpoint")=="NO-GO"]
        if result["phase"]!=expected_phase or result["current_holder"]!=expected_holder:
            raise WorkflowError("synthetic scenario failed terminal custody assertion")
        if fail_at is not None and (len(failed)!=1 or failed[0]["agent"]!=fail_at):
            raise WorkflowError("synthetic rejection did not occur at its intended checkpoint")
        results.append({k:result[k] for k in ("run_key","scenario","phase","current_holder","verified_receipts","sequence")})
    return marked({"result":"PASS","scenarios":len(results),"results":results,
        "proof_backend":"dedicated PostgreSQL demo database; actual canonical version/snapshot/receipt services",
        "scope":"Synthetic implemented checkpoints only; not real source authentication or specialist approval.",
        "provider_calls":0,"operational_writes":0,"external_actions":0})



def run_catalogue(prefix):
    from app.demo_catalogue import CASES
    _key(prefix)
    if len(prefix)>55: raise WorkflowError('catalogue prefix must be at most 55 characters')
    results=[]
    for profile in CASES:
        key=prefix+'-'+profile['id']
        result=run_bounded_scenario(key,scenario_id=profile['id'])
        expected='returned_to_gabriel' if profile['expected_failure'] else 'complete_go'
        if result['phase']!=expected: raise WorkflowError('catalogue terminal outcome differs from declared expectation')
        failures=[e['agent'] for e in result['events'] if e.get('checkpoint')=='NO-GO']
        if failures!=([profile['expected_failure']] if profile['expected_failure'] else []): raise WorkflowError('catalogue rejected at an unintended checkpoint')
        if not profile['expected_failure']:
            result=service('owner_story' if profile['id']=='elm-fork' else 'seed_obligation',run_key=key)
        results.append(dict(run_key=key,name=profile['name'],city=profile['city'],state=profile['state'],property_type=profile['property_type'],
            phase=result['phase'],current_holder=result['current_holder'],verified_receipts=result['verified_receipts'],failure=failures,
            owner_obligation_seed=result['owner_obligation_seed'],owner_story=result['owner_story']))
    return marked(dict(result='PASS',scenarios=len(results),results=results,provider_calls=0,external_actions=0,operational_writes=0,
        coverage='16 invented properties across US regions, Alaska and Hawaii; 4 per property type. Outdoor admission remains fail-closed.'))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("init","start","advance","inspect","run","suite","catalogue","catalogue-suite","showcase-suite","seed_obligation","owner_story"))
    parser.add_argument("--run-key")
    parser.add_argument("--fail-at",choices=STAGES)
    parser.add_argument("--sequence",type=int)
    parser.add_argument("--scenario-id")
    args = parser.parse_args()
    if args.action=="catalogue":
        from app.demo_catalogue import catalogue
        result=catalogue()
    elif args.action=="showcase-suite":
        matrix=run_suite(args.run_key+"-matrix")
        catalogue=run_catalogue(args.run_key+"-us")
        result=marked(dict(result="PASS",scenarios=matrix["scenarios"]+catalogue["scenarios"],checkpoint_matrix=matrix,property_catalogue=catalogue,provider_calls=0,operational_writes=0,external_actions=0))
    elif args.action=="catalogue-suite":
        result=run_catalogue(args.run_key)
    elif args.action=="init":
        result = initialize()
    elif args.action=="run":
        result = run_bounded_scenario(args.run_key,args.fail_at,scenario_id=args.scenario_id)
    elif args.action=="suite":
        result = run_suite(args.run_key)
    else:
        result = service(args.action,run_key=args.run_key,fail_at=args.fail_at,scenario_id=args.scenario_id,expected_sequence=args.sequence)
    print(json.dumps(result,indent=2,default=str))


if __name__=="__main__":
    main()
