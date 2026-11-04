from copy import deepcopy
from uuid import uuid4
import pytest
from app import handoff_demo as demo, mailbox_routing
from app.simeon_followup import return_in_transaction
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError, get_record_in_transaction
from tests.test_handoff_demo import Conn, KEY
from tests.test_funnel_acceptance import _new_db


def test_cold_return_preserves_reports_and_custody_and_replays_once(monkeypatch):
    monkeypatch.setenv('MVP_WORKFLOW_ENABLED','1')
    monkeypatch.setenv('RESEND_RECEIVING_DOMAIN','growup.securecodeusa.com')
    conn=Conn(_new_db()); key='demo-handoff-cold-'+uuid4().hex
    with scope(conn,KEY):
        view=demo.start_in_transaction(conn,run_key=key,scenario_id='hudson-sky')
        for _ in range(15):
            if view['current_holder']=='simeon': break
            view=demo.advance_in_transaction(conn,run_key=key,expected_sequence=view['sequence'])
        assert view['current_holder']=='simeon'
        run=demo._get(conn,key)
        source=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
        reports=deepcopy(source.payload['dossier']['reports'])
        mid=str(uuid4())
        detail={'id':mid,'from':'werenotrandom@protonmail.com','to':['simeon@growup.securecodeusa.com'],
            'subject':'Fictional review','text':'Please clarify assumptions.','message_id':'<fixture@protonmail.com>'}
        mailbox_routing.receive(conn,metadata={**detail,'email_id':mid},event_id='test',body_digest='a',authentication='resend_authenticated_api')
        mailbox_routing.retain_content(conn,conversation_id=mid,detail=detail,response_digest='b')
        args={'conversation_id':mid,'run_id':run.run_id,'dossier_key':'main','expected_digest':source.digest,
            'operation_id':'cold-return','notes':'Simulated cold-lead scenario; clarify uncertainty in lighting costs.',
            'reason':'Owner-requested synthetic scenario, not actual elapsed lead inactivity','principal':'test-owner'}
        result=return_in_transaction(conn,**args)
        assert result['current_holder']=='uriel' and not result['marketing_authorized']
        stored=get_record_in_transaction(conn,run.run_id,'dossier_version','main')
        assert stored.payload['dossier']['reports']==reports
        assert stored.payload['dossier']['agent_reassessments'][-1]['report']['conversation_id']==mid
        assert result['verified_receipts']>view['verified_receipts']
        before=deepcopy(conn._db)
        assert return_in_transaction(conn,**args)['replayed'] and conn._db==before
        with pytest.raises(WorkflowError,match='conflict'):
            return_in_transaction(conn,**{**args,'notes':'changed retry'})
