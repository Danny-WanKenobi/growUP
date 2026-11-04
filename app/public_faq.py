"""Public, evidence-qualified Grow Up knowledge used by the page and CSR."""
FAQS = [
 ('What is Grow Up?', 'Grow Up explores whether underused property could support agricultural use. It brings property evidence, preliminary farm layouts and inspectable financial scenarios into one traceable workflow.'),
 ('Which properties can Grow Up explore?', 'High-rises, general commercial space and warehouse or industrial buildings are core property types. Outdoor food-forest concepts are also explored in demonstrations. Every site needs its own evidence and suitability review.'),
 ('How do I discuss a property opportunity?', 'Contact Simeon at simeon@growup.securecodeusa.com. You can ask general questions first. A conversation does not establish ownership, suitability, permission or an agreement.'),
 ('What growing method does Grow Up use?', 'Indoor concepts use passive, non-circulating Kratky hydroponics. Layouts may include containers, reservoirs and stacked racks, while retaining the passive growing principle.'),
 ('Does Grow Up guarantee a profitable conversion?', 'No. Financial outputs are conditional estimates based on explicit assumptions. Conversion costs, crop yields, utilities, labor, leases and market demand can change the outcome.'),
 ('Are designs construction-ready?', 'No. Designs are preliminary feasibility concepts. Structural capacity, electrical supply, HVAC, water, access and permitted use require supporting evidence and qualified professional review.'),
 ('What happens when evidence is missing?', 'The workflow records the gap and can return a dossier for more information. Unknown facts stay unknown; a polished scenario does not turn an assumption into a verified fact.'),
 ('Can occupied buildings be considered?', 'A scenario may explore voluntary tenant relocation or consolidation. Existing occupancy is recorded separately. Tenant consent, lease terms, costs and access remain constraints; no tenant move is assumed to be authorized.'),
 ('What is the difference between a demo and real work?', 'Synthetic demonstrations use fictional properties and clearly labeled simulated decisions. Ordinary demo outreach is simulated. Any separately authorized real test email is a real delivery, not evidence of customer interest.'),
 ('Who handles construction questions?', 'Solomon handles construction coordination questions at solomon@growup.securecodeusa.com. An email or preliminary plan does not authorize site entry, procurement or construction.'),
 ('Who answers general questions?', 'Grow Up Care is the customer-service assistant. Use HELP for immediate FAQ guidance or email csr@growup.securecodeusa.com. Deal questions can be escalated to Simeon and construction questions to Solomon.'),
 ('Can I arrange a future conversation?', 'Yes. HELP can record a tentative appointment request for Simeon or Solomon. The time and timezone are explicit; confirmation and availability are separate from the request. No external calendar invitation is implied.'),
 ('Can the assistant sign a contract or approve work?', 'No. Customer-service replies do not execute contracts, transfer ownership, provide professional certification or authorize physical work. Exceptional decisions are escalated for review.'),
 ('What should I share in HELP?', 'Share a general question and, when requesting an appointment, a contact email. Do not submit passwords, API keys, financial-account details, confidential contracts or sensitive personal documents. Conversations are retained for support and follow-up.'),
 ('Why does LOGIN require credentials?', 'The public page and HELP are open to visitors. LOGIN is the existing private operator gateway; access to operational tools remains restricted. Public support does not grant access to dossiers or owner controls.'),
]


def answer(text):
    """Deterministic FAQ matching, with explicit escalation rather than invention."""
    import re
    words=set(re.findall(r'[a-z]+',text.lower()))
    normalized=' '.join(re.findall(r'[a-z]+',text.lower()))
    for index,(question,response) in enumerate(FAQS,1):
        if normalized==' '.join(re.findall(r'[a-z]+',question.lower())):
            return {'text':response,'faq':index,'escalate':None}
    if words and words <= {'hi','hello','hey','there','thanks','thank','you'}:
        return {'text':'Hello! I’m Grow Up Care. Ask me about Grow Up, preliminary farm concepts, evidence requirements or how to reach Simeon or Solomon.', 'escalate':None}
    target = 'solomon' if words & {'construction','contractor','solomon','procurement','site'} else (
        'simeon' if words & {'simeon','deal','closing','offer','contract','proposal','opportunity'} else None)
    if words & {'urgent','emergency','hazard','unsafe'}:
        return {'text':'I cannot assess an emergency or authorize work. Please use the appropriate local emergency or qualified site-response service. I can retain your concern for owner review.', 'escalate':'human_owner'}
    if target:
        name=target.title()
        return {'text':f'I have recorded this for {name} review. You can also contact {target}@growup.securecodeusa.com. No agreement, booking or work authorization is created by this conversation.', 'escalate':target}
    stop={'what','is','the','a','an','do','does','can','i','it','to','and','of','how','grow','up','my','you','for'}
    ranked=[]
    for index,(question,response) in enumerate(FAQS):
        terms=set(re.findall(r'[a-z]+',question.lower()))-stop
        ranked.append((len((words-stop)&terms),-index,index))
    score,_,index=max(ranked)
    if score:
        return {'text':FAQS[index][1],'faq':index+1,'escalate':None}
    return {'text':'I can explain Grow Up, preliminary farming concepts, evidence requirements and next steps. I have recorded your question for customer-service review because it is outside my current FAQ knowledge. Please avoid sharing sensitive information.', 'escalate':'human_owner'}
