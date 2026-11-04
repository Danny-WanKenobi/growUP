from datetime import datetime
from email import policy
from email.parser import BytesParser
from io import BytesIO
from zipfile import ZipFile

import pytest

from app.uriel_campaign import collateral, demo_message, DEMO_RECIPIENT, CONTACT
from app.workflow import digest_payload


def campaign():
    result = {'status':'draft_not_sent', 'collateral':collateral(
        'Fictional review fixture. Conditional operating net: $100/month [Michael test-ref]. No guaranteed returns.')}
    result['campaign_id'] = 'campaign:' + digest_payload(result)
    return result


def test_full_zip_manifest_and_recipient_pinned_mime_with_dated_itinerary():
    result = demo_message(campaign(), start_at=datetime.fromisoformat('2026-11-04T10:00:00-06:00'),
        timezone_name='America/Chicago')
    assert result['recipient'] == DEMO_RECIPIENT and not result['sent'] and not result['scheduled']
    with ZipFile(BytesIO(result['zip_bytes'])) as archive:
        names = archive.namelist()
        assert len([n for n in names if n.startswith('emails/')]) == 4
        assert len([n for n in names if n.startswith('letters/')]) == 3
        assert len([n for n in names if n.startswith('flyers/')]) == 2
        assert {'brochure.txt','business-card.txt','itinerary.json','README.txt'} <= set(names)
        assert archive.read('flyers/flyer-1.txt') != archive.read('flyers/flyer-2.txt')
        for name in names:
            if name not in ('itinerary.json','README.txt'):
                assert CONTACT.encode() in archive.read(name)
                assert b'No guaranteed returns' in archive.read(name) or name == 'business-card.txt'
    stages = result['itinerary']
    assert [s['business_cards'] for s in stages] == [0,0,1,2]
    assert [s['letter'] for s in stages] == [None,1,2,3]
    assert [s['cost_tier'] for s in stages] == [1,2,3,4]
    assert all(s['printing_postage_cost_usd'] is None for s in stages)
    assert [datetime.fromisoformat(s['proposed_at']).day for s in stages] == [4,11,18,25]
    message = BytesParser(policy=policy.default).parsebytes(result['message_bytes'])
    assert str(message['To']) == DEMO_RECIPIENT and str(message['Reply-To']) == CONTACT
    assert list(message.iter_attachments())[0].get_payload(decode=True) == result['zip_bytes']
    assert all(s['proposed_at'] in message.get_body(preferencelist=('plain',)).get_content() for s in stages)


def test_package_refuses_tampering_suppression_and_naive_dates():
    inputs = dict(start_at=datetime.fromisoformat('2026-11-04T10:00:00+00:00'),timezone_name='UTC')
    damaged = campaign(); damaged['status'] = 'changed'
    with pytest.raises(ValueError, match='intact'): demo_message(damaged, **inputs)
    suppressed = campaign(); suppressed['status'] = 'suppressed'
    suppressed['campaign_id'] = 'campaign:' + digest_payload({k:v for k,v in suppressed.items() if k!='campaign_id'})
    with pytest.raises(ValueError, match='suppressed'): demo_message(suppressed, **inputs)
    with pytest.raises(ValueError, match='aware'):
        demo_message(campaign(), start_at=datetime(2026,11,4),timezone_name='UTC')


def test_sabbath_dates_are_shifted_and_zip_is_repeatable():
    inputs = dict(start_at=datetime.fromisoformat('2026-11-06T23:00:00+02:00'),timezone_name='Asia/Jerusalem')
    first = demo_message(campaign(), **inputs)
    assert all(s['sabbath_adjusted'] for s in first['itinerary'])
    assert first['zip_bytes'] == demo_message(campaign(), **inputs)['zip_bytes']
