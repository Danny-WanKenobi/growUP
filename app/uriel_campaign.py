"""Four-burst campaign collateral and a recipient-pinned demo MIME package.

Pure preparation only: no provider client, delivery, printing or scheduling.
"""
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from io import BytesIO
import json
from zipfile import ZipFile, ZIP_DEFLATED, ZipInfo
from zoneinfo import ZoneInfo

from app.workflow import digest_payload

DEMO_RECIPIENT = 'werenotrandom@protonmail.com'
SENDER = 'uriel@growup.securecodeusa.com'
CONTACT = 'simeon@growup.securecodeusa.com'


def collateral(introduction):
    """All materials retain conditional claims and route humans to Simeon."""
    contact = '\nQuestions or interest? Contact Simeon: ' + CONTACT
    stages = (
        ('An opportunity to explore', 'We invite you to discuss the preliminary concept below.'),
        ('A closer look at the possibilities', 'Following up with a written introduction for your consideration.'),
        ('Compare the preliminary scenarios', 'The enclosed overview highlights assumptions and open questions.'),
        ('Final invitation to discuss', 'This is the final planned contact in this campaign. No response or agreement is presumed.'),
    )
    emails = [{'subject': subject, 'body': opening + '\n\n' + introduction + contact}
              for subject, opening in stages]
    letters = [email['body'] for email in emails[1:]]
    return {'emails': emails, 'letters': letters,
        'flyers': ['Explore a preliminary passive Kratky concept\n' + introduction + contact,
                   'Discuss scenario assumptions and next evidence steps\n' + introduction + contact],
        'brochure': 'GROW UP — PRELIMINARY OPPORTUNITY BRIEF\n\n' + introduction +
            '\n\nProcess: evidence review, conditional design, deterministic financial comparisons, '
            'and further review before any executable proposal. No return, approval or capacity is guaranteed.' + contact,
        'business_card': 'Grow Up\nPreliminary property-to-agriculture feasibility\nSimeon\n' + CONTACT}


def demo_message(campaign, *, start_at, timezone_name):
    """Build .eml bytes, ZIP bytes and chronological hypothetical itinerary.

    An explicit timezone/aware start is mandatory. No fabricated marketing dollar
    costs: increasing tiers describe collateral scope; printing/postage are unknown.
    Sabbath adjustment is presentation-only and uses the existing offline guard.
    """
    if campaign.get('campaign_id') != 'campaign:' + digest_payload(
            {k:v for k,v in campaign.items() if k != 'campaign_id'}):
        raise ValueError('intact campaign required')
    if campaign.get('status') == 'suppressed':
        raise ValueError('suppressed campaign cannot prepare demo delivery')
    if not isinstance(start_at, datetime) or start_at.tzinfo is None:
        raise ValueError('explicit aware campaign start required')
    zone = ZoneInfo(timezone_name)
    start = start_at.astimezone(zone)
    assets = campaign.get('collateral') or {}
    if (len(assets.get('emails', [])) != 4 or len(assets.get('letters', [])) != 3
            or len(assets.get('flyers', [])) != 2 or not assets.get('brochure') or not assets.get('business_card')):
        raise ValueError('complete campaign collateral required')
    from app.sabbath import blackout
    itinerary = []
    for index in range(4):
        planned = start + timedelta(days=7 * index)
        adjusted = planned
        # Advance at most two days, keeping aware timestamps through DST.
        for _ in range(49):
            if not blackout(adjusted):
                break
            adjusted += timedelta(hours=1)
        else:
            raise ValueError('campaign date cannot clear Sabbath guard')
        itinerary.append({'burst':index + 1, 'proposed_at':adjusted.isoformat(),
            'delivery_channel':'email','collateral_distribution':'email_attachment_only','physical_mail_enabled':False,
            'original_at':planned.isoformat(), 'timezone':timezone_name,
            'sabbath_adjusted':adjusted != planned, 'email':index + 1,
            'letter':index if index else None, 'flyer':1 if index == 2 else 2 if index == 3 else None,
            'business_cards':1 if index == 2 else 2 if index == 3 else 0,
            'brochure':index == 3, 'cost_tier':index + 1,
            'printing_postage_cost_usd':None, 'status':'hypothetical_not_scheduled'})
    lines = ['DEMO — hypothetical four-burst campaign; nothing scheduled or sent.',
             'Four weekly stages: day 0, 7, 14 and 21 within a one-month campaign window.',
             'Cost tiers increase collateral scope; actual dollar costs are unknown.']
    for item in itinerary:
        lines.append(f"Burst {item['burst']} — {item['proposed_at']} ({timezone_name}): "
            f"email {item['email']}; letter {item['letter'] or 'none'}; flyer {item['flyer'] or 'none'}; "
            f"business cards {item['business_cards']}; brochure {'yes' if item['brochure'] else 'no'}.")
    files = {}
    for i, email in enumerate(assets['emails'], 1):
        files[f'emails/email-{i}.txt'] = email['subject'] + '\n\n' + email['body']
    for i, text in enumerate(assets['letters'], 1): files[f'letters/letter-{i}.txt'] = text
    for i, text in enumerate(assets['flyers'], 1): files[f'flyers/flyer-{i}.txt'] = text
    files['brochure.txt'] = assets['brochure']
    files['business-card.txt'] = assets['business_card']
    files['itinerary.json'] = json.dumps(itinerary, indent=2)
    files['README.txt'] = '\n'.join(lines) + '\nText collateral for review, not print-ready artwork.\n'
    if campaign.get('data_provenance') == 'synthetic':
        label = 'SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY; VERIFICATION AND AUTHORIZATION ARE SIMULATED.\n'
        lines.insert(0, label.strip())
        files = {name: (label + text if name.endswith('.txt') else text) for name, text in files.items()}
    buf = BytesIO()
    with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as archive:
        for name, text in sorted(files.items()):
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, text.encode('utf-8'))
    attachment = buf.getvalue()
    if len(attachment) > 5_000_000: raise ValueError('demo attachment exceeds limit')
    message = EmailMessage(policy=SMTP)
    message['From'] = SENDER
    message['To'] = DEMO_RECIPIENT
    message['Reply-To'] = CONTACT
    message['Subject'] = '[DEMO] Grow Up four-stage campaign review package'
    message.set_content('\n'.join(lines) + '\n\nAll campaign materials are attached for review. Contact: ' + CONTACT)
    message.add_attachment(attachment, maintype='application', subtype='zip', filename='growup-campaign-demo.zip')
    return {'recipient':DEMO_RECIPIENT, 'sender':SENDER, 'reply_to':CONTACT,
        'itinerary':itinerary, 'zip_bytes':attachment, 'message_bytes':message.as_bytes(),
        'sent':False, 'scheduled':False, 'provider_calls':0}
