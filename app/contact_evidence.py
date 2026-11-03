"""Pure owner/contact intake checks. Syntax and references do not authenticate ownership."""
import re
from urllib.parse import urlsplit
from uuid import UUID


def assess_contact(entry, *, property_id):
    result={'eligible_for_draft':False,'ownership_verified':False,'channel_authorized':False,
        'status':'needs_evidence','email':None,'reasons':[],'evidence_refs':[]}
    if not isinstance(entry,dict) or not isinstance(entry.get('value'),dict):
        result['reasons']=['structured property-bound contact evidence required']; return result
    value=entry['value']; refs=entry.get('evidence_refs')
    if set(value)-{'name','email','property_id','relationship'}:
        result['reasons'].append('unsupported contact assertions')
    for field in ('name','email','relationship'):
        text=value.get(field)
        if not isinstance(text,str) or not text.strip() or len(text)>254 or any(ord(c)<32 or ord(c)==127 for c in text):
            result['reasons'].append('invalid '+field)
    try:
        if UUID(str(value.get('property_id'))) != UUID(str(property_id)):
            result['reasons'].append('contact evidence names a different property')
    except (ValueError,TypeError): result['reasons'].append('explicit property identity required')
    email=value.get('email','')
    if not isinstance(email,str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+",email) or len(email.split('@')[0])>64:
        result['reasons'].append('one plain email address required')
    if isinstance(email,str) and (email.split('@')[0].startswith('.') or email.split('@')[0].endswith('.') or '..' in email.split('@')[0]):
        result['reasons'].append('invalid email local part')
    if value.get('relationship') not in ('owner','authorized_representative'):
        result['reasons'].append('supported ownership/representation claim required')
    if not isinstance(refs,list) or not refs or len(refs)>50:
        result['reasons'].append('contact evidence references required')
    else:
        for ref in refs:
            if not isinstance(ref,str) or len(ref)>2000:
                result['reasons'].append('invalid contact evidence reference'); continue
            try:
                parsed=urlsplit(ref)
            except ValueError:
                result['reasons'].append('invalid contact evidence URL'); continue
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                result['reasons'].append('source URL references required; no embedded credentials')
        if not result['reasons']: result['evidence_refs']=list(refs)
    if result['reasons']: return result
    result.update(eligible_for_draft=True,status='provided_unverified',email=email,
        claimed_name=value['name'],claimed_relationship=value['relationship'],
        reasons=['Ownership, source authenticity, mailbox control and channel permission require independent evidence; no DNS or provider lookup performed.'])
    return result
