"""Deterministic specialist-chat preparation; no private dossiers or execution."""
import re
from app.personalities import presentation,require_channel


def respond(owner,text,channel):
    if channel=='email':
        from app.public_faq import answer
        result=answer(text);return {**result,**presentation('csr',result['text'],'email',human_initiated=True,introduce=True),'conversation_owner':'csr','handoff':None}
    require_channel(owner,'chat','inbound');words=text.lower()
    explicit=re.search(r'\b(?:connect|speak|talk|switch|transfer|return|back)\b.{0,45}\b(simeon|solomon|customer service|customer care|csr)\b',words)
    target=explicit.group(1) if explicit else None
    if target in ('customer service','customer care'):target='csr'
    if re.search(r'\b(?:connect|speak|talk|switch|transfer)\b.{0,35}\b(uriel|wondrue|priscilla)\b',words):
        return {**presentation(owner,'That agent does not conduct public support chat. I can help here or connect you with Simeon or Solomon.','chat',human_initiated=True),'conversation_owner':owner,'escalate':None,'handoff':None}
    if owner=='csr' and not target:
        from app.public_faq import answer
        result=answer(text)
        if result.get('escalate') in ('simeon','solomon'):target=result['escalate']
        else:return {**result,**presentation('csr',result['text'],'chat',human_initiated=True),'conversation_owner':'csr','handoff':None}
    target=target or owner
    require_channel(target,'chat','outbound',human_initiated=True)
    if target=='simeon':
        response='I can help organize your property-opportunity questions and the next discussion. Which property or proposal are you asking about, and what would you like clarified? A chat does not verify ownership, accept terms, confirm financing or sign an agreement. Please keep confidential documents out of this public session.'
    elif target=='solomon':
        response='I can help outline construction scope, sequencing, inspections or proposed changes. Which part of the project needs coordination? Site access, contractor qualifications, permits, safety and any work or spending authorization still require their own evidence and review. No work is scheduled or authorized by this chat.'
    else:
        response='I’m back with you for general support. What would you like help with?'
    if target=='simeon' and re.search(r'\b(cost|price|profit|return|financ)',words):
        response='A useful financial discussion starts with the exact property, proposed space and retained cost/revenue assumptions. I cannot quote a price, promise a return or confirm funding from this public chat. Which assumption or proposed term would you like to clarify?'
    if target=='solomon' and re.search(r'\b(change|delay|schedule|inspection|permit)',words):
        response='Let’s separate the proposed scope, schedule dependencies, cost effect and required inspection or permit evidence. A change should be documented and reviewed before work proceeds. Tell me which checkpoint or proposed change needs attention; nothing is booked, purchased or authorized here.'
    exceptional=bool(re.search(r'\b(?:sign|execute|accept|authorize|approve|pay)\s+(?:(?:this|the|my|our|an?)\s+)?(?:contract|agreement|offer|payment|invoice|work|purchase)\b',words))
    if exceptional:response+=' I will retain this request for the human owner’s review; I cannot make that commitment.'
    handoff={'from':owner,'to':target,'reason':'Customer-requested specialist routing','context_preserved':True} if target!=owner else None
    return {**presentation(target,response,'chat',human_initiated=True,introduce=True),'conversation_owner':target,'handoff':handoff,'escalate':'human_owner' if exceptional else None,
        'capability_scope':'Preliminary deterministic conversation guidance; no negotiation, dossier lookup or commitment was executed.'}
