'use strict';
// The existing shell supplies api(), panel(), structure() and safe text nodes.
let guidedState=null,guidedBusy=false,guidedPendingCase=null;

async function openGuidedReview(){
 panel('guided','Guided Human Review','SYNTHETIC PRACTICE ONLY · Separate growup_handoff_demo database. No discovery, models, email, contracts or operational dossier changes.');
 if(!config.guided_demo_review_available){$('panel-content').append(e('p','This server has no configured isolated handoff demo. Use the development cockpit; no operational database will be substituted.','empty'));return;}
 const create=e('button','Create a separate practice dossier');
 create.onclick=async()=>{if(guidedBusy)return;guidedBusy=true;create.disabled=true;guidedPendingCase=guidedPendingCase||crypto.randomUUID();try{guidedState=await api('guided-review',{id:guidedPendingCase});guidedPendingCase=null;renderGuided();}catch(err){notify(err.message);}finally{guidedBusy=false;create.disabled=false;}};
 $('panel-content').append(create,e('p','Creates one fictional case using the existing canonical pipeline and custody services. Existing demonstration runs remain unchanged.','empty'));
 try{const data=await api('guided-review');if(data.items.length){const select=e('select');select.setAttribute('aria-label','Saved practice case');for(const row of data.items){const option=e('option',new Date(row.created_at).toLocaleString()+' · '+row.id.slice(0,8));option.value=row.id;select.append(option);}const open=e('button','Open saved practice case','quiet');open.onclick=async()=>{try{guidedState=await api('guided-review/'+select.value);renderGuided();}catch(err){notify(err.message);}};$('panel-content').append(select,open);}}catch(err){notify(err.message);}
}

async function refreshGuided(){guidedState=await api('guided-review/'+guidedState.case.id);renderGuided();}
async function guidedRecord(version){try{const record=await api(`guided-review/${guidedState.case.id}/record?version=${version}`);panel('guided-record','Synthetic retained dossier · v'+record.version,'Exact immutable version in growup_handoff_demo. This is fictional evidence and simulated authorization.');const back=e('button','Back to guided review','quiet');back.onclick=renderGuided;const d=e('div',undefined,'structured');structure(record,d);$('panel-content').append(back,d);}catch(err){notify(err.message);}}
function guidedEvidenceButton(version,label){const b=e('button',label,'quiet');b.onclick=()=>guidedRecord(version);return b;}

function renderGuided(){
 const state=guidedState,review=state.review;
 panel('guided','Guided Human Review','SYNTHETIC PRACTICE ONLY · No property claims, professional approvals or external permissions are established.');
 const caseBar=e('article',undefined,'record-card');caseBar.append(e('span','ISOLATED SYNTHETIC CASE','tag'),e('h3','A revised fictional zoning note needs review'),e('p','Nicole’s earlier screening report must stay intact. You can supply a fictional update or explicitly keep the evidence unknown. The shared policy remains unchanged.'),e('p',`Current holder: ${human(state.current_holder)} · ${state.verified_receipts} verified custody receipts · dossier v${state.source_version}`));
 const refresh=e('button','Refresh stored checkpoint','quiet');refresh.onclick=()=>refreshGuided().catch(err=>notify(err.message));const cases=e('button','Other practice cases','quiet');cases.onclick=openGuidedReview;caseBar.append(guidedEvidenceButton(state.case.original_version,'Inspect original version'),guidedEvidenceButton(state.source_version,'Inspect current version'),refresh,cases);$('panel-content').append(caseBar);
 if(state.operating_rehearsal){const proof=e('article',undefined,'record-card');proof.append(e('h3','Complete fictional owner operating cycle'),e('p','Assessment → approved correction → dependency refresh → protected filing → dated obligation → later dispute. No real property or external action.'));const timeline=e('ol');for(const item of state.operating_rehearsal.timeline)timeline.append(e('li',human(item.phase)+' · dossier v'+item.dossier_version));proof.append(timeline);$('panel-content').append(proof);}
 if(!review||review.status==='rejected'||review.status==='complete'){
  if(review)renderGuidedOutcome(state);
  const form=e('form',undefined,'action-form');form.append(e('h3','Choose what to preview'));
  const select=e('select');select.setAttribute('aria-label','Synthetic correction choice');for(const[k,label]of Object.entries(state.choices)){const option=e('option',label);option.value=k;select.append(option);}const prepare=e('button','Prepare exact preview');form.append(select,prepare);
  let previewId=null;form.onsubmit=async ev=>{ev.preventDefault();if(guidedBusy)return;guidedBusy=true;prepare.disabled=true;previewId=previewId||crypto.randomUUID();try{guidedState=await api(`guided-review/${state.case.id}/preview`,{id:previewId,choice:select.value,expected_digest:state.source_digest});renderGuided();}catch(err){notify(err.message);}finally{guidedBusy=false;prepare.disabled=false;}};$('panel-content').append(form);return;
 }
 const m=review.manifest;
 const preview=e('article',undefined,'record-card');preview.append(e('span',human(review.status),'tag'),e('h3','Exact proposed change'),e('p',m.explanation),e('p',`Source version ${m.source_version}; approval is bound to its exact digest and this complete preview.`));
 const before=m.before_inputs.official_requirements.value.find(r=>r.topic==='zoning_use');const after=m.after_inputs.official_requirements.value.find(r=>r.topic==='zoning_use');
 const dl=e('dl',undefined,'change-comparison');for(const field of ['record_id','record_date','requirement','state','disposition'])if(before[field]!==after[field])dl.append(e('dt',human(field)),e('dd',String(before[field])+' → '+String(after[field])));
 preview.append(dl,e('p','Expected result: '+human(m.expected_rework_status)+'. '+(m.close_target_administrative_review?'Only this administrative task will close after the exact assessment matches.':'This issue stays unresolved; needs-evidence is a valid result.')));
 const steps=e('ol');for(const step of m.steps)steps.append(e('li',step));preview.append(steps,e('p','All inherited reports, dossier versions, snapshots and custody history are preserved. Supplied fictional evidence remains unverified.'));
 const detail=e('details');detail.append(e('summary','Complete approved input and assessment manifest'));const body=e('div',undefined,'structured');structure(m,body);detail.append(body);preview.append(detail);$('panel-content').append(preview);
 if(['pending','deferred'].includes(review.status)){
  const form=e('form',undefined,'action-form');const label=e('label','Your decision reason');const note=e('textarea');note.required=true;note.maxLength=1000;note.setAttribute('aria-label','Guided review decision reason');label.append(note);const controls=e('div',undefined,'decision-buttons');for(const[value,text]of Object.entries({approve:'Approve this exact correction',defer:'Defer',reject:'Reject'})){const button=e('button',text);button.name='choice';button.value=value;controls.append(button);}form.append(label,controls);
  form.onsubmit=async ev=>{ev.preventDefault();if(guidedBusy)return;guidedBusy=true;controls.querySelectorAll('button').forEach(b=>b.disabled=true);try{guidedState=await api(`guided-review/${state.case.id}/${review.id}/decision`,{expected_digest:review.review_digest,choice:ev.submitter.value,note:note.value});renderGuided();}catch(err){notify(err.message+' Refresh the stored checkpoint before continuing.');try{await refreshGuided();}catch{}}finally{guidedBusy=false;controls.querySelectorAll('button').forEach(b=>b.disabled=false);}};$('panel-content').append(form);
 }else{
  const resume=e('button','Resume the approved correction');resume.onclick=async()=>{if(guidedBusy)return;guidedBusy=true;resume.disabled=true;try{guidedState=await api(`guided-review/${state.case.id}/${review.id}/resume`,{});renderGuided();}catch(err){notify(err.message+' No unchecked retry was performed.');try{await refreshGuided();}catch{}}finally{guidedBusy=false;resume.disabled=false;}};$('panel-content').append(e('p','The approval is already durable. Completed steps are not repeated; each remaining step checks the current source digest.'),resume);
 }
 renderGuidedTimeline(review);
}

function renderGuidedTimeline(review){
 if(!review.timeline.length)return;
 const section=e('article',undefined,'record-card');section.append(e('h3','Dated correction and custody history'));
 const list=e('ol');for(const point of review.timeline){const li=e('li');li.append(e('strong',human(point.phase)+' · '+human(point.holder)),e('p',new Date(point.at).toLocaleString()+` · dossier v${point.dossier_version} · ${point.verified_receipts} verified receipts`));if(point.receipt_id)li.append(e('p',`Receipt ${point.receipt_id} binds transfer version ${point.receipt_version}. Snapshot: ${point.snapshot_id}`,'evidence-line'));list.append(li);}section.append(list);$('panel-content').append(section);
}

function renderGuidedOutcome(state){
 const review=state.review;const complete=review.status==='complete';const outcome=review.result?.status;
 const section=e('article',undefined,'record-card');section.append(e('span',human(review.status),'tag'),e('h3',complete?(outcome==='needs_evidence'?'Returned to Manny — more evidence required':'Linked reassessment returned to Manny'):'Rejected — no correction dispatched'));
 section.append(e('p',`Original dossier preserved: ${state.original_preserved?'yes':'no'}. Inherited reports preserved: ${state.inherited_reports_preserved?'yes':'no'}.`));
 if(complete)section.append(e('p','Linked assessment digest: '+review.report_digest,'evidence-line'));
 const stale=Object.keys(state.stale_dependencies);if(stale.length)section.append(e('p','Downstream reports requiring reassessment: '+stale.map(human).join(', ')+'. These are not silently promoted.'));
 const tasks=state.owner_brief.exceptions.filter(t=>!['closed_by_owner','closed_as_duplicate'].includes(t.review_status));section.append(e('h3','Remaining evidence and review gaps'));
 if(tasks.length){const list=e('ul');for(const task of tasks.slice(0,10))list.append(e('li',String(task.requirement||task.reason||task.record_key).replaceAll('_',' ')));section.append(list);if(tasks.length>10)section.append(e('p',`${tasks.length-10} additional retained review items are included in the owner package.`));}else section.append(e('p','No unresolved administrative items are listed. This still grants no real-world permission.'));
 const details=e('details');details.append(e('summary','Source-linked owner package and audit context'));const d=e('div',undefined,'structured');structure({owner_brief:state.owner_brief,review},d);details.append(d);section.append(details);$('panel-content').append(section);renderGuidedTimeline(review);
}
