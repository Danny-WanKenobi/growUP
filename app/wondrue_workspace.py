"""Owner cockpit storage. Chat metadata is separate from immutable business records.

No model/provider calls. Operational execution is limited to reviewed internal
operations; uncertain dispatches are held, never automatically re-executed.
"""
from copy import deepcopy
from uuid import UUID
import json

from app.communications import instant, lock, read, write
from app.workflow import digest_payload
from app.workflow_db import (WorkflowError, create_run_in_transaction,
    get_run_by_key_in_transaction, get_record_in_transaction)

PREFIX = 'wondrue-chat:'
LIBRARY_KINDS = ('dossier_version', 'raphael_decision', 'qualification', 'custody_link')
# Deliberately no acquisition, messaging, inference, contracts or agent dispatch.
REVIEW_ACTIONS = {
    'prepare_owner_brief': 'Append an updated owner brief',
    'prepare_elmer_intake': 'Prepare accounting intake from retained costs',
    'complete_elmer_intake': 'Complete the retained accounting assessment',
    'resolve_owner_exception': 'Record an administrative owner-review decision',
}


def transaction(action, mutate):
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.transactions import run_consequential_transaction
    from app.observability.policy import OperationClass
    event = AgentEvent(agent=AgentId.WONDRUE, run_id='wondrue-workspace',
        event_type='wondrue.workspace.' + action, summary='Owner cockpit history/review',
        details={}, retention_class=RetentionClass.PERMANENT)
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
                                         event=event, mutate=mutate)


def _text(value, maximum, name):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise WorkflowError('bounded ' + name + ' required')
    return value.strip()


def valid_record(kind,payload,digest):
    if kind=='dossier_version':
        if payload.get('digest') is not None and payload['digest']!=digest:return False
        return digest_payload({k:v for k,v in payload.items() if k!='digest'})==digest
    return digest_payload(payload)==digest


def chat(conn, chat_id, create=False):
    chat_id = str(UUID(str(chat_id)))
    key = PREFIX + chat_id
    lock(conn, key)
    run = get_run_by_key_in_transaction(conn, key)
    if run is None and create:
        run = create_run_in_transaction(conn, idempotency_key=key)
        write(conn, run.run_id, 'chat', {'id':chat_id, 'title':'New conversation',
            'created_at':instant().isoformat(), 'updated_at':instant().isoformat(), 'count':0})
    if run is None:
        raise WorkflowError('conversation not found')
    return run, read(conn, run.run_id, 'chat')


def turns(conn, chat_id):
    run, meta = chat(conn, chat_id)
    items = []
    for i in range(1, meta.payload['count'] + 1):
        row = read(conn, run.run_id, 'turn:' + str(i))
        if row is None:
            raise WorkflowError('conversation history incomplete')
        items.append(deepcopy(row.payload))
    return {'conversation':meta.payload, 'turns':items}


def append_turn(conn, chat_id, request_id, text, answer, input_digest=None):
    request_id = str(UUID(str(request_id)))
    text = _text(text, 4000, 'message')
    run, meta = chat(conn, chat_id)
    replay = read(conn, run.run_id, 'request:' + request_id)
    if replay:
        if replay.payload['text'] != text or replay.payload.get('input_digest')!=input_digest:
            raise WorkflowError('message identity conflict')
        return replay.payload
    if meta.payload['count'] >= 200:
        raise WorkflowError('conversation has reached 200 turns; start a new chat')
    value = {'request_id':request_id, 'text':text, 'answer':answer, 'input_digest':input_digest,
             'created_at':instant().isoformat(), 'sequence':meta.payload['count'] + 1}
    if len(json.dumps(value)) > 150000:
        raise WorkflowError('answer too large; inspect the Library record instead')
    write(conn, run.run_id, 'turn:' + str(value['sequence']), value)
    write(conn, run.run_id, 'request:' + request_id, value)
    state = {**meta.payload, 'count':value['sequence'], 'updated_at':value['created_at']}
    if state['count'] == 1:
        state['title'] = ((state.get('context') or {}).get('title','')+' · '+text).strip(' ·')[:70]
    write(conn, run.run_id, 'chat', state, meta)
    return value


def conversations(conn, query='', offset=0):
    with conn.cursor() as cur:
        cur.execute("SELECT r.payload,r.digest FROM workflow_records r JOIN workflow_runs w USING(run_id) "
                    "WHERE w.idempotency_key LIKE %(prefix)s AND r.kind='research_artifact' "
                    "AND r.record_key='chat' AND NOT EXISTS (SELECT 1 FROM workflow_records n "
                    "WHERE n.run_id=r.run_id AND n.kind=r.kind AND n.record_key=r.record_key AND n.version>r.version) "
                    "AND (%(query)s='' OR EXISTS (SELECT 1 FROM workflow_records t WHERE t.run_id=r.run_id "
                    "AND t.kind='research_artifact' AND t.record_key LIKE %(turn)s "
                    "AND strpos(lower(t.payload::text),lower(%(query)s))>0)) "
                    "ORDER BY r.payload->>'updated_at' DESC,r.run_id LIMIT 30 OFFSET %(offset)s",
                    {'prefix':PREFIX+'%', 'turn':'turn:%', 'query':query, 'offset':offset})
        rows = cur.fetchall()
    for row in rows:
        if row['digest'] != digest_payload(row['payload']):
            raise WorkflowError('conversation index integrity failure')
    return {'items':[row['payload'] for row in rows], 'offset':offset, 'has_more':len(rows)==30}


def catalogue(conn, query='', offset=0, tasks=False):
    kinds = ['owner_review_task'] if tasks else list(LIBRARY_KINDS)
    with conn.cursor() as cur:
        cur.execute("SELECT r.run_id,r.kind,r.record_key,r.version,r.digest,r.payload FROM workflow_records r "
                    "WHERE r.kind=ANY(%(kinds)s) AND NOT EXISTS (SELECT 1 FROM workflow_records n "
                    "WHERE n.run_id=r.run_id AND n.kind=r.kind AND n.record_key=r.record_key AND n.version>r.version) "
                    "AND (%(query)s='' OR strpos(lower(r.payload::text || r.record_key),lower(%(query)s))>0) "
                    "ORDER BY r.run_id,r.kind,r.record_key LIMIT 30 OFFSET %(offset)s",
                    {'kinds':kinds,'query':query,'offset':offset})
        rows = cur.fetchall()
    items = []
    for row in rows:
        if not valid_record(row['kind'],row['payload'],row['digest']):
            # A quarantined row must not make every other retained record invisible.
            # Never expose its claims or permit an action based on its payload.
            items.append({'run_id':str(row['run_id']),'kind':row['kind'],'key':row['record_key'],
                          'version':row['version'],'digest':row['digest'],'title':'Record needs integrity review',
                          'status':'integrity_failed','note':'Claims quarantined. Inspection and operational proposals are refused.'})
            continue
        p = row['payload']; d = p.get('dossier') or p
        if not isinstance(d,dict):d={}
        items.append({'run_id':str(row['run_id']), 'kind':row['kind'], 'key':row['record_key'],
                      'version':row['version'], 'digest':row['digest'],
                      'title':str(d.get('name') or d.get('street_address') or p.get('requirement') or row['record_key'])[:180],
                      'status':'retained_task' if tasks else str(p.get('decision') or p.get('status') or p.get('outcome') or 'retained'),
                      'note':'Retained task; inspect linked dossier for current resolution.' if tasks else 'Retained evidence; not a new verification.'})
    return {'items':items,'offset':offset,'has_more':len(rows)==30}


def record(conn, run_id, kind, key, version=None):
    if kind not in (*LIBRARY_KINDS, 'owner_review_task'):
        raise WorkflowError('record type is not in the retained Library')
    row = get_record_in_transaction(conn, UUID(str(run_id)), kind, _text(key, 500, 'record key'), version=version)
    if row is None or not valid_record(kind,row.payload,row.digest):
        raise WorkflowError('retained record missing or integrity check failed')
    return {'run_id':str(row.run_id), 'kind':kind, 'key':key, 'version':row.version,
            'digest':row.digest, 'data':row.payload}


def intent_for_review(raw):
    from app.model_adapter import validate_intent
    value = validate_intent(raw)
    if value['intent'] not in REVIEW_ACTIONS:
        raise WorkflowError('This operation is not enabled in the cockpit review executor; no action taken.')
    args = value['args']
    allowed = {'run_id','dossier_key','expected_digest','operation_id'}
    if value['intent'] == 'resolve_owner_exception':
        allowed.add('decision_json')
    if set(args) - allowed:
        raise WorkflowError('unsupported review arguments')
    args['run_id'] = str(UUID(str(args.get('run_id'))))
    args['operation_id'] = str(UUID(str(args.get('operation_id'))))
    args['dossier_key'] = _text(args.get('dossier_key'), 500, 'dossier key')
    digest = args.get('expected_digest', '')
    if not isinstance(digest, str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
        raise WorkflowError('exact retained dossier digest required')
    return value


def propose(conn, request_id, raw, note):
    request_id = str(UUID(str(request_id)))
    value = intent_for_review(raw)
    source = record(conn, value['args']['run_id'], 'dossier_version', value['args']['dossier_key'])
    if source['digest'] != value['args']['expected_digest']:
        raise WorkflowError('dossier changed; inspect again')
    note = _text(note, 1000, 'review reason')
    lock(conn, 'wondrue-review:' + request_id)
    run = get_run_by_key_in_transaction(conn, 'wondrue-review:' + request_id)
    if run:
        existing = read(conn, run.run_id, 'review')
        if existing.payload['intent'] != value or existing.payload['reason'] != note:
            raise WorkflowError('review identity conflict')
        return existing.payload
    run = create_run_in_transaction(conn, idempotency_key='wondrue-review:' + request_id)
    state = {'id':request_id, 'intent':value, 'reason':note, 'status':'pending',
             'created_at':instant().isoformat(), 'source_version':source['version'],
             'source_digest':source['digest'], 'choices':['approve','reject','defer'],
             'effects':REVIEW_ACTIONS[value['intent']], 'external_actions':0,
             'provider_calls':0, 'grants_property_authorization':False}
    write(conn, run.run_id, 'review', state)
    return state


def review_list(conn, offset=0):
    with conn.cursor() as cur:
        cur.execute("SELECT r.payload,r.digest FROM workflow_records r JOIN workflow_runs w USING(run_id) "
                    "WHERE w.idempotency_key LIKE %(prefix)s AND r.kind='research_artifact' AND r.record_key='review' "
                    "AND NOT EXISTS (SELECT 1 FROM workflow_records n WHERE n.run_id=r.run_id AND n.kind=r.kind "
                    "AND n.record_key=r.record_key AND n.version>r.version) ORDER BY r.payload->>'created_at' DESC,r.run_id "
                    "LIMIT 30 OFFSET %(offset)s", {'prefix':'wondrue-review:%', 'offset':offset})
        rows=cur.fetchall()
    if any(row['digest'] != digest_payload(row['payload']) for row in rows):
        raise WorkflowError('review integrity failure')
    return {'items':[dict(row['payload'], review_digest=row['digest']) for row in rows], 'has_more':len(rows)==30}


def decide(conn, request_id, expected_digest, choice, note):
    if choice not in ('approve','reject','defer'):
        raise WorkflowError('explicit preplanned review choice required')
    request_id=str(UUID(str(request_id)))
    lock(conn, 'wondrue-review:' + request_id)
    run=get_run_by_key_in_transaction(conn, 'wondrue-review:' + request_id)
    if run is None:raise WorkflowError('review not found')
    row=read(conn, run.run_id, 'review')
    if row.digest!=expected_digest:raise WorkflowError('review changed; refresh before deciding')
    state=deepcopy(row.payload)
    if state['status'] not in ('pending','deferred'):
        raise WorkflowError('review is already decided; uncertain dispatches require reconciliation, never retry')
    state.update(decision=choice, decision_note=_text(note,1000,'decision note'),
                 decided_at=instant().isoformat(), principal='wondrue-operator')
    if choice=='approve':
        source=record(conn,state['intent']['args']['run_id'],'dossier_version',state['intent']['args']['dossier_key'])
        if source['digest']!=state['source_digest'] or source['version']!=state['source_version']:
            raise WorkflowError('retained evidence changed; create a new review')
        # Commit this claim BEFORE calling an existing separately audited service.
        state['status']='dispatch_claimed'
    else:state['status']='rejected' if choice=='reject' else 'deferred'
    write(conn,run.run_id,'review',state,row)
    return state


def finish(conn, request_id, result=None):
    lock(conn,'wondrue-review:'+request_id)
    run=get_run_by_key_in_transaction(conn,'wondrue-review:'+request_id)
    row=read(conn,run.run_id,'review')
    if row.payload['status']!='dispatch_claimed':raise WorkflowError('dispatch claim missing')
    state={**row.payload,'status':'completed' if result is not None else 'needs_reconciliation',
           'finished_at':instant().isoformat(), 'result':result,
           'recovery':'Inspect operation_id in the existing agent audit before any further action.'}
    write(conn,run.run_id,'review',state,row)
    return state
