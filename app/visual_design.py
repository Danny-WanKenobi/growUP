"""Editable, deterministic artwork from retained inputs, never a site photograph.

Generated SVG contains no scripts, external resources, foreignObject or raw markup
from evidence. Financial copy is escaped verbatim; no new figures are invented.
"""
from base64 import b64encode
from hashlib import sha256
from html import escape
import json
import os
import textwrap
from xml.etree import ElementTree

from app.workflow import digest_payload
from app.workflow_db import WorkflowError

VERSION = 'growup-visuals-1'
CONTACT = 'simeon@growup.securecodeusa.com'
LABEL = 'ARTISTIC CONCEPT — NOT A SITE PHOTO OR CONSTRUCTION PLAN'


def _text(text, x, y, *, size=20, color='#e7eee7', width=85, step=27):
    lines = textwrap.wrap(str(text), width=width, break_long_words=True) or ['']
    return ''.join(f'<text x="{x}" y="{y+i*step}" font-size="{size}" fill="{color}">{escape(line)}</text>'
                   for i, line in enumerate(lines))


def _frame(title, subtitle, art, *, footer=LABEL, height=760):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}" '
            'role="img" aria-label="'+escape(title, quote=True)+'">'
            '<defs><linearGradient id="bg" x2="1" y2="1"><stop stop-color="#112c2c"/>'
            '<stop offset="1" stop-color="#071b1d"/></linearGradient></defs>'
            f'<rect width="1200" height="{height}" rx="24" fill="url(#bg)"/>'
            '<g font-family="Arial, sans-serif">'+_text('GROW UP / POSSIBILITY, WITH PERSPECTIVE',48,46,size=16,color='#a6ce86')+
            _text(title,48,108,size=36,width=52,step=44)+_text(subtitle,48,158,size=18,width=105)+art+
            _text(footer,48,height-46,size=14,color='#d9b97a',width=125,step=18)+'</g></svg>')


def _rack(x, y, *, tiers=3):
    pieces = [f'<path d="M{x} {y-245}V{y+20} M{x+190} {y-245}V{y+20}" stroke="#a9b7b4" stroke-width="9"/>']
    for tier in range(tiers):
        top=y-tier*82
        pieces += [f'<rect x="{x}" y="{top}" width="190" height="8" fill="#b1b7ac"/>',
                   f'<rect x="{x+12}" y="{top-33}" width="166" height="30" rx="6" fill="#355c71"/>',
                   f'<rect x="{x+12}" y="{top-36}" width="166" height="5" fill="#8ca8a0"/>',
                   f'<path d="M{x+20} {top-65}H{x+170}" stroke="#f4e7a7" stroke-width="6"/>']
        for offset in (35,75,115,155):
            pieces += [f'<path d="M{x+offset} {top-35}v-20" stroke="#a0d17d" stroke-width="3"/>',
                       f'<ellipse cx="{x+offset-7}" cy="{top-54}" rx="13" ry="6" fill="#8bc777" transform="rotate(25 {x+offset-7} {top-54})"/>',
                       f'<ellipse cx="{x+offset+8}" cy="{top-60}" rx="13" ry="6" fill="#a6d788"/>']
    return ''.join(pieces)


def concepts(report):
    """Three different views of the same proposal, not three capacity approvals."""
    basis = digest_payload(report.get('alternatives') or {})
    alternatives=report.get('alternatives') or {}
    chosen='rack3' if alternatives.get('rack3',{}).get('concept_ready') else next(iter(sorted(alternatives)),'unknown')
    supplied=((alternatives.get(chosen) or {}).get('scenario') or {}).get('outputs') or {}
    tiers=supplied.get('tiers', 3 if chosen=='rack3' else 1)
    tiers=int(tiers) if isinstance(tiers,(int,float)) and not isinstance(tiers,bool) and 1<=tiers<=4 else 1
    titles = ('A living room for growing', 'Space, access and light', 'The quiet root zone')
    arts = []
    perspective = '<path d="M70 560L180 480H1070L1140 560Z" fill="#264943"/>'
    perspective += ''.join(_rack(x,510,tiers=tiers) for x in (150,450,750))
    perspective += '<path d="M150 200H340 M450 200H640 M750 200H940" stroke="#fff2b6" stroke-width="8"/>'
    perspective += _text('Passive reservoirs. Ceiling and under-shelf lighting as applicable.',70,615,size=22)
    perspective += _text('Service clearances, loads, wiring and access require site-specific review.',70,652,size=18)
    arts.append(perspective)
    plan = '<rect x="70" y="220" width="690" height="350" rx="12" fill="#234943" stroke="#8baca0" stroke-width="3"/>'
    for x in (110,340,570):
        plan += f'<rect x="{x}" y="260" width="125" height="255" rx="10" fill="#426c54"/>'
        plan += _text('GROW',x+20,390,size=17,width=10)
    plan += _text('Access paths',790,290,size=26,width=24)+_text('Keep exits, service access and equipment clearances available. Geometry is illustrative; not to scale.',790,335,size=19,width=27)
    plan += _text('No room count, measured dimensions or usable area inferred from this sketch.',70,625,size=20)
    arts.append(plan)
    rootzone = '<rect x="100" y="325" width="470" height="220" rx="18" fill="#355c71"/>'
    rootzone += '<rect x="105" y="330" width="460" height="76" fill="#182b35"/>'
    rootzone += '<path d="M100 322H570" stroke="#c4d4bd" stroke-width="12"/>'
    rootzone += '<path d="M330 320V240 M330 280Q270 210 250 265 M330 260Q390 190 405 245" stroke="#a6d788" stroke-width="18" fill="none"/>'
    rootzone += '<path d="M315 335Q270 390 310 455 M345 335Q390 410 340 480 M330 335V490" stroke="#d5cbaa" stroke-width="5" fill="none"/>'
    rootzone += _text('Root-zone air space',650,350,size=27,width=26)+_text('Passive, non-circulating nutrient solution. Preserve established air roots; refill, quality and sanitation need an explicit plan.',650,398,size=20,width=37)
    rootzone += _text('Water supply, treatment and container capacity remain engineering inputs.',70,625,size=20)
    arts.append(rootzone)
    output=[]
    for index, (title, art) in enumerate(zip(titles, arts),1):
        quantities=[]
        for name, item in sorted((report.get('alternatives') or {}).items()):
            values=(item.get('scenario') or {}).get('outputs') or {}
            quantities.append(str(name)+': '+', '.join(f'{key}={values[key]}' for key in ('growing_m2','tiers','sites') if key in values))
        art += _text('Retained estimates / '+(' | '.join(quantities) or 'quantities unknown'),70,674,size=14,width=130,step=17)
        svg=_frame(title, 'Onesimus / passive Kratky conversion study / conceptual feasibility only', art)
        output.append({'id':f'onesimus-concept-{index}', 'kind':'generated_concept', 'author':'onesimus',
            'renderer':VERSION, 'title':title, 'svg':svg, 'sha256':sha256(svg.encode()).hexdigest(),
            'calculation_basis_digest':basis, 'source_inspection_digest':report.get('source_inspection_digest'),
            'classification':'artistic_concept', 'label':LABEL, 'to_scale':False,
            'source_photo_used':False, 'capacity_or_permission_verified':False})
        output[-1].update(source_scenario=chosen,depicted_tiers=tiers)
    return output


def validate_concepts(report):
    if report.get('conceptualizations') != concepts(report):
        raise WorkflowError('three intact calculation-linked Onesimus conceptualizations required')
    return report['conceptualizations']


def validate_visual_package(package):
    """Reject active SVG, path traversal and unbounded raster rendering inputs."""
    drawings=package.get('concepts',[])
    if len(drawings)!=3 or [d.get('id') for d in drawings]!=[f'onesimus-concept-{i}' for i in (1,2,3)]:
        raise WorkflowError('three named retained visual concepts required')
    tags={'svg','defs','linearGradient','stop','rect','g','text','path','ellipse'}
    attrs={'width','height','viewBox','role','aria-label','id','x2','y2','stop-color','offset','x','y','rx','ry','cx','cy',
           'fill','font-family','font-size','d','stroke','stroke-width','transform'}
    for drawing in drawings:
        svg=drawing.get('svg','');tiers=drawing.get('depicted_tiers')
        if (drawing.get('renderer')!=VERSION or not isinstance(svg,str) or len(svg)>128_000
                or drawing.get('sha256')!=sha256(svg.encode()).hexdigest()
                or type(tiers) is not int or not 1<=tiers<=4 or '<!' in svg):
            raise WorkflowError('campaign concept integrity or rendering bound failed')
        try:tree=ElementTree.fromstring(svg)
        except ElementTree.ParseError:raise WorkflowError('invalid concept SVG') from None
        for node in tree.iter():
            if node.tag.split('}')[-1] not in tags or set(node.attrib)-attrs:
                raise WorkflowError('active or external SVG content refused')
            if any('url(' in value and value!='url(#bg)' for value in node.attrib.values()):
                raise WorkflowError('external SVG resources refused')
    if len(package.get('source_photos',[]))>10:
        raise WorkflowError('campaign photo count exceeds bound')


def campaign_visuals(dossier, engineering, introduction):
    drawings = validate_concepts(engineering)
    photos=[]; unavailable=[]
    # Exact content-addressed approval is operator configuration, never a model's
    # or incoming listing's assertion. Internal source caching does not grant rights.
    approvals=set(filter(None,os.getenv('GROWUP_IMAGE_PUBLICATION_SHA256','').split(',')))
    media=dossier.get('media') or {}
    for image in media.get('images',[]):
        if image.get('sha256') not in approvals:
            unavailable.append({'sha256':image.get('sha256'),'reason':'publication rights not approved'});continue
        from app.dossier_media import read_photo
        read_photo(image)  # Validate the retained bytes before binding the reference.
        photos.append({**image, 'rights_basis':'operator_content_hash_allowlist',
                       'label':'Source photo; property association unverified unless separately established.'})
    # Artwork is reused by Simeon and Solomon; these roles never invoke a designer.
    return {'schema':'campaign-visuals-1', 'author':'uriel', 'concepts':drawings,
            'source_photos':photos, 'withheld_source_photos':unavailable,
            'copy':introduction, 'synthetic':bool(dossier.get('synthetic')),
            'status':'prepared_not_sent', 'provider_calls':0, 'photos_missing':not bool(photos),
            'contact':CONTACT, 'reuse_agents':['simeon','solomon'], 'renderer':VERSION}


def render_material(title, text, package, *, index=0, asset_prefix=None):
    """Self-contained reviewable HTML; exact copy retained, concepts embedded safely."""
    drawing=package['concepts'][index % 3]
    # Never accept arbitrary SVG via a campaign payload, even with a valid hash.
    if drawing.get('renderer')!=VERSION or drawing.get('sha256')!=sha256(drawing['svg'].encode()).hexdigest():
        raise WorkflowError('campaign concept integrity failure')
    if asset_prefix not in (None, '', '../'):
        raise WorkflowError('local campaign asset prefix required')
    visual=('data:image/svg+xml;base64,'+b64encode(drawing['svg'].encode()).decode()
            if asset_prefix is None else asset_prefix+'concepts/'+drawing['id']+'.svg')
    photos=''
    approvals=set(filter(None,os.getenv('GROWUP_IMAGE_PUBLICATION_SHA256','').split(',')))
    for image in package.get('source_photos',[]):
        if image['sha256'] not in approvals:
            raise WorkflowError('photo publication approval changed; prepare a new reviewed package')
        from app.dossier_media import read_photo
        body=read_photo(image)
        src=('data:image/webp;base64,'+b64encode(body).decode() if asset_prefix is None
             else asset_prefix+'media/'+image['sha256']+'.webp')
        photos+='<figure><img src="'+src+'" alt="Retained source photograph"><figcaption>'+escape(image['label'])+' Source: '+escape(image['page_url'])+'</figcaption></figure>'
    if 'business card' in title.lower():
        return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>Grow Up / Simeon</title><style>body{background:#e8eee7;font:18px Arial;padding:24px;color:#183a35}'
            'main{max-width:760px;margin:40px auto}article{padding:42px;background:#102c2c;color:white;border-radius:18px;border-left:10px solid #a6ce86}'
            'h1{font-size:46px;margin:0 0 6px}h2{font-size:28px}a{color:#c4e5a7;overflow-wrap:anywhere}small{font-size:13px}'
            '@media print{body{padding:0;background:white}main{margin:0}article{border-radius:0}}</style></head><body><main>'
            +('<p>SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY</p>' if package.get('synthetic') else '')+
            '<article><h1>Grow Up</h1><p>Possibility, with perspective.</p><h2>Simeon</h2><p>Property opportunity conversations</p><p><a href="mailto:'+CONTACT+'">'+CONTACT+'</a></p><small>Preliminary concepts. Evidence before commitments.</small></article>'
            '<p>Digital collateral / confirm printer dimensions and production specifications before printing.</p></main></body></html>')
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'">'
            '<title>'+escape(title)+'</title><style>body{margin:0;background:#e8eee7;color:#153735;font:17px/1.65 Arial,sans-serif}'
            'main{max-width:900px;margin:36px auto;background:#fff;padding:40px;border-top:8px solid #7baf62;border-radius:14px}'
            'h1{font-size:38px;line-height:1.2}img{max-width:100%;height:auto;border-radius:12px}p{white-space:pre-wrap}'
            '.eyebrow{letter-spacing:.15em;font-size:12px}aside{padding:18px;background:#edf4e9}footer{border-top:1px solid #ccc;padding-top:20px}'
            '@media(max-width:600px){main{margin:10px;padding:20px}h1{font-size:29px}}@media print{body{background:white}main{margin:0}}</style></head><body><main>'
            '<div class="eyebrow">GROW UP / POSSIBILITY, WITH PERSPECTIVE</div><h1>'+escape(title)+'</h1>'
            +('<aside>SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY</aside>' if package.get('synthetic') else '')+
            '<figure><img src="'+visual+'" alt="'+escape(drawing['title'],quote=True)+'"><figcaption>'+LABEL+'</figcaption></figure>'
            +photos+'<p>'+escape(text)+'</p><aside>Preliminary concept. No guaranteed returns, verified capacity, permissions or construction approval. Alternative scenarios are not additive.</aside>'
            '<footer>Discuss the evidence and next steps with Simeon: <a href="mailto:'+CONTACT+'">'+CONTACT+'</a></footer></main></body></html>')


def collateral_artwork(assets, package):
    """One shared preparation path for ordinary campaigns and demo ZIP export."""
    validate_visual_package(package)
    files={}
    for index,item in enumerate(assets['emails'],1):
        files[f'artwork/emails/email-{index}.html']=render_material(item['subject'],item['body'],package,index=index-1,asset_prefix='../')
    for kind in ('letters','flyers'):
        for index,text in enumerate(assets[kind],1):
            title=('Room for a fresh possibility' if index==1 else 'A concept worth a closer look') if kind=='flyers' else 'An invitation to explore the possibilities'
            files[f'artwork/{kind}/{kind[:-1]}-{index}.html']=render_material(title,text,package,index=index-1,asset_prefix='../')
    for kind in ('brochure','business_card'):
        title='From underused space to a growing possibility' if kind=='brochure' else 'Grow Up / business card'
        files[f'artwork/{kind}.html']=render_material(title,assets[kind],package,asset_prefix='')
    return files


def raster_concept(drawing):
    """Mail-compatible original PNG interpretation; no remote image renderer."""
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    if type(drawing.get('depicted_tiers')) is not int or not 1<=drawing['depicted_tiers']<=4:
        raise WorkflowError('bounded illustrated tier count required')
    canvas=Image.new('RGB',(1200,760),'#102c2c'); pen=ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=28); small=ImageFont.load_default(size=18)
    pen.text((50,35),'GROW UP / PASSIVE KRATKY CONVERSION STUDY',font=small,fill='#a6ce86')
    pen.text((50,85),drawing['title'],font=font,fill='white')
    if drawing['id']=='onesimus-concept-1':
        for x in (150,450,750):
            pen.line([(x,220),(x,530)],fill='#a9b7b4',width=9)
            pen.line([(x+190,220),(x+190,530)],fill='#a9b7b4',width=9)
            for y in [490-i*95 for i in range(drawing['depicted_tiers'])]:
                pen.rectangle((x+10,y-34,x+180,y),fill='#355c71')
                pen.line([(x+10,y-65),(x+180,y-65)],fill='#fff2b6',width=6)
                for dx in (35,75,115,155):
                    pen.line([(x+dx,y-34),(x+dx,y-57)],fill='#a6d788',width=3)
                    pen.ellipse((x+dx-16,y-63,x+dx+2,y-49),fill='#85c274')
        note='Passive reservoirs; ceiling and under-shelf lighting as applicable.'
    elif drawing['id']=='onesimus-concept-2':
        pen.rounded_rectangle((90,200,790,550),radius=12,fill='#264943',outline='#8baca0',width=3)
        for x in (135,365,595):pen.rounded_rectangle((x,240,x+130,505),radius=10,fill='#527e5a')
        pen.text((830,280),'ACCESS\nPATHS',font=font,fill='#c8e4b4')
        note='Illustrative geometry only. Preserve exits, access and equipment clearance.'
    else:
        pen.rounded_rectangle((130,300,590,550),radius=18,fill='#355c71')
        pen.rectangle((135,305,585,395),fill='#182b35')
        pen.line([(125,299),(595,299)],fill='#c4d4bd',width=12)
        for x in (300,335,370):pen.line([(x,305),(x-20,450),(x+8,490)],fill='#d5cbaa',width=5)
        pen.line([(335,300),(335,210)],fill='#a6d788',width=10)
        pen.ellipse((270,205,337,240),fill='#a6d788');pen.ellipse((335,185,400,222),fill='#8bc777')
        pen.text((650,340),'AIR SPACE\n\nPASSIVE SOLUTION',font=font,fill='#c8e4b4')
        note='Non-circulating root zone. Refill, water quality and sanitation need review.'
    pen.text((50,610),note,font=small,fill='white')
    pen.text((50,650),'Schematic only; no scale, measured geometry or verified building capacity.',font=small,fill='#a6ce86')
    pen.text((50,704),LABEL,font=small,fill='#d9b97a')
    out=BytesIO();canvas.save(out,'PNG',optimize=True);return out.getvalue()


def reuse_visuals(package, *, actor, text):
    """Simeon authors text and references retained art; no design/provider call."""
    if actor not in ('simeon','solomon') or not isinstance(text,str) or not text.strip():
        raise WorkflowError('specialist authored text and existing visual package required')
    campaign=(package.get('payload') or {}).get('campaign',{})
    visual=campaign.get('visual_package')
    if not visual:
        return {'status':'no_retained_visuals','design_requested':False,'provider_calls':0}
    return {'status':'reusing_retained_visuals', 'package_entry':str(package.get('entry_id','')),
            'visual_package_digest':digest_payload(visual), 'concept_sha256':[c['sha256'] for c in visual['concepts']],
            'artwork_digest':digest_payload(campaign.get('artwork') or {}),
            'authored_text_digest':digest_payload({'text':text}), 'design_requested':False,'provider_calls':0}


def canva_brief(agent, *, dossier_digest, package):
    if agent not in ('onesimus','uriel'):
        raise WorkflowError('only Onesimus and Uriel prepare design jobs; Simeon reuses retained visuals')
    return {'provider':'canva', 'agent':agent, 'source_dossier_digest':dossier_digest,
            'status':'account_connection_and_export_required', 'automatically_submitted':False,
            'requests_used':0, 'free_quota_verified':False, 'credit_allowance_assumed':False,
            'brand':{'palette':['#102c2c','#a6ce86','#e8eee7'],'type':'clear readable sans-serif',
                     'style':'calm, botanical, evidence-led; no photorealistic claims of an actual conversion'},
            'instructions':'Use the supplied concepts and exact conditional copy. Do not invent dimensions, yields, profits, safety, permissions or ownership. Clearly label all conceptual art. Preserve source image credits and approved reuse rights.',
            'materials': ['three engineering concept views'] if agent=='onesimus' else ['four emails','three letters','two flyers','brochure','business card'],
            'input_digest':digest_payload(package)}
