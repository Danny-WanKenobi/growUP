"""Critical image, artwork and lineage boundaries. No provider requests or sends."""
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

from PIL import Image
import pytest

from app import dossier_media as media
from app.agents.onesimus.report import build_engineering_report
from app.visual_design import (concepts, validate_concepts, campaign_visuals, render_material,
                               reuse_visuals, canva_brief, raster_concept, collateral_artwork)
from app.workflow import digest_payload
from app.workflow_db import WorkflowError
from app.uriel_campaign import collateral, demo_message
from tests.test_engineering_reports import inspection, scenario


def image_bytes():
    photo=Image.new('RGB',(2200,1400),'#547c63');out=BytesIO()
    exif=Image.Exif();exif[270]='untrusted source metadata'
    photo.save(out,'JPEG',exif=exif);return out.getvalue()


@pytest.fixture
def private_media(monkeypatch,tmp_path):
    monkeypatch.setenv('GROWUP_MEDIA_DIR',str(tmp_path))
    monkeypatch.setenv('DATABASE_NAME','growup_mvp_gate')
    monkeypatch.delenv('GROWUP_IMAGE_PUBLICATION_SHA256',raising=False)
    return tmp_path


def retain():
    return media.retain_photo(image_bytes(),page_url='https://example.org/property',
                              image_url='https://images.example.org/property.jpg',retrieved_at='2026-11-04T10:00:00Z')


def engineering():
    source=inspection();source['digest']=digest_payload(source)
    return build_engineering_report({'bench':scenario()},inspection=source)


def test_compression_strips_metadata_archive_is_repeatable_and_database_isolation(private_media,monkeypatch):
    entry=retain();compressed=media.read_photo(entry)
    with Image.open(BytesIO(compressed)) as image:
        assert image.width<=1600 and image.height<=1600 and not image.getexif()
    assert entry['bytes']<entry['input_bytes'] and not entry['outreach_approved']
    archive=media.archive_photos([entry]);assert archive==media.archive_photos([entry])
    body=(media.root()/'archives'/(archive['archive_sha256']+'.zip')).read_bytes()
    assert sha256(body).hexdigest()==archive['archive_sha256']
    with ZipFile(BytesIO(body)) as z:
        assert z.read('images/'+entry['sha256']+'.webp')==compressed
        assert not json.loads(z.read('manifest.json'))['property_claims_authenticated']
    monkeypatch.setenv('DATABASE_NAME','growup_handoff_demo')
    with pytest.raises(FileNotFoundError):media.read_photo(entry)
    with pytest.raises(WorkflowError):media.read_photo({**entry,'sha256':'../../etc/passwd'})


def test_malicious_oversized_and_corrupt_images_refuse(private_media,monkeypatch):
    with pytest.raises(WorkflowError):media.compress_photo(b'<svg onload="bad()"/>')
    monkeypatch.setattr(media,'MAX_PIXELS',100)
    with pytest.raises(WorkflowError,match='pixel'):media.compress_photo(image_bytes())
    monkeypatch.setattr(media,'MAX_PIXELS',24_000_000)
    entry=retain();(media.root()/'images'/(entry['sha256']+'.webp')).write_bytes(b'changed')
    with pytest.raises(WorkflowError,match='digest mismatch'):media.read_photo(entry)


def test_three_views_bind_calculations_and_cannot_confer_verification():
    report=engineering();drawings=validate_concepts(report)
    assert len(drawings)==3 and len({a['sha256'] for a in drawings})==3
    assert all(a['calculation_basis_digest']==digest_payload(report['alternatives']) for a in drawings)
    assert all('ARTISTIC CONCEPT' in a['svg'] and not a['capacity_or_permission_verified'] for a in drawings)
    assert all(Image.open(BytesIO(raster_concept(d))).size==(1200,760) for d in drawings)
    damaged=deepcopy(report);damaged['alternatives']['bench']['scenario']['outputs']['sites']=999
    with pytest.raises(WorkflowError,match='three intact'):validate_concepts(damaged)
    missing=deepcopy(report);missing['conceptualizations']=missing['conceptualizations'][:2]
    with pytest.raises(WorkflowError):validate_concepts(missing)
    with pytest.raises(WorkflowError):canva_brief('simeon',dossier_digest='a'*64,package=drawings)


def test_photos_are_internal_until_exact_operator_approval_and_text_is_escaped(private_media,monkeypatch):
    entry=retain();dossier={'media':{'images':[entry]},'synthetic':True}
    visual=campaign_visuals(dossier,engineering(),'<script>do not execute</script>')
    assert not visual['source_photos'] and visual['withheld_source_photos']
    monkeypatch.setenv('GROWUP_IMAGE_PUBLICATION_SHA256',entry['sha256'])
    visual=campaign_visuals(dossier,engineering(),'Conditional financial copy')
    assert len(visual['source_photos'])==1
    html=render_material('Property <unsafe>','<script>do not execute</script>',visual)
    assert '<script>' not in html and '&lt;script&gt;' in html and 'SYNTHETIC DEMONSTRATION' in html


def test_illustrated_full_package_schedule_mime_and_simeon_reuse(tmp_path):
    text='Fictional Hudson Lantern Tower. Preliminary passive Kratky concept. No guaranteed returns.'
    visual=campaign_visuals({'synthetic':True},engineering(),text)
    campaign={'status':'draft_not_sent','data_provenance':'synthetic','collateral':collateral(text),'visual_package':visual}
    campaign['artwork']=collateral_artwork(campaign['collateral'],visual)
    assert len(campaign['artwork'])==11
    campaign['campaign_id']='campaign:'+digest_payload(campaign)
    args={'start_at':datetime.fromisoformat('2026-11-05T10:00:00-06:00'),'timezone_name':'America/Chicago'}
    prepared=demo_message(campaign,**args)
    assert prepared['provider_calls']==0 and not prepared['sent']
    with ZipFile(BytesIO(prepared['zip_bytes'])) as archive:
        names=archive.namelist()
        assert len([n for n in names if n.startswith('artwork/emails/')])==4
        assert len([n for n in names if n.startswith('artwork/letters/')])==3
        assert len([n for n in names if n.startswith('artwork/flyers/')])==2
        assert 'artwork/brochure.html' in names and 'artwork/business_card.html' in names
        assert len([n for n in names if n.endswith('.png')])==3
        assert all('SYNTHETIC DEMONSTRATION' in archive.read(n).decode() for n in names if n.endswith('.html'))
        # A private ignored preview for browser QA, never a public deployment.
        preview=Path('tmp/outreach-visual-review');preview.mkdir(parents=True,exist_ok=True)
        (preview/'growup-illustrated-campaign-review.zip').write_bytes(prepared['zip_bytes'])
        for name in names:
            dest=preview/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(archive.read(name))
    assert [t['proposed_at'][:10] for t in prepared['itinerary']]==['2026-11-05','2026-11-12','2026-11-19','2026-11-26']
    assert b'Content-ID: <growup-concept>' in prepared['message_bytes']
    package={'entry_id':str(uuid4()),'payload':{'campaign':campaign}}
    result=reuse_visuals(package,actor='simeon',text='My own reply.')
    assert result['concept_sha256']==[c['sha256'] for c in visual['concepts']]
    assert not result['design_requested'] and result['provider_calls']==0


def test_ordinary_uriel_preparation_persists_all_eleven_designs(monkeypatch):
    from app import handoff_demo as demo
    from app.handoff_demo_domain import scope
    from tests.test_handoff_demo import Conn,KEY
    from tests.test_funnel_acceptance import _new_db
    from app.workflow_db import get_record_in_transaction
    conn=Conn(_new_db())
    with scope(conn,KEY):
        demo.start_in_transaction(conn,run_key='demo-handoff-artwork')
        for sequence in range(5):demo.advance_in_transaction(conn,run_key='demo-handoff-artwork',expected_sequence=sequence)
        run=demo._get(conn,'demo-handoff-artwork')
        dossier=get_record_in_transaction(conn,run.run_id,'dossier_version','main').payload['dossier']
        campaign=dossier['reports']['uriel']['campaign']
        assert len(campaign['artwork'])==11 and not campaign['execution']['sent']
        assert all('ARTISTIC CONCEPT' in html or 'Simeon' in html for html in campaign['artwork'].values())


def test_active_svg_and_archive_traversal_cannot_enter_a_campaign():
    visual=campaign_visuals({'synthetic':True},engineering(),'Conditional copy')
    assets=collateral('Conditional copy')
    for damage in ('script','path','tiers'):
        bad=deepcopy(visual);drawing=bad['concepts'][0]
        if damage=='script':
            drawing['svg']=drawing['svg'].replace('</svg>','<script>alert(1)</script></svg>')
            drawing['sha256']=sha256(drawing['svg'].encode()).hexdigest()
        elif damage=='path':drawing['id']='../../outside'
        else:drawing['depicted_tiers']=1_000_000_000
        with pytest.raises(WorkflowError):collateral_artwork(assets,bad)


@pytest.mark.skipif(os.getenv('GROWUP_PG_INTEGRATION')!='1',reason='Dedicated PostgreSQL gate required')
def test_pg_image_claim_survives_interruption_and_dossier_media_is_bound(private_media,monkeypatch):
    from tests.test_dcad_admission import _require_gate_database
    from app.properties_db import get_connection
    from app.workflow_db import create_run, get_run_in_transaction, put_record_in_transaction, get_record_in_transaction
    from app.agents.gabriel.dossier import DossierSet,PropertyDossier
    from app.agents.gabriel.persist import persist_dossier_versions
    _require_gate_database()
    monkeypatch.setenv('GABRIEL_IMAGE_CACHE_ENABLED','1');monkeypatch.setenv('GABRIEL_IMAGE_HOSTS','images.example.org')
    resolver=lambda *a,**kw:[(2,1,6,'',('93.184.216.34',443))]
    run=create_run(idempotency_key='pg-image-interruption-'+uuid4().hex)
    result={'url':'https://example.org/property','image_url':'https://images.example.org/property.jpg'}
    calls=[]
    def interrupted(*a,**kw):
        with get_connection() as c:assert get_run_in_transaction(c,run.run_id).usage['image_attempts']==1
        calls.append(1);raise KeyboardInterrupt('test after committed claim')
    with pytest.raises(KeyboardInterrupt):media.collect_search_images(run.run_id,[result],transport=interrupted,resolver=resolver)
    media.collect_search_images(run.run_id,[result],transport=interrupted,resolver=resolver)
    assert len(calls)==1 and not result.get('images')
    successful=create_run(idempotency_key='pg-image-retention-'+uuid4().hex)
    media.collect_search_images(successful.run_id,[result],transport=lambda *a,**kw:image_bytes(),resolver=resolver)
    assert result['images']
    with get_connection() as conn:
        payload={'results':[result]}
        put_record_in_transaction(conn,successful.run_id,kind='research_artifact',record_key='search',version=1,payload=payload,digest=digest_payload(payload))
        dossier=PropertyDossier(key='addr:fictional',sources=[result['url']])
        persist_dossier_versions(conn,successful.run_id,DossierSet(dossiers=[dossier]),None,['search'])
        stored=get_record_in_transaction(conn,successful.run_id,'dossier_version','addr:fictional')
        assert stored.payload['dossier']['media']['images']==result['images']
        conn.commit()
