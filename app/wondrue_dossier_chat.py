"""Pinned, evidence-linked conversations. No inferred authorization or new math."""
from copy import deepcopy
from uuid import UUID
import json
from app import wondrue_workspace as ws
from app.communications import instant
from app.workflow import digest_payload
from app.workflow_db import WorkflowError,get_record_in_transaction


def select(conn,chat_id,request_id,expected_revision,reference):
    run,meta=ws.chat(conn,chat_id)
    request_id=str(UUID(str(request_id)))
    fingerprint=digest_payload({'expected_revision':expected_revision,'reference':reference})
    prior=ws.read(conn,run.run_id,'selection:'+request_id)
    if prior:
        if prior.payload['request_digest']!=fingerprint:raise WorkflowError('selection identity conflict')
        return meta.payload
    revision=meta.payload.get('context_revision',0)
    if revision!=expected_revision:raise WorkflowError('conversation selection changed; reload before selecting')
    context=None
    if reference is not None:
        source=ws.record(conn,reference['run_id'],'dossier_version',reference['key'],reference['version'])
        if source['digest']!=reference['digest']:raise WorkflowError('selected dossier digest changed')
        d=source['data'].get('dossier') or {}
        context={k:source[k] for k in ('run_id','key','version','digest','kind')}
        context['title']=str(d.get('name') or d.get('street_address') or source['key'])[:180]
    state={**meta.payload,'context':context,'context_revision':revision+1,'updated_at':instant().isoformat()}
    ws.write(conn,run.run_id,'selection:'+request_id,{'request_digest':fingerprint,'previous_revision':revision,
        'context':context,'context_revision':revision+1,'selected_at':state['updated_at']})
    ws.write(conn,run.run_id,'chat',state,meta)
    return state


def explain(conn,context,text):
    source=ws.record(conn,context['run_id'],'dossier_version',context['key'],context['version'])
    if source['digest']!=context['digest']:raise WorkflowError('pinned dossier integrity changed; refusing an answer')
    latest=ws.record(conn,context['run_id'],'dossier_version',context['key'])
    d=source['data'].get('dossier') or {}
    from app.linked_reassessments import effective_reports,stale_dependencies
    reports=effective_reports(d)
    for name,report in reports.items():
        if not isinstance(report,dict) or report.get('digest')!=digest_payload({k:v for k,v in report.items() if k!='digest'}):
            raise WorkflowError('retained '+name+' report failed integrity verification')
    stale=stale_dependencies(reports)
    decision=get_record_in_transaction(conn,UUID(context['run_id']),'raphael_decision',source['digest'],1)
    if decision and (not ws.valid_record('raphael_decision',decision.payload,decision.digest)
            or decision.payload.get('dossier_key')!=context['key']
            or decision.payload.get('dossier_digest')!=source['digest']
            or decision.payload.get('dossier_version')!=source['version']):
        raise WorkflowError('Raphael decision does not bind this exact dossier version')
    words=text.lower()
    if any(w in words for w in ('approve','change','update','delete','send','prepare','promote','resolve','rework','review')):
        topic='review'
    elif any(w in words for w in ('finance','financial','money','cost','profit','revenue','payback','economics')):topic='finances'
    elif any(w in words for w in ('engineer','layout','water','lighting','grow','kratky','area')):topic='engineering'
    elif any(w in words for w in ('gap','missing','evidence','block','unknown','why','risk','verification')):topic='gaps'
    else:topic='progress'
    data={};cited=[]
    if topic=='review':
        message='Open this exact dossier to propose a supported internal action in Hold for Human Review. No change has been made. Approval never authenticates missing evidence.'
    elif topic=='engineering':
        r=reports.get('onesimus')
        message='Onesimus has no retained engineering report on this selected version.' if r is None else 'These are retained conceptual engineering scenarios, with their stated assumptions and unknowns. They do not establish building capacity or construction readiness.'
        if r:
            data={k:deepcopy(r[k]) for k in ('status','missing','alternatives','room_portfolio','lighting','water','limitations') if k in r};cited=['onesimus']
    elif topic=='finances':
        r=reports.get('michael')
        message='Michael has no retained financial report on this selected version. Profitability is unknown.' if r is None else 'These are Michael’s stored calculations and planning cases. No figures were recalculated; alternatives are not additive, and estimated returns are not guaranteed.'
        if r:
            data={k:deepcopy(r[k]) for k in ('status','missing','assessments','planning_cases','room_finance','limitations') if k in r};cited=['michael']
    elif topic=='gaps':
        message='These gaps and qualifications are retained in the selected dossier and reports. An empty list does not independently establish authorization.'
        for name,r in reports.items():
            gaps={k:deepcopy(r[k]) for k in ('missing','basis','decision_basis','unknowns','not_quantified','limitations') if r.get(k)}
            if gaps:data[name]=gaps;cited.append(name)
        if not data:message='No explicit gap list is retained on this selected version. That does not establish completeness, verification or authorization.'
        for key in ('county_attribution','category_attribution'):
            if d.get(key):data[key]=deepcopy(d[key])
    else:
        message='Progress is shown from retained reports on the selected version. A report’s presence is not proof that the next handoff succeeded.'
        data['retained_reports']=[{'agent':name,'status':r.get('status') or r.get('decision') or r.get('outcome') or 'report retained',
            'dependency_status':'stale' if name in stale else 'no recorded dependency mismatch'} for name,r in reports.items()]
        cited=list(reports)
        if not reports:message='No agent reports are retained on this selected dossier version. Downstream progress is unknown.'
        from app.custody import dossier_id_for_property,CustodyError
        from app.custody_db import verify_chain_in_transaction,current_custody_in_transaction
        try:
            did=dossier_id_for_property(source['data'].get('property_id'))
            receipts=verify_chain_in_transaction(conn,did)
            holder=current_custody_in_transaction(conn,did)
            data['current_custody']={'as_of':instant().isoformat(),'holder':holder['holder'] if holder else 'unknown',
                'version':holder.get('version') if holder else None,'verified_receipts':receipts,
                'scope':'Current custody at answer time, not historical custody of the pinned source; receipt integrity does not verify property claims.'}
        except (CustodyError,ValueError):
            data['current_custody']={'status':'not verified','holder':'unknown','scope':'No custody or property authorization assertion is made.'}
    if decision and topic in ('gaps','progress'):
        data['raphael_decision']={k:deepcopy(decision.payload[k]) for k in ('outcome','basis','scope') if k in decision.payload}
        if topic=='gaps':message='Raphael’s recorded decision and evidence requirements for this exact dossier version are shown below. No authorization was changed.'
    # Readable highlights quote stored quantities/reasons; never calculate or fill gaps.
    highlights=[]
    if topic=='gaps':
        for name,r in reports.items():
            reasons=r.get('missing') or r.get('basis') or r.get('decision_basis') or []
            if isinstance(reasons,list) and reasons:
                highlights.append(name.title()+': '+', '.join(str(v).replace('_',' ') for v in reasons[:8]))
        if decision:highlights.append('Raphael: '+str(decision.payload.get('outcome','unknown')).replace('_',' ')+' — '+', '.join(str(v) for v in decision.payload.get('basis',[])[:8]))
    elif topic=='finances' and reports.get('michael'):
        for name,item in list((reports['michael'].get('assessments') or {}).items())[:4]:
            values=[str(k).replace('_',' ')+': '+str(v) for k,v in (item.get('outputs') or {}).items() if v is None or isinstance(v,(str,int,float))]
            highlights.append(str(name)+' — '+str(item.get('verdict','status not recorded')).replace('_',' ')+('. '+ '; '.join(values[:8]) if values else '. No computed figures recorded.'))
    elif topic=='engineering' and reports.get('onesimus'):
        for name,item in list((reports['onesimus'].get('alternatives') or {}).items())[:4]:
            quantities=item.get('bill_of_quantities') or []
            values=[str(q.get('name','quantity')).replace('_',' ')+': '+str(q.get('value','unknown'))+' '+str(q.get('unit','')) for q in quantities[:6] if isinstance(q,dict)]
            highlights.append(str(name)+': '+('; '.join(values) if values else 'See the retained scenario for inputs and missing quantities.'))
    elif topic=='progress' and reports:
        highlights.append('Retained reports: '+', '.join(name.title() for name in reports)+'.')
    if highlights:message+='\n\n'+'\n'.join(highlights[:10])
    if stale:
        data['stale_report_dependencies']=stale
        message+=' Some report dependencies are stale; those reports must not be treated as current assessments.'
    newer=latest['version']!=source['version'] or latest['digest']!=source['digest']
    if newer:message+=f" A newer dossier version ({latest['version']}) exists. This answer remains pinned to version {source['version']}; select the newer version explicitly to change context."
    if len(json.dumps(data))>60000:
        data={'inline_details':'Report exceeds the conversation display bound. Open the exact cited dossier to inspect all retained details.'}
    def report_path(name):
        path='reports.'+name
        for i,entry in enumerate(d.get('agent_reassessments') or []):
            if entry.get('agent')==name and (entry.get('report') or {}).get('digest')==reports[name]['digest']:
                path=f'agent_reassessments[{i}].report'
        if name=='elmer':
            for i,entry in enumerate(d.get('elmer_assessments') or []):
                if entry.get('digest')==reports[name]['digest']:path=f'elmer_assessments[{i}]'
        return path
    citations=[{'label':f"Dossier v{source['version']}",**context}]+[
        {'label':name+' report','report_digest':reports[name]['digest'],'report_path':report_path(name),**context} for name in cited]
    if decision and topic in ('gaps','progress'):
        citations.append({'label':'Raphael decision','run_id':context['run_id'],'kind':'raphael_decision',
            'key':decision.record_key,'version':decision.version,'digest':decision.digest,'title':'Retained Raphael decision'})
    return {'text':message,'topic':topic,'data':data,'context':context,'newer_version_available':newer,
            'citations':citations,
            'view':'dossier_review' if topic=='review' else None,'provider_calls':0,'operational_writes':0}
