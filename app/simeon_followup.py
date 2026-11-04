"""Explicit dossier-bound cold-lead return; no automatic marketing or identity inference."""
from copy import deepcopy
from uuid import UUID

from app.communications import lock, read, write
from app.workflow import digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, get_run_by_key_in_transaction


def seal(value):
    return {**value,'digest':digest_payload({k:v for k,v in value.items() if k!='digest'})}


def return_in_transaction(conn, *, conversation_id, run_id, dossier_key, expected_digest,
                          operation_id, notes, reason, principal):
    from app.agents.stages import _load
    from app.custody_db import issue_receipt_in_transaction, verify_chain_in_transaction
    from app.dossier_reports import append_reassessment_in_transaction
    from app.linked_reassessments import effective_reports
    from app.uriel import prepare_campaign
    mid=str(UUID(conversation_id)); rid=UUID(str(run_id))
    if (not principal or not isinstance(operation_id,str) or not 1<=len(operation_id)<=100
            or not isinstance(notes,str) or not notes.strip() or len(notes)>8000
            or not isinstance(reason,str) or not reason.strip() or len(reason)>500):
        raise WorkflowError('bounded notes, reason, operation and principal required')
    lock(conn,'direct-mail:'+mid)
    run=get_run_by_key_in_transaction(conn,'direct-mail:'+mid)
    if run is None: raise WorkflowError('established incoming conversation required')
    inbound=read(conn,run.run_id,'inbound'); state=read(conn,run.run_id,'state')
    if state.payload['owner']!='simeon': raise WorkflowError('Simeon-owned conversation required')
    material={'conversation_id':mid,'run_id':str(rid),'dossier_key':dossier_key,
        'expected_digest':expected_digest,'notes':notes,'reason':reason,'principal':principal}
    request_key='followup:'+operation_id
    prior=read(conn,run.run_id,request_key)
    if prior:
        if prior.payload['material_digest']!=digest_payload(material): raise WorkflowError('follow-up operation conflict')
        return {**prior.payload['result'],'replayed':True}
    if __import__('os').getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP','0')=='1':
        raise WorkflowError('follow-up emergency-stopped')
    source,did=_load(conn,rid,dossier_key,'simeon')
    if source.digest!=expected_digest: raise WorkflowError('dossier changed; inspect before binding')
    binding=state.payload.get('dossier_binding')
    wanted={'run_id':str(rid),'dossier_id':str(did),'dossier_key':dossier_key}
    if binding is not None and binding!=wanted: raise WorkflowError('conversation already linked to another dossier')
    content=read(conn,run.run_id,'content')
    if content is None: raise WorkflowError('authenticated conversation content required')
    reply_plan=read(conn,run.run_id,'reply-plan')
    reply_receipt=read(conn,run.run_id,'reply-receipt')
    # All association is an explicit principal decision; sender/subject never
    # establishes ownership or automatically chooses a property.
    report=seal({'agent':'simeon','schema_version':'simeon-conversation-followup-1',
        'source_dossier_digest':source.digest,'conversation_id':mid,
        'inbound_record_digest':inbound.digest,'content_digest':content.digest,
        'notes':notes,'reason':reason,'association_authority':principal,
        'retained_correspondence':{'inbound':deepcopy(inbound.payload),
            'content':deepcopy(content.payload),
            'reply':deepcopy(reply_plan.payload) if reply_plan else None,
            'reply_receipt':deepcopy(reply_receipt.payload) if reply_receipt else None},
        'identity_or_interest_verified':False,'marketing_authorized':False,
        'outcome':'return_for_tailored_campaign_review'})
    previous=effective_reports(source.payload['dossier']).get('simeon') or {}
    reassessment=seal({'agent':'simeon','report':report,'parent_report_digest':previous.get('digest'),
        'source_dossier_digest':source.digest,'job_key':request_key})
    stored,version=append_reassessment_in_transaction(conn,rid,dossier_key,reassessment)
    receipt=issue_receipt_in_transaction(conn,dossier_id=did,version=version.version,kind='return',
        from_agent='simeon',to_agent='uriel',issued_by=principal,
        note='Conversation-linked campaign rework; no contact or consent inferred')
    campaign=prepare_campaign(stored.payload)
    campaign.pop('campaign_id')
    campaign['followup_context']={'conversation_id':mid,'report_digest':report['digest'],
        'notes':notes,'reason':reason,'status':'draft_requires_suppression_and_channel_review'}
    # Quote retained notes as claims rather than treating them as instructions.
    campaign['collateral']['emails'][0]['body'] += '\n\nPrior discussion topics (unverified):\n'+notes
    campaign['campaign_id']='campaign:'+digest_payload(campaign)
    record=write(conn,rid,'conversation-campaign:'+operation_id,campaign)
    checkpoint=get_record_in_transaction(conn,rid,'research_artifact','handoff-state')
    if checkpoint and checkpoint.payload.get('synthetic') is True:
        # Keep the existing demo viewer/controller aligned with actual custody.
        from app.agents.registry import PIPELINE_IDS
        progress=deepcopy(checkpoint.payload)
        progress.update(phase='campaign_rework',index=PIPELINE_IDS.index('uriel'),
            sequence=progress['sequence']+1)
        progress['events'].append({'agent':'simeon','to_agent':'uriel','phase':'campaign_rework',
            'sequence':progress['sequence'],'receipt_id':str(receipt.receipt_id),
            'reason':reason,'report_digest':report['digest']})
        write(conn,rid,'handoff-state',progress,checkpoint)
    new_state={**state.payload,'dossier_binding':wanted,'status':'returned_to_uriel',
        'revision':state.payload['revision']+1}
    write(conn,run.run_id,'state',new_state,state)
    result={'current_holder':'uriel','dossier_id':str(did),'version':version.version,
        'receipt_id':str(receipt.receipt_id),'report_digest':report['digest'],
        'campaign_digest':record.digest,'campaign_id':campaign['campaign_id'],
        'verified_receipts':verify_chain_in_transaction(conn,did),
        'external_actions':0,'marketing_authorized':False,'replayed':False}
    write(conn,run.run_id,request_key,{'material_digest':digest_payload(material),'result':result})
    return result
