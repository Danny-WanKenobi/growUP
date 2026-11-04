"""One isolated, retained-dossier visual proof. No transport or dossier mutation."""
from base64 import b64decode
from copy import deepcopy
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from hashlib import sha256
from html import escape
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

from app import handoff_demo as demo
from app.demo import DEMO_LABEL
from app.handoff_demo_domain import DOMAIN, scope
from app.workflow import digest_payload
from app.workflow_db import (WorkflowError, create_run_in_transaction, get_record_in_transaction,
                             put_record_in_transaction,get_run_by_key_in_transaction)

VERSION='retained-visual-proof-1'
DEFAULT_SOURCE='demo-handoff-tour-b41f70297d8c-hudson-sky'
START='2026-11-05T10:00:00-06:00'
ZONE='America/Chicago'
REPLY=('DRAFT FOR A REQUESTED CONVERSATION — no real inbound message is correlated.\n\n'
       'I’m Simeon. These illustrations show a preliminary passive Kratky concept for the fictional Hudson Lantern Tower. '
       'The two-room plan and single-room scenario are alternatives, not additive production or financial promises. '
       'Which space, operating assumption or next evidence step would you like to discuss? '
       'No agreement, site capacity or construction work is authorized by this draft.')


def _intact(report):
    if not isinstance(report,dict) or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'}):
        raise WorkflowError('retained proof report integrity failure')


def build(source, *, start_at=START, timezone=ZONE):
    """All art/copy derives from this one verified synthetic source envelope."""
    from app.visual_design import visual_assessment,reuse_visuals,validate_visual_package
    from app.uriel import prepare_campaign
    from app.uriel_campaign import demo_message,DEMO_RECIPIENT,SENDER,CONTACT
    payload=source['dossier']
    if (source.get('synthetic') is not True or payload.get('domain')!=DOMAIN
            or payload.get('digest')!=digest_payload({k:v for k,v in payload.items() if k!='digest'})):
        raise WorkflowError('verified isolated synthetic dossier required')
    reports=payload['dossier']['reports'];engineering=reports['onesimus'];financial=reports['michael']
    for name in ('gabriel','raphael','onesimus','michael'):_intact(reports[name])
    if financial.get('source_engineering_digest')!=engineering['digest']:
        raise WorkflowError('retained financial/engineering lineage mismatch')
    before=deepcopy(source)
    assessment=visual_assessment(engineering,source_dossier_digest=payload['digest'])
    campaign=prepare_campaign(payload,visual_assessment=assessment)
    if campaign['missing']:
        raise WorkflowError('retained campaign has unresolved requirements: '+', '.join(campaign['missing']))
    validate_visual_package(campaign['visual_package'])
    prepared=demo_message(campaign,start_at=datetime.fromisoformat(start_at),timezone_name=timezone)
    package={'entry_id':'proof:'+campaign['campaign_id'],'payload':{'campaign':campaign}}
    reply=reuse_visuals(package,actor='simeon',text=REPLY)
    if not reply.get('html') or reply['attachments'][0]['content']!=campaign['visual_package']['png_assets'][0]['content']:
        raise WorkflowError('Simeon reply must reuse exact retained Uriel image')
    if source!=before:raise WorkflowError('visual proof mutated source dossier')
    with ZipFile(BytesIO(prepared['zip_bytes'])) as archive:
        names=archive.namelist()
        if len([n for n in names if n.startswith('artwork/') and n.endswith('.html')])!=11:
            raise WorkflowError('complete illustrated campaign required')
    identity={'version':VERSION,'source_run_key':source['run_key'],'source_dossier_digest':payload['digest'],
              'campaign_id':campaign['campaign_id'],'start_at':start_at,'timezone':timezone,'reply_text_digest':digest_payload({'text':REPLY})}
    proof_key='visual-proof:'+digest_payload(identity)
    manifest={'version':VERSION,'result':'PASS','synthetic':True,'domain':DOMAIN,'demo_label':DEMO_LABEL,
        'proof_key':proof_key,'source_run_key':source['run_key'],'property_id':source['property_id'],
        'dossier_id':source['dossier_id'],'source_dossier_digest':payload['digest'],
        'source_engineering_digest':engineering['digest'],'source_financial_digest':financial['digest'],
        'source_custody_receipts_verified':source['verified_receipts'],'source_holder':source['current_holder'],
        'visual_assessment_digest':assessment['digest'],'concept_count':3,
        'concept_sha256':[c['sha256'] for c in assessment['conceptualizations']],
        'png_sha256':[p['sha256'] for p in campaign['visual_package']['png_assets']],
        'room_ids':[r['room_id'] for r in engineering.get('room_portfolio',{}).get('rooms',[])],
        'room_portfolio_digest':assessment['room_portfolio_digest'],'designed_materials':11,
        'zip_bytes':len(prepared['zip_bytes']),'zip_sha256':sha256(prepared['zip_bytes']).hexdigest(),
        'uriel_eml_sha256':sha256(prepared['message_bytes']).hexdigest(),
        'simeon_reused_png_sha256':reply['attachments'][0]['sha256'],
        'source_photo_count':len(campaign['visual_package']['source_photos']),
        'art_credit':'Onesimus conceptual views; Uriel locally rendered original PNGs. No Canva or source photos asserted.',
        'contact':{'prospective_recipient':DEMO_RECIPIENT,'sender':SENDER,'reply_to':CONTACT},
        'itinerary':prepared['itinerary'],'provider_calls':0,'external_actions':0,'operational_writes':0,
        'source_dossier_changed':False,'source_custody_changed':False,'calculations_changed':False,
        'real_inbound_correlated':False,'simeon_delivery_enabled':False,
        'scope':'Linked visual assessment over retained synthetic calculations; existing custody verified, no new promotion or specialist approval.'}
    content={'synthetic':True,'domain':DOMAIN,'manifest':manifest,'visual_assessment':assessment,
             'campaign':campaign,'simeon_reply':reply,'source_reference':identity}
    content['digest']=digest_payload(content)
    return content,prepared


def prepare_in_transaction(conn, *, source_run_key=DEFAULT_SOURCE):
    """Append one proof artifact; current dossier, reports and receipts stay intact."""
    from app.custody_db import lock_dossier_in_transaction
    initial=demo.inspect_in_transaction(conn,run_key=source_run_key)
    from uuid import UUID
    lock_dossier_in_transaction(conn,UUID(initial['dossier_id']))
    source=demo.inspect_in_transaction(conn,run_key=source_run_key)
    content,_=build(source)
    record_digest=digest_payload(content)
    with conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%(key)s))',{'key':content['manifest']['proof_key']})
    run=get_run_by_key_in_transaction(conn,content['manifest']['proof_key'])
    if run is None:
        run=create_run_in_transaction(conn,idempotency_key=content['manifest']['proof_key'],stage='visual_proof',
                                      limits={'synthetic':True,'source_run_key':source_run_key})
    prior=get_record_in_transaction(conn,run.run_id,'research_artifact','visual-proof-package')
    if prior:
        if prior.digest!=record_digest or prior.payload!=content:
            raise WorkflowError('stored visual proof differs; preserve prior evidence')
        replayed=True
    else:
        put_record_in_transaction(conn,run.run_id,kind='research_artifact',record_key='visual-proof-package',
                                  version=1,payload=content,digest=record_digest)
        replayed=False
    after=demo.inspect_in_transaction(conn,run_key=source_run_key)
    if source!=after:raise WorkflowError('original dossier/custody changed during proof')
    return {'proof_run_id':str(run.run_id),'record_key':'visual-proof-package','artifact_digest':record_digest,
            'replayed':replayed,**content['manifest']}


def prepare(source_run_key=DEFAULT_SOURCE):
    from app.observability.events import AgentEvent,AgentId,RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    cfg,key=demo.configuration()
    def mutate(conn):
        with scope(conn,key):return prepare_in_transaction(conn,source_run_key=source_run_key)
    event=AgentEvent(agent=AgentId.URIEL,run_id=source_run_key,event_type='demo.visual_proof',
        summary='Retained synthetic dossier visual proof; no delivery or dossier mutation',
        details={'synthetic':True,'source_run_key':source_run_key},retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(config=cfg,operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,event=event,mutate=mutate)


def _rows(mapping):
    def cell(value):
        if isinstance(value,dict):return '<ul>'+''.join('<li>'+escape(str(k).replace('_',' '))+': '+cell(v)+'</li>' for k,v in value.items())+'</ul>'
        if isinstance(value,list):return '<ol>'+''.join('<li>'+cell(v)+'</li>' for v in value)+'</ol>'
        if value is None:return 'unknown'
        if isinstance(value,bool):return 'yes' if value else 'no'
        return escape(str(value))
    return ''.join('<tr><th>'+escape(str(k).replace('_',' '))+'</th><td>'+cell(v)+'</td></tr>' for k,v in mapping.items())


def review_html(source, result):
    d=source['dossier']['dossier'];e=d['reports']['onesimus'];m=d['reports']['michael'];manifest=result['manifest']
    room_rows=''
    for room in e.get('room_portfolio',{}).get('rooms',[]):
        scenario=room.get('selected_scenario') or {};inputs=scenario.get('inputs') or {}
        def quantity(name):
            item=inputs.get(name) or {};return str(item.get('value','unknown'))+' '+str(item.get('unit',''))+' / '+str(item.get('classification','unknown'))
        room_rows+='<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in (room['room_id'],room.get('selected_config'),quantity('room_length_m'),quantity('room_width_m'),quantity('clear_height_m')) )+'</tr>'
    single=''
    headline=('capex_usd','revenue_usd_per_year','opex_usd_per_year','operating_net_usd_per_year',
              'monthly_operating_cashflow_usd','occupancy_quantified','maintenance_quantified')
    for name,assessment in (m.get('assessments') or {}).items():
        values=assessment.get('outputs') or {}
        single+='<h3>'+escape(name)+'</h3><table>'+_rows({k:values[k] for k in headline if k in values})+'</table>'
        if values.get('occupancy_quantified') is False or values.get('maintenance_quantified') is False:
            single+='<p>Important retained gap: occupancy and/or maintenance costs are not quantified in this single-room assessment. Its operating net is not a complete verified budget.</p>'
        single+='<details><summary>Retained calculation basis and sensitivities</summary><table>'+_rows({k:v for k,v in values.items() if k not in headline})+'</table></details>'
    portfolio=(m.get('room_finance') or {}).get('totals') or {}
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Hudson Lantern / retained visual proof</title><style>body{font:17px/1.6 Arial;background:#e8eee7;color:#143a35;margin:0}main{max-width:1100px;margin:auto;padding:28px}h1{font-size:38px}section{background:white;padding:24px;margin:20px 0;border-radius:14px}img{max-width:100%;border-radius:10px}.concepts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}a{color:#235e32}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border-bottom:1px solid #ddd;text-align:left;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}code{overflow-wrap:anywhere}.scroll{overflow-x:auto}</style></head><body><main>'
        '<p>SYNTHETIC DEMONSTRATION / NO EMAIL SENT</p><h1>'+escape(d.get('name') or 'Retained synthetic dossier')+'</h1>'
        '<p>One existing dossier. Original engineering, financial reports and custody preserved. This linked assessment adds visual materials only.</p>'
        '<section><h2>1. Three concepts, retained assumptions</h2><div class="concepts">'+''.join('<figure><img src="artwork/concepts/onesimus-concept-'+str(i)+'.png" alt="Concept view '+str(i)+'"><figcaption>'+escape(result['visual_assessment']['conceptualizations'][i-1]['title'])+'</figcaption></figure>' for i in (1,2,3))+
        '</div><p>These drawings depict the retained main bench scenario, not a measured model of the tower or the separately selected room portfolio. The room plans below remain distinct alternatives.</p><div class="scroll"><table><tr><th>Room</th><th>Configuration</th><th>Length</th><th>Width</th><th>Height</th></tr>'+room_rows+'</table></div></section>'
        '<section><h2>2. Finances stay unchanged</h2><p>Conditional synthetic estimates. Single-room and portfolio results are alternatives and must not be added together.</p>'+single+'<h3>Room portfolio totals</h3><table>'+_rows(portfolio)+'</table></section>'
        '<section><h2>3. Complete illustrated campaign</h2><p>Four emails, three letters, two flyers, brochure and card. Dates are hypothetical; nothing is scheduled.</p><p><a href="growup-visual-campaign.zip">Campaign ZIP</a> · <a href="artwork/brochure.html">Brochure</a> · <a href="artwork/business_card.html">Business card</a> · <a href="uriel-review.eml">Prepared Uriel email</a></p><table>'+''.join('<tr><th>Burst '+str(t['burst'])+'</th><td>'+escape(t['proposed_at'])+'</td><td>'+str(t['timezone'])+'</td></tr>' for t in manifest['itinerary'])+'</table></section>'
        '<section><h2>4. Simeon writes; retained imagery follows</h2><p>'+escape(REPLY).replace('\n','<br>')+'</p><img src="artwork/concepts/onesimus-concept-1.png" alt="Exact retained campaign image"><p><a href="simeon-draft.eml">Simeon draft</a>. No real inbound correlation exists; this draft cannot initiate contact.</p><p>Reused PNG SHA-256: <code>'+manifest['simeon_reused_png_sha256']+'</code></p></section>'
        '<section><h2>5. Source and custody trace</h2><table>'+_rows({k:manifest[k] for k in ('source_run_key','source_dossier_digest','source_engineering_digest','source_financial_digest','source_holder','source_custody_receipts_verified','visual_assessment_digest','zip_sha256')})+'</table><p>'+escape(manifest['art_credit'])+'</p><a href="proof.json">Read the proof manifest</a></section></main></body></html>')


def export(result, destination):
    """Read committed proof and its unchanged source, then export review files only."""
    from uuid import UUID
    from app.properties_db import get_connection
    cfg,key=demo.configuration()
    with get_connection(cfg) as conn:
        with scope(conn,key):
            with conn.cursor() as cur:cur.execute('SET TRANSACTION READ ONLY')
            record=get_record_in_transaction(conn,UUID(result['proof_run_id']),'research_artifact','visual-proof-package')
            if (record is None or record.digest!=digest_payload(record.payload)
                    or record.payload.get('digest')!=digest_payload({k:v for k,v in record.payload.items() if k!='digest'})):
                raise WorkflowError('committed visual proof integrity failure')
            content=record.payload;source=demo.inspect_in_transaction(conn,run_key=content['manifest']['source_run_key'])
            rebuilt,prepared=build(source)
            if rebuilt!=content:raise WorkflowError('retained source/proof changed before export')
            conn.rollback()
    dest=Path(destination);dest.mkdir(parents=True,exist_ok=True,mode=0o700)
    (dest/'growup-visual-campaign.zip').write_bytes(prepared['zip_bytes'])
    (dest/'uriel-review.eml').write_bytes(prepared['message_bytes'])
    reply=EmailMessage(policy=SMTP)
    from app.uriel_campaign import DEMO_RECIPIENT,CONTACT
    reply['From']=CONTACT;reply['To']=DEMO_RECIPIENT;reply['Subject']='[DRAFT ONLY] Hudson Lantern visual conversation'
    reply['X-Growup-Status']='synthetic-draft-only-no-inbound-correlation'
    reply.set_content(REPLY);reply.add_alternative(content['simeon_reply']['html'],subtype='html')
    image=content['simeon_reply']['attachments'][0]
    reply.get_payload()[-1].add_related(b64decode(image['content']),maintype='image',subtype='png',cid='<growup-retained-concept>')
    for index,part in enumerate(reply.walk()):
        if part.is_multipart():part.set_boundary('growup-reply-'+content['digest'][:40]+'-'+str(index))
    (dest/'simeon-draft.eml').write_bytes(reply.as_bytes())
    with ZipFile(BytesIO(prepared['zip_bytes'])) as archive:
        # Generated names were validated in the campaign builder; also constrain
        # export here so a future filename change cannot escape the review folder.
        for name in archive.namelist():
            target=(dest/name).resolve()
            if not target.is_relative_to(dest.resolve()):raise WorkflowError('unsafe campaign export path')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(name))
    (dest/'proof.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (dest/'index.html').write_text(review_html(source,content),encoding='utf-8')
    return dest


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--source-run-key',default=DEFAULT_SOURCE);parser.add_argument('--output',required=True)
    args=parser.parse_args();result=prepare(args.source_run_key);export(result,args.output)
    print(json.dumps(result))


if __name__=='__main__':main()
