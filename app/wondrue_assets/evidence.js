'use strict';
async function openEvidenceDesk(record){
 const source=refOf(record),data=await api('evidence?'+new URLSearchParams(source));
 panel('evidence','Evidence & attachments','Retain documents for review. Uploading never verifies their claims or changes an assessment.');
 $('panel-content').append(cite({...source,kind:'dossier_version'}),e('p',data.limits,'warning'),e('p',`${data.items.length} of ${data.limit} attachments retained for this dossier. Up to 512 KiB each; PDF, PNG, JPEG, UTF-8 TXT/CSV. Downloads only; documents are not opened or parsed here.`));
 const form=e('form',undefined,'action-form'),file=e('input'),issuer=e('input'),url=e('input'),documentDate=e('input'),obtained=e('input'),note=e('textarea'),scope=e('select');
 function label(text,input){const l=e('label',text);input.setAttribute('aria-label',text);l.append(input);form.append(l);}
 file.type='file';file.accept='.pdf,.png,.jpg,.jpeg,.txt,.csv';file.required=true;label('Document file',file);
 issuer.required=true;issuer.maxLength=240;label('Declared issuer or source (unverified)',issuer);
 url.type='url';url.maxLength=2048;url.placeholder='Unknown if blank; no URL will be fetched';label('Source URL, if known',url);
 documentDate.type=obtained.type='date';label('Date on document, if known',documentDate);label('Date you obtained it, if known',obtained);
 scope.multiple=true;scope.size=5;scope.required=true;for(const value of ['identity','category','dimensions','occupancy','zoning','structural','electrical','water','financial','other']){const o=e('option',human(value));o.value=value;scope.append(o);}label('Claim scope (select up to eight)',scope);
 note.required=true;note.maxLength=1000;note.placeholder='What does this document claim to support? Include any uncertainty or contradiction.';label('Claim description and limitations',note);
 const prepare=e('button','Review attachment before saving');form.append(e('p','Authentication status will be owner supplied / unverified. Declared issuer, URL and dates are claims, not proof of authenticity. No agent will consume this file automatically.','empty'),prepare);
 const preview=e('div');
 form.onsubmit=async ev=>{ev.preventDefault();prepare.disabled=true;try{
  const selected=file.files[0];if(!selected||selected.size===0||selected.size>data.max_bytes)throw Error('Choose a nonempty document no larger than 512 KiB.');
  const scopes=[...scope.selectedOptions].map(o=>o.value);if(scopes.length>8)throw Error('Select at most eight claim scopes.');
  const buffer=await selected.arrayBuffer(),bytes=new Uint8Array(buffer),hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',buffer))].map(v=>v.toString(16).padStart(2,'0')).join('');
  let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  const request={id:crypto.randomUUID(),source,filename:selected.name,content_base64:btoa(binary),issuer:issuer.value,source_url:url.value||null,document_date:documentDate.value||null,obtained_on:obtained.value||null,claim_scope:scopes,note:note.value};
  preview.replaceChildren(e('h2','Confirm this exact unverified attachment'));const c=e('article',undefined,'record-card');c.append(e('h3',selected.name),e('p',`${selected.size} bytes · SHA-256 ${hash}`,'evidence-line'),e('p','Declared source: '+request.issuer),e('p','Document date: '+(request.document_date||'Unknown')+' · Obtained: '+(request.obtained_on||'Unknown')),e('p','Claim scope: '+scopes.map(human).join(', ')),e('p',request.note));if(request.source_url)c.append(e('p',request.source_url));
  const save=button('Save unverified attachment & queue Human Review',async()=>{save.disabled=true;try{await api('evidence',request);notify('Document retained and review task queued. Dossier facts, reports and custody are unchanged.');await openEvidenceDesk(record);}finally{save.disabled=false;}});c.append(save,e('p','The selected server will retain these private bytes and metadata. No source retrieval, document parsing, verification admission or external action.','warning'));preview.append(c);
 }catch(err){notify(err.message);}finally{prepare.disabled=false;}};
 for(const item of data.items){const c=e('article',undefined,'record-card');c.append(e('span',human(item.authentication_status),'tag'),e('h3',item.filename),e('p',item.declared_issuer),e('p',item.note),e('p','Document date: '+(item.document_date||'Unknown')+' · Obtained: '+(item.obtained_on||'Unknown')+' · Retained: '+item.received_at),e('p','Claim scope: '+item.claim_scope.map(human).join(', ')),e('p',item.sha256,'evidence-line'),e('p',item.bound_to_selected_version?'Bound to this selected version.':'Bound to an earlier/different retained version; never silently rebound.','warning'));if(item.source_url)c.append(e('p',item.source_url));
  const download=e('a','Download untrusted original ↗');download.href=`/wondrue/api/evidence/${encodeURIComponent(source.run_id)}/${encodeURIComponent(item.id)}/download`;c.append(download,button('Inspect intake review task',()=>inspect({run_id:source.run_id,kind:'owner_review_task',key:item.review_task_key,version:1,title:'Evidence intake review'})));$('panel-content').append(c);
 }
 if(config.grant==='full')$('panel-content').append(form,preview);else $('panel-content').append(e('p','Read-only session. Owner access is required to retain a document.','empty'));
}
