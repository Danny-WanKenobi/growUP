"""Bounded, durable internal processing of one canonical dossier; no transports."""
from copy import deepcopy
import argparse
import json
import os
from uuid import UUID

from app.workflow import digest_payload
from app.workflow_db import WorkflowError, get_record_in_transaction, put_record_in_transaction
from app.agents.stages import _load, build_report
from app.custody_db import current_custody_in_transaction, issue_receipt_in_transaction
from app.dossier_reports import append_agent_report_in_transaction

ROUTE = ('gabriel', 'raphael', 'onesimus', 'michael', 'uriel', 'priscilla', 'nicole', 'theresia_keys', 'simeon', 'solomon')
VERSION = 'dossier-coordinator-4'
THIRD_VERSION = 'dossier-coordinator-3'
PREVIOUS_VERSION = 'dossier-coordinator-2'
LEGACY_VERSION = 'dossier-coordinator-1'


def _sealed(value):
    result = deepcopy(value)
    result['digest'] = digest_payload(result)
    return result


def _build(agent, payload):
    """Reuse stage policy/calculators; all inputs come from the retained dossier."""
    from app.agents.raphael.inspection import build_inspection_report
    from app.agents.raphael.service import evaluate_dossier
    from app.agents.onesimus.service import evaluate_scenario
    from app.agents.onesimus.crops import crop_profile
    from app.agents.onesimus.report import build_engineering_report
    from app.mvp import _site_inputs
    from app.michael import assess, financial_inputs_from_raw
    from app.linked_reassessments import effective_dossier
    dossier = effective_dossier(payload['dossier'])
    reports = dossier.get('reports') or {}
    inputs = dossier.get('assessment_inputs') or {}
    if not isinstance(inputs, dict):
        raise WorkflowError('assessment_inputs must be a mapping')
    if agent == 'raphael':
        decision = evaluate_dossier(payload)
        report = build_inspection_report(payload, site_inputs=inputs.get('site') or {}, decision=decision)
        eligible = decision['outcome'] == 'authorized'
        reasons = [] if eligible else decision['basis']
    elif agent == 'onesimus':
        inspection = reports.get('raphael') or {}
        if inspection.get('outcome') != 'authorized':
            raise WorkflowError('authorized Raphael report required')
        from app.agents.onesimus.engineering import CROP_UNITS
        from app.agents.onesimus.service import ClassifiedInput
        crop = crop_profile()
        overrides = inputs.get('crop') or {}
        if not isinstance(overrides, dict) or set(overrides) - set(CROP_UNITS):
            raise WorkflowError('unsupported crop input')
        for name, raw in overrides.items():
            if not isinstance(raw, dict):
                raise WorkflowError('crop input requires classification, unit and provenance')
            item = ClassifiedInput.model_validate({**raw, 'name':name})
            if item.classification == 'verified':
                item = item.model_copy(update={'classification':'assumed'})
            crop[name] = item
        scenarios = {}
        for config in ('bench', 'rack3'):
            scenario = evaluate_scenario(config, crop, _site_inputs(inspection.get('physical_inputs') or {}))
            scenario['dossier_digest'] = payload['digest']
            scenarios[config] = scenario
        report = build_engineering_report(scenarios, inspection=inspection,
                                         options=inspection.get('engineering_options') or {})
        if inputs.get('rooms') is not None:
            from app.agents.onesimus.portfolio import plan_rooms
            report['room_portfolio']=plan_rooms(payload,inputs['rooms'],crop=crop)
            if not report['room_portfolio']['complete']:
                report['missing']=list(dict.fromkeys(report['missing']+report['room_portfolio']['missing']));report['status']='needs_inputs'
            report.pop('digest',None);report=_sealed(report)
        eligible = report['status'] == 'concept_ready'
        if report.get('room_portfolio') and not report['room_portfolio']['complete']: eligible=False
        reasons = [] if eligible else report.get('missing') or ['no plausible preliminary layout']
    elif agent == 'michael':
        engineering = reports.get('onesimus') or {}
        from app.michael import planning_cases
        cases = {name: planning_cases(name, item['scenario'], financial_inputs_from_raw(
            inputs.get('financial') or {}, scenario=name))
            for name, item in (engineering.get('alternatives') or {}).items()}
        assessments = {name: case.get('cases',{}).get('base') or {
            'complete':False,'verdict':'insufficient_evidence','missing':case['missing'],'outputs':{}}
            for name, case in cases.items()}
        eligible = any(a.get('complete') and a.get('verdict') == 'favorable_under_assumptions'
                       for a in assessments.values())
        reasons = [] if eligible else ['no complete financially favorable scenario under stated assumptions']
        report = {'schema_version': 'michael-finance-report-1', 'assessments': assessments,
                  'source_engineering_digest': engineering.get('digest'), 'missing': reasons, 'planning_cases':cases}
        if engineering.get('room_portfolio'):
            from app.agents.onesimus.portfolio import assess_room_finance
            report['room_finance']=assess_room_finance(engineering['room_portfolio'],inputs.get('room_financial'))
            if not report['room_finance'].get('favorable_under_assumptions'):
                eligible=False;reasons.extend(report['room_finance']['missing'] or ['room portfolio is not favorable under supplied assumptions'])
    else:
        report = build_report(agent, payload)
        eligible = not report['missing']
        reasons = report['missing']
    if agent=='onesimus':
        from app.agents.onesimus.portfolio import engineering_sensitivities
        report['sensitivity_cases']=engineering_sensitivities(report)
    if agent=='michael':
        from app.michael import operating_stress_cases
        report['operating_stress_cases']={name:operating_stress_cases(name,item.get('scenario') or item,financial_inputs_from_raw(inputs.get('financial') or {},scenario=name)) for name,item in (reports.get('onesimus',{}).get('alternatives') or {}).items()}
    report = {k: v for k, v in report.items() if k != 'digest'}
    report.update(agent=agent, source_dossier_digest=payload['digest'])
    return _sealed(report), eligible, list(reasons)


def step_in_transaction(conn, *, run_id, dossier_key, principal, expected_sequence, action='step'):
    """One locked stage, report/snapshot, receipt, checkpoint and audit commit together.

    Caller owns transaction. Audit wrapper is mandatory for external entrypoints.
    Repeated sequence replays the stored result without advancing another stage.
    """
    from app.mvp import _require_enabled
    _require_enabled()
    if not principal or not principal.strip():
        raise WorkflowError('authenticated coordinator principal required')
    if action not in ('step', 'stop', 'resume'):
        raise WorkflowError('unknown coordinator action')
    if isinstance(expected_sequence, bool) or not isinstance(expected_sequence, int) or expected_sequence < 0:
        raise WorkflowError('expected_sequence must be a non-negative integer')
    run_id = UUID(str(run_id))
    initial = get_record_in_transaction(conn, run_id, 'dossier_version', dossier_key)
    if initial is None:
        raise WorkflowError('stored dossier not found')
    from app.custody import dossier_id_for_property
    did = dossier_id_for_property(initial.payload.get('property_id'))
    from app.custody_db import lock_dossier_in_transaction
    lock_dossier_in_transaction(conn, did)
    source, did = _load(conn, run_id, dossier_key)
    key = 'coordinator:' + dossier_key
    record = get_record_in_transaction(conn, run_id, 'research_artifact', key)
    if record and record.digest != digest_payload(record.payload):
        raise WorkflowError('coordinator checkpoint integrity failure')
    state = deepcopy(record.payload) if record else {'version': VERSION, 'sequence': 0, 'status': 'running',
        'principal': principal, 'dossier_id': str(did), 'dossier_key': dossier_key}
    if state['principal'] != principal or state['dossier_id'] != str(did) or state['version'] not in (LEGACY_VERSION, PREVIOUS_VERSION, THIRD_VERSION, VERSION):
        raise WorkflowError('coordinator identity/configuration mismatch')
    if expected_sequence < state['sequence']:
        return {**state, 'replayed': True}
    if expected_sequence != state['sequence']:
        raise WorkflowError('sequence ahead of coordinator checkpoint')
    holder = current_custody_in_transaction(conn, did)
    agent = holder['holder'] if holder else 'gabriel'
    if state['version'] in (LEGACY_VERSION, PREVIOUS_VERSION, THIRD_VERSION) and action != 'stop':
        if (action != 'resume' or state['status'] not in ('running', 'stopped', 'delivered_to_nicole', 'delivered_to_theresia_keys', 'delivered_to_simeon')
                or agent not in ROUTE[1:] or state.get('dossier_digest', source.digest) != source.digest):
            raise WorkflowError('legacy coordinator requires explicit resume from its unchanged held dossier')
        # Append the new route checkpoint only on explicit operator resume. The
        # existing checkpoint, reports and custody history are never rewritten.
        state.update(version=VERSION, status='stopped', upgraded_from=state['version'])
    if action == 'stop':
        state['status'] = 'stopped'
    elif action == 'resume':
        if state['status'] != 'stopped':
            raise WorkflowError('only stopped processing can resume')
        state['status'] = 'running'
    elif state['status'] != 'running':
        return {**state, 'replayed': True}
    elif os.getenv('DOSSIER_COORDINATOR_EMERGENCY_STOP', '0') == '1':
        state['status'] = 'stopped'
    else:
        if agent not in ROUTE[1:]:
            raise WorkflowError('coordinator requires current custody from Raphael through Solomon')
        source, _ = _load(conn, run_id, dossier_key, agent)
        reports = source.payload.get('dossier', {}).get('reports') or {}
        if agent in reports:
            # A prior independently appended report needs explicit reconciliation;
            # never silently use its outcome as current processing authority.
            reason = 'existing stage report requires explicit reconciliation'
            task = {'agent': agent, 'dossier_id': str(did), 'property_id': str(source.payload['property_id']),
                    'requirement': reason, 'status': 'pending', 'authority': 'human_owner',
                    'grants_permission': False, 'source_dossier_digest': source.digest}
            put_record_in_transaction(conn, run_id, kind='owner_review_task',
                record_key=key+':exception:'+source.digest, version=1, payload=task, digest=digest_payload(task))
            state.update(status='owner_review', reason=reason)
        else:
            try:
                report, eligible, reasons = _build(agent, source.payload)
                if agent == 'uriel' and eligible:
                    from app.uriel import prepare_package_in_transaction
                    incoming = current_custody_in_transaction(conn, did)
                    package = prepare_package_in_transaction(conn, dossier_id=did,
                        version=incoming['version'], receipt_id=incoming['receipt_id'], prepared_by=principal,
                        dossier=source.payload['dossier'],
                        engineering=source.payload['dossier']['reports']['onesimus'],
                        economics=source.payload['dossier']['reports']['michael'],
                        campaign=report['campaign'])
                    report = _sealed({**{k:v for k,v in report.items() if k!='digest'},'package_entry':package['entry_id']})
            except ValueError:
                task = {'agent': agent, 'dossier_id': str(did),
                    'property_id': str(source.payload['property_id']),
                    'requirement': 'invalid stage inputs require owner review', 'status': 'pending',
                    'authority': 'human_owner', 'grants_permission': False,
                    'source_dossier_digest': source.digest}
                put_record_in_transaction(conn, run_id, kind='owner_review_task',
                    record_key=key+':exception:'+source.digest, version=1, payload=task, digest=digest_payload(task))
                state.update(status='owner_review', reason=task['requirement'])
                report = None
            if report is not None:
                stored, version = append_agent_report_in_transaction(conn, run_id, dossier_key, agent=agent, report=report)
                for issue in report.get('owner_review_requests') or []:
                    task = {**issue,'agent':agent,'dossier_id':str(did),'property_id':str(source.payload['property_id']),
                        'source_dossier_digest':source.digest,'report_digest':report['digest'],'grants_permission':False}
                    put_record_in_transaction(conn,run_id,kind='owner_review_task',
                        record_key='owner:'+digest_payload(task),version=1,payload=task,digest=digest_payload(task))
                reason = '; '.join(str(r) for r in reasons)[:450] or 'preliminary stage requirements satisfied'
                if agent == 'solomon' and eligible:
                    # Hold the enhanced, snapshotted dossier for planning review.
                    # Do not fabricate closing, release to construction, or advance to Elmer.
                    state.update(status='solomon_planning_review',agent=agent,current_holder=agent,
                        reason='Preconstruction planning only; closing and construction authority remain unresolved.',
                        dossier_digest=stored.digest,custody_version=version.version)
                else:
                    target = ROUTE[ROUTE.index(agent)+1] if eligible else ('michael' if agent == 'priscilla' else ROUTE[ROUTE.index(agent)-1])
                    if agent == 'simeon' and eligible:
                        reason='Conditional preconstruction-planning handoff only; deal is not closed and construction is not authorized.'
                    receipt = issue_receipt_in_transaction(conn, dossier_id=did, version=version.version,
                        kind='forward' if eligible else 'return', from_agent=agent, to_agent=target,
                        issued_by=principal, note=reason)
                    state.update(status='running' if eligible else 'returned',agent=agent,current_holder=target,
                        reason=reason,receipt_id=str(receipt.receipt_id),dossier_digest=stored.digest,custody_version=version.version)
    state['sequence'] += 1
    put_record_in_transaction(conn, run_id, kind='research_artifact', record_key=key,
        version=record.version+1 if record else 1, payload=state, digest=digest_payload(state))
    return state


def process(*, principal, **inputs):
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    event = AgentEvent(agent=AgentId.WONDRUE, run_id=str(inputs['run_id']),
        event_type='workflow.coordinator_step', summary='Internal dossier coordination; no external execution',
        details={'principal': principal, 'action': inputs.get('action', 'step'),
                 'dossier_key': inputs['dossier_key'], 'expected_sequence': inputs['expected_sequence']},
        retention_class=RetentionClass.PERMANENT)
    def mutate(conn):
        try:
            return step_in_transaction(conn, principal=principal, **inputs)
        except BaseException:
            # KeyboardInterrupt/SystemExit also release the transaction and locks.
            try:
                conn.rollback()
            finally:
                conn.close()
            raise
    return run_consequential_transaction(operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=event, mutate=mutate)


def run_bounded(*, principal, run_id, dossier_key, expected_sequence, max_steps=5):
    """Commit at most five stages. Restart from the last acknowledged sequence."""
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 5:
        raise WorkflowError('max_steps must be between 1 and 5')
    result = None
    for _ in range(max_steps):
        result = process(principal=principal, run_id=run_id, dossier_key=dossier_key,
                         expected_sequence=expected_sequence)
        expected_sequence = result['sequence']
        if result['status'] != 'running' or result.get('replayed'):
            break
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('step', 'stop', 'resume', 'run'))
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--dossier-key', required=True)
    parser.add_argument('--principal', required=True)
    parser.add_argument('--expected-sequence', required=True, type=int)
    args = vars(parser.parse_args())
    action = args.pop('action')
    if action == 'run':
        result = run_bounded(**args)
    else:
        result = process(action=action, **args)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
