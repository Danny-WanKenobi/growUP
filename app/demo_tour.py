"""Read-only narrated demonstration; its connector can only open the demo DB."""
from app import handoff_demo as demo
from app.demo_story import package_id,run_key,seed_cases,program
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction

# About 540 words at normal conversational pace. Text is also the fallback.
CHAPTERS=(
 ('Welcome','Welcome. I’m Wondrue, your Grow Up Cockpit Agent. Let’s follow an underused property from discovery to an owner decision. This is an isolated synthetic demonstration. Some address references came from earlier real discovery, but the conversion stories, missing measurements, approvals and business outcomes are fictional. No owner has agreed, no contract has been signed, and no message or construction action will occur during this tour.'),
 ('The opportunity','Grow Up explores whether underused buildings could support preliminary indoor-farming concepts. The research library documents office-market challenges and also retains recovery and premium-building counterevidence. A market trend never proves that an individual owner is distressed. Our expansion story imagines flagship conversions across Texas incorporated places and every other U.S. state; those planned slots are not completed projects or verified tallest buildings.'),
 ('Gabriel and Raphael','Gabriel organizes candidate identity, sources and research gaps. Raphael applies the shared verification gates. Here, synthetic evidence lets a demonstration dossier advance. The failed examples matter just as much: an identity conflict returns a dossier to Gabriel, and an unsupported outdoor case stays a conceptual food-forest opportunity. Nothing quietly overrides a missing fact to manufacture a real authorization.'),
 ('Onesimus','Onesimus turns explicitly classified dimensions into passive Kratky concepts. Plants use non-circulating reservoirs with root-zone air space. Racks, aisles and separate rooms affect usable growing area. Water, nutrients, lighting, climate control and labor remain quantified planning inputs. Tenant consolidation is a hypothetical scenario requiring consent and other reviews, never a claim that tenants can simply be moved.'),
 ('Michael','Michael uses inspectable Python arithmetic to estimate costs, revenue and operating results. Compare alternatives rather than adding incompatible layouts together. Price declines, higher energy costs, delays and relocation allowances reveal fragile assumptions. A favorable result under supplied assumptions is not a promise of profit, financing or building capacity.'),
 ('Uriel and Priscilla','Uriel prepares evidence-linked campaign material, with Simeon as the human-conversation contact. Priscilla tracks actual recorded campaign and conversion events; simulated events remain labeled. The inbound-only Customer Service Agent handles general questions and escalates to Simeon or Solomon. Suppression, permissions and duplicate-send guards remain separate from a compelling draft. This tour sends no email, letter or advertisement.'),
 ('Nicole, Theresia and Simeon','Nicole surfaces jurisdiction and permitting gaps. Theresia organizes underwriting conditions and conditional contract preparation. Simeon checks closing readiness and manages inbound conversations. An unanswered question becomes a traceable evidence request or owner exception. A simulated consent record in this demo is never permission to execute a real agreement.'),
 ('Solomon and advisers','Solomon plans phases, inspection checkpoints, costs and change orders. Our expansion story includes seven separately tailored robotics proposals and fictional contract outcomes. Those companies have not been contacted and no partnership is implied. Yoseph and Deborah support risk and legal questions outside the custody pipeline. Construction capability, safety, insurance and qualifications require real evidence before execution.'),
 ('Elmer and Manny','Elmer distinguishes estimates, commitments, invoices and actual expenses. Corrections append linked accounting assessments instead of rewriting history. Manny receives the consolidated owner package, protected filing and dated obligations. The warehouse lifecycle shows overdue review, correction and later reopening while preserving the earlier filing.'),
 ('Custody and control','Every demonstrated transition uses the existing canonical dossier, immutable snapshots and signed custody receipts. Rejection reasons travel upstream with the package. The tour shows retained results, not a prerecorded claim of success. Original reports remain available; exceptions and consequential decisions stay controlled. That is Grow Up: ambitious property reuse, deterministic analysis, honest uncertainty and a traceable path from opportunity to owner review.')
)


def snapshot(result):
    payload=result['dossier'];d=payload['dossier'];reports=d['reports'];profile=d.get('demo_scenario') or {}
    return {'run_key':result['run_key'],'scenario_id':result['scenario_id'],'name':d['name'],'city':d['city'],'state':d['state'],
        'property_type':profile.get('property_type'),'synthetic':True,'phase':result['phase'],'holder':result['current_holder'],
        'verified_receipts':result['verified_receipts'],'version':payload['version'],'digest':payload['digest'],
        'events':result['events'],'assumptions':profile.get('synthetic_augmentation',[]),'real_reference':profile.get('real_reference'),
        'engineering':(reports.get('onesimus') or {}).get('room_portfolio') or (reports.get('onesimus') or {}).get('alternatives'),
        'finances':(reports.get('michael') or {}).get('room_finance') or (reports.get('michael') or {}).get('assessments'),
        'owner_lifecycle':result.get('owner_story'),'food_forest':d.get('food_forest_preview'),
        'report_digests':{name:r.get('digest') for name,r in reports.items()},'property_id':payload['property_id']}


def inspect_in_transaction(conn):
    from app.handoff_demo_domain import assert_database
    from app.communications import read
    assert_database(conn);anchor=demo._get(conn,run_key(seed_cases()[0]['id']))
    record=read(conn,anchor.run_id,'showcase-tour-manifest')
    if record is None:raise WorkflowError('Prepare the isolated demo-tour package first; no operational fallback is permitted')
    conversion_options=None
    for item in record.payload['cases']:
        run=demo._get(conn,item['run_key']);row=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
        if row is None or row.digest!=item['digest'] or row.digest!=digest_payload({k:v for k,v in row.payload.items() if k!='digest'}):raise WorkflowError('A tour source changed; preserve it and rebuild the reviewed package')
        from app.custody import dossier_id_for_property
        from app.custody_db import verify_chain_in_transaction,current_custody_in_transaction
        did=dossier_id_for_property(item['property_id'])
        if verify_chain_in_transaction(conn,did)!=item['verified_receipts'] or (current_custody_in_transaction(conn,did) or {}).get('holder','gabriel')!=item['holder']:
            raise WorkflowError('Tour custody changed; inspect the retained chain before recording')
        if item.get('scenario_id')=='hudson-sky':
            from app.conversion_demo import build
            conversion_options=build(row.payload)
    from app.demo_knowledge import library
    return demo.marked({'package_id':package_id(),'database':'growup_handoff_demo','chapters':[{'title':title,'text':text} for title,text in CHAPTERS],
        'cases':record.payload['cases'],'program':program(),'knowledge':library(),'manifest_digest':record.digest,
        'conversion_options':conversion_options,
        'voice_scope':'Local browser voices preferred. Microphone requires a user gesture; optional browser recognition may transmit audio to its provider. No paid speech adapter.',
        'provider_calls':0,'operational_writes':0,'external_actions':0})


def service():
    from app.properties_db import get_connection
    from app.handoff_demo_domain import scope
    cfg,key=demo.configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            with conn.cursor() as cur:cur.execute('SET TRANSACTION READ ONLY')
            result=inspect_in_transaction(conn);conn.rollback();return result
