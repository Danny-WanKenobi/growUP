"""Credential-free page shell; all data requests use Wondrue authentication."""
import html
from app.handoff_demo import STAGES


def page():
    options = '<option value="">All checkpoints GO</option>' + ''.join(
        f'<option value="{html.escape(a)}">NO-GO at {html.escape(a)}</option>' for a in STAGES)
    from app.demo_catalogue import CASES
    properties='<option value="">Checkpoint matrix (generic fixture)</option>'+''.join(f'<option value="{html.escape(c["id"])}">{html.escape(c["name"]+" — "+c["city"]+", "+c["state"]+" / "+c["property_type"])}</option>' for c in CASES)
    return TEMPLATE.replace("__OPTIONS__", options).replace("__PROPERTIES__",properties)


TEMPLATE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grow Up — Synthetic Custody Demonstration</title>
<style>
*{box-sizing:border-box}body{font:16px system-ui,sans-serif;margin:0;background:#eef3ef;color:#18342b}main{max-width:1120px;margin:auto;padding:24px}
main,section,label,input,select{min-width:0}main{overflow-wrap:anywhere}label{max-width:100%}input,select{width:100%;max-width:100%}h1{margin-bottom:8px}.notice{padding:16px;background:#fff3ce;border:2px solid #ad7800;border-radius:8px}
section{margin-top:18px;padding:20px;background:white;border-radius:10px;box-shadow:0 2px 8px #0001}
label{display:inline-flex;flex-direction:column;gap:6px;margin:6px 14px 10px 0}input,select,button{font:inherit;padding:9px;border:1px solid #a8b9af;border-radius:5px}
button{background:#205b43;color:white;cursor:pointer;margin:4px}button:disabled{opacity:.45;cursor:default}#stop{background:#8c342f}
#status{font-weight:650;padding:12px;background:#eaf3ed;border-radius:6px}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:9px;border-bottom:1px solid #dde5df;vertical-align:top}
.go{color:#14663f}.nogo{color:#a12525}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;background:#f3f5f4;padding:12px}a{color:#205b43}.scroll{max-width:100%;overflow-x:auto}table{min-width:650px}@media(max-width:600px){main{padding:14px}section{padding:14px}label{display:flex;width:100%;margin-right:0}button{max-width:100%;white-space:normal}}
</style></head><body><main>
<a href="/wondrue">Back to Wondrue</a><h1>Canonical handoff demonstration</h1>
<p class="notice"><strong>SYNTHETIC DATA — NOT REAL PROPERTY OR PROFESSIONAL APPROVAL.</strong><br>
Separate demo database and signing key. Shared custody services. No discovery, provider calls, outreach or signed business documents.</p>
<section><label>Wondrue access key<input id="key" type="password" autocomplete="off" spellcheck="false"></label>
<label>Fictional US property<select id="property">__PROPERTIES__</select></label>
<label>Checkpoint test<select id="scenario">__OPTIONS__</select></label>
<label>Demo run key<input id="run" placeholder="Created when you press New scenario" size="40"></label><br>
<button id="new">New scenario</button><button id="step">Advance one checkpoint</button><button id="play">Play to completion</button><button id="inspect">Inspect stored run</button><button id="owner">Exercise owner obligations &amp; protected filing</button><button id="stop">Stop playback</button>
<p>Your access key is held only in this page's memory and is never placed in the run data, URL or browser storage.</p>
<div id="status" role="status" aria-live="polite">Choose a scenario and create a synthetic run.</div></section>
<section><h2>Observable custody timeline</h2><p id="identity">No run loaded.</p><p id="story"></p><p id="owner-status"></p><div class="scroll"><table><thead><tr><th>Step</th><th>Agent</th><th>Checkpoint</th><th>Handoff</th><th>Version</th><th>Reason</th></tr></thead><tbody id="events"></tbody></table></div></section>
<section aria-labelledby="rooms-title"><h2 id="rooms-title">Room and finance comparison</h2>
<p id="room-status">Load a stored run to inspect its room plan.</p>
<div class="scroll"><table><caption>Selected layout per distinct room — alternatives are not added together</caption><thead><tr><th>Room</th><th>Selected layout</th><th>Growing area (m²)</th><th>Estimated crop (kg/year)</th></tr></thead><tbody id="room-rows"></tbody></table></div>
<div class="scroll"><table><caption>Conditional planning estimates in USD</caption><thead><tr><th>Room / scope</th><th>Setup cost</th><th>Annual revenue</th><th>Annual operating costs</th><th>Annual operating surplus</th></tr></thead><tbody id="finance-rows"></tbody></table></div>
<p>These are supplied assumptions and calculated estimates, not verified building capacity or guaranteed returns. Shared-cost allocation is unverified. The combined room plan is separate from single-room alternatives.</p></section>
<section aria-labelledby="lifecycle-title"><h2 id="lifecycle-title">Dated owner lifecycle</h2>
<p>Fictional scenario as-of dates, not a live monitoring log. Reported satisfaction is not verified payment or completion.</p>
<p id="lifecycle-status">No retained dated owner checks loaded.</p>
<div class="scroll"><table><thead><tr><th>Scenario as of</th><th>Obligation / due date</th><th>Recorded state</th><th>Unresolved review tasks</th></tr></thead><tbody id="lifecycle-rows"></tbody></table></div>
<p id="vault-status"></p></section>
<section><details><summary>Full labeled dossier and proof details</summary><pre id="details">No records loaded.</pre></details></section>
</main><script>
'use strict';
const el=id=>document.getElementById(id); let state=null,busy=false,stopped=false;
function controls(value){busy=value;for(const id of ['new','step','play','inspect','owner'])el(id).disabled=value;el('owner').disabled=value||!state||state.phase!=='complete_go'||!state.scenario_id;}
function rowInto(id,values){const row=document.createElement('tr');for(const value of values){const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}el(id).appendChild(row);}
function numberText(value,money=false){if(value===null||value===undefined||typeof value==='boolean'||!['string','number'].includes(typeof value)||String(value).trim()===''||!Number.isFinite(Number(value)))return 'Unknown';return (money?'$':'')+new Intl.NumberFormat('en-US',{minimumFractionDigits:money?2:0,maximumFractionDigits:money?2:3}).format(Number(value));}
function renderSummaries(data,property){
for(const id of ['room-rows','finance-rows','lifecycle-rows'])el(id).replaceChildren();
const reports=property.reports||{},portfolio=reports.onesimus?.room_portfolio,finance=reports.michael?.room_finance;
const rooms=Array.isArray(portfolio?.rooms)?portfolio.rooms:[];
el('room-status').textContent=!rooms.length?'No retained multi-room plan for this run.':portfolio.complete===true?'One selected layout per supplied distinct room. Capacity and room boundaries remain unverified.':'Room plan incomplete; missing inputs or impossible layouts remain unresolved.';
for(const room of rooms){const outputs=room.selected_scenario?.outputs||{};rowInto('room-rows',[room.room_id||'Unknown room',room.selected_config||'No plausible selection',numberText(outputs.growing_m2),numberText(outputs.kg_per_year)]);
const base=finance?.rooms?.[room.room_id]?.cases?.base;const costs=base?.complete===true?base.outputs||{}:{};
rowInto('finance-rows',[room.room_id||'Unknown room',...['capex_usd','revenue_usd_per_year','opex_usd_per_year','operating_net_usd_per_year'].map(k=>numberText(costs[k],true))]);}
if(rooms.length){const totals=portfolio.complete===true?portfolio.selected_room_totals||{}:{};rowInto('room-rows',['Combined selected rooms','One layout per room',numberText(totals.growing_m2),numberText(totals.kg_per_year)]);
const costs=portfolio.complete===true&&finance?.complete===true?finance.totals||{}:{};rowInto('finance-rows',['Combined selected rooms',...['capex_usd','revenue_usd_per_year','opex_usd_per_year','operating_net_usd_per_year'].map(k=>numberText(costs[k],true))]);}
const briefs=Array.isArray(property.owner_briefs)?property.owner_briefs:[];let checks=0;
for(const brief of briefs){const tracking=brief.asset_tracking;if(!tracking?.as_of||!Array.isArray(tracking.obligations))continue;
for(const item of tracking.obligations){const event=item.event||{};let status=event.status==='reported_satisfied'?'Reported satisfied — unverified':event.status==='disputed'?'Disputed — review reopened':event.status==='open'?(item.overdue===true?'Overdue':event.due_on===tracking.as_of?'Due today':'Open'):event.status||'Unknown';
rowInto('lifecycle-rows',[tracking.as_of,`${event.entity_id||'Unknown obligation'} | due ${event.due_on||'Unknown'}`,status,Array.isArray(brief.unresolved_task_keys)?String(brief.unresolved_task_keys.length):'Unknown']);checks++;}}
el('lifecycle-status').textContent=checks?`${checks} retained obligation checks, in recorded order.`:'No retained dated owner checks for this run.';
const vault=Array.isArray(property.owner_vault_entries)?property.owner_vault_entries:[];
el('vault-status').textContent=vault.length?`Protected filing: ${vault.length} immutable snapshot reference(s). Filing date not retained; no date is inferred. Latest snapshot: ${vault[vault.length-1].snapshot_id||'Unknown'}. Later evidence preserves the archived copy: ${data.owner_story?.vault_preserved_after_new_evidence===true?'confirmed by stored synthetic proof':'not established by this run'}.`:'No retained protected filing for this run.';
}
function render(data){state=data;
el('property').value=data.scenario_id||'';el('scenario').disabled=!!data.scenario_id;
const scenario=data.scenario==='all_go'?'':data.scenario;
let option=Array.from(el('scenario').options).find(item=>item.value===scenario);
if(!option){option=document.createElement('option');option.value=scenario;option.textContent=`Historical NO-GO at ${scenario}`;option.disabled=true;el('scenario').appendChild(option);}
el('scenario').value=scenario;el('status').textContent=`${data.phase} | current holder: ${data.current_holder} | verified receipts: ${data.verified_receipts}`;
el('identity').textContent=`SYNTHETIC dossier ${data.dossier_id} | property ${data.property_id}`;el('events').replaceChildren();
const property=data.dossier?.dossier||{};el('story').textContent=property.demo_scenario?`${property.name} | ${property.city}, ${property.state} | ${property.demo_scenario.property_type}. ${property.demo_scenario.story}`:'Generic synthetic checkpoint matrix.';
renderSummaries(data,property);
if(property.food_forest_preview)el('story').textContent+=` Outdoor concept: ${property.food_forest_preview.status}; missing ${property.food_forest_preview.missing.join(', ')}. No outdoor authorization or yield is claimed.`;
el('owner-status').textContent=data.owner_story?`SYNTHETIC owner lifecycle: ${data.owner_story.result}; overdue tasks ${data.owner_story.overdue_task_count}; reopened tasks ${data.owner_story.reopened_task_count}; archived copy preserved ${data.owner_story.vault_preserved_after_new_evidence}.`:data.owner_obligation_seed?'A fictional dated obligation is retained.':'Owner lifecycle becomes available after a GO run reaches Manny.';
for(const event of data.events){const row=document.createElement('tr');for(const value of [event.sequence,event.agent,event.checkpoint||'RETURN',event.to_agent?`${event.agent} → ${event.to_agent}`:'—',event.version||'—',event.reason||'']){const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}row.className=event.checkpoint==='NO-GO'?'nogo':'go';el('events').appendChild(row);}
el('details').textContent=JSON.stringify(data,null,2);}
async function command(intent,args){if(!el('key').value)throw Error('Enter your Wondrue access key.');
const response=await fetch('/wondrue/command',{method:'POST',headers:{'Content-Type':'application/json','X-Wondrue-Key':el('key').value},body:JSON.stringify({mode:intent==='inspect_handoff_demo'?'read-only':'full',target:'demo',use_live:false,intent:{intent,args}})});
const body=await response.json();if(!response.ok)throw Error(body.detail||'Request failed');render(body.data);return body.data;}
async function advance(){return command('advance_handoff_demo',{run_key:el('run').value,expected_sequence:state?state.sequence:0});}
async function perform(task){if(busy)return;controls(true);try{await task();}catch(error){el('status').textContent=error.message;}finally{controls(false);}}
el('new').onclick=()=>perform(async()=>{const token=crypto.randomUUID().replaceAll('-','');el('run').value='demo-handoff-ui-'+token;state=null;await command('start_handoff_demo',{run_key:el('run').value,fail_at:el('property').value?'':el('scenario').value,scenario_id:el('property').value});});
el('property').onchange=()=>{el('scenario').disabled=!!el('property').value;};
el('owner').onclick=()=>perform(()=>command('exercise_demo_obligations',{run_key:el('run').value}));
el('step').onclick=()=>perform(advance);
el('inspect').onclick=()=>perform(()=>command('inspect_handoff_demo',{run_key:el('run').value}));
el('stop').onclick=()=>{stopped=true;el('status').textContent='Playback stopped; any in-flight step may finish. Stored progress is preserved.';};
el('play').onclick=()=>perform(async()=>{stopped=false;if(!state||state.run_key!==el('run').value)await command('inspect_handoff_demo',{run_key:el('run').value});for(let count=0;count<28&&!stopped;count++){if(['complete_go','returned_to_gabriel'].includes(state.phase))break;await advance();await new Promise(resolve=>setTimeout(resolve,450));}});
</script></body></html>'''
