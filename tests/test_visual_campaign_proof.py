"""Retained synthetic visual assessment, source preservation and replay."""
from copy import deepcopy
from hashlib import sha256
import pytest

from app import handoff_demo as demo
from app import visual_campaign_proof as proof
from app.handoff_demo_domain import scope
from app.workflow_db import WorkflowError,get_record_in_transaction
from app.visual_design import visual_assessment,validate_visual_assessment
from tests.test_handoff_demo import Conn,KEY
from tests.test_funnel_acceptance import _new_db


def source(conn):
    result=demo.start_in_transaction(conn,run_key='demo-handoff-one-visual-source',scenario_id='hudson-sky')
    for _ in range(5):result=demo.advance_in_transaction(conn,run_key=result['run_key'],expected_sequence=result['sequence'])
    return demo.inspect_in_transaction(conn,run_key=result['run_key'])


def test_retained_assumptions_campaign_and_reply_preserve_source():
    conn=Conn(_new_db())
    with scope(conn,KEY):
        original=source(conn);before=deepcopy(original)
        result,prepared=proof.build(original)
        manifest=result['manifest']
        assert original==before and manifest['room_ids']==['north-pilot','south-pilot']
        assert manifest['source_engineering_digest']==original['dossier']['dossier']['reports']['onesimus']['digest']
        assert manifest['simeon_reused_png_sha256']==manifest['png_sha256'][0]
        assert sha256(prepared['zip_bytes']).hexdigest()==manifest['zip_sha256']
        rebuilt,again=proof.build(original)
        assert rebuilt==result and again['message_bytes']==prepared['message_bytes']
        assert manifest['concept_count']==3 and manifest['designed_materials']==11
        assert not manifest['calculations_changed'] and not manifest['simeon_delivery_enabled']
        assert all(t['status']=='hypothetical_not_scheduled' for t in manifest['itinerary'])
        html=proof.review_html(original,result)
        assert 'north-pilot' in html and 'south-pilot' in html and 'Finances stay unchanged' in html


def test_linked_proof_is_idempotent_without_new_dossier_versions_or_receipts():
    db=_new_db();conn=Conn(db)
    with scope(conn,KEY):
        original=source(conn);versions=deepcopy(db['versions']);receipts=deepcopy(db['receipts'])
        first=proof.prepare_in_transaction(conn,source_run_key=original['run_key'])
        assert not first['replayed']
        second=proof.prepare_in_transaction(conn,source_run_key=original['run_key'])
        assert second['replayed'] and first['artifact_digest']==second['artifact_digest']
        assert db['versions']==versions and db['receipts']==receipts
        assert demo.inspect_in_transaction(conn,run_key=original['run_key'])==original


def test_malformed_source_and_visual_assessment_cannot_rebind_finances():
    conn=Conn(_new_db())
    with scope(conn,KEY):
        original=source(conn);engineering=original['dossier']['dossier']['reports']['onesimus']
        assessment=visual_assessment(engineering,source_dossier_digest=original['dossier']['digest'])
        damaged=deepcopy(assessment);damaged['source_engineering_digest']='0'*64
        with pytest.raises(WorkflowError):validate_visual_assessment(engineering,damaged,source_dossier_digest=original['dossier']['digest'])
        broken=deepcopy(original);broken['dossier']['dossier']['reports']['michael']['assessments']['bench']['outputs']={}
        with pytest.raises(WorkflowError):proof.build(broken)
        non_demo=deepcopy(original);non_demo['synthetic']=False
        with pytest.raises(WorkflowError):proof.build(non_demo)
