"""Credential-free page shell; all data requests use Wondrue authentication."""
import html
from app.handoff_demo import STAGES


def page():
    options = '<option value="">All checkpoints GO</option>' + ''.join(
        f'<option value="{html.escape(a)}">NO-GO at {html.escape(a)}</option>' for a in STAGES)
    return TEMPLATE.replace("__OPTIONS__", options)


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
<label>Scenario<select id="scenario">__OPTIONS__</select></label>
<label>Demo run key<input id="run" placeholder="Created when you press New scenario" size="40"></label><br>
<button id="new">New scenario</button><button id="step">Advance one checkpoint</button><button id="play">Play to completion</button><button id="inspect">Inspect stored run</button><button id="stop">Stop playback</button>
<p>Your access key is held only in this page's memory and is never placed in the run data, URL or browser storage.</p>
<div id="status" role="status" aria-live="polite">Choose a scenario and create a synthetic run.</div></section>
<section><h2>Observable custody timeline</h2><p id="identity">No run loaded.</p><div class="scroll"><table><thead><tr><th>Step</th><th>Agent</th><th>Checkpoint</th><th>Handoff</th><th>Version</th><th>Reason</th></tr></thead><tbody id="events"></tbody></table></div></section>
<section><details><summary>Full labeled dossier and proof details</summary><pre id="details">No records loaded.</pre></details></section>
</main><script>
'use strict';
const el=id=>document.getElementById(id); let state=null,busy=false,stopped=false;
function controls(value){busy=value;for(const id of ['new','step','play','inspect'])el(id).disabled=value;}
function render(data){state=data;
const scenario=data.scenario==='all_go'?'':data.scenario;
let option=Array.from(el('scenario').options).find(item=>item.value===scenario);
if(!option){option=document.createElement('option');option.value=scenario;option.textContent=`Historical NO-GO at ${scenario}`;option.disabled=true;el('scenario').appendChild(option);}
el('scenario').value=scenario;el('status').textContent=`${data.phase} | current holder: ${data.current_holder} | verified receipts: ${data.verified_receipts}`;
el('identity').textContent=`SYNTHETIC dossier ${data.dossier_id} | property ${data.property_id}`;el('events').replaceChildren();
for(const event of data.events){const row=document.createElement('tr');for(const value of [event.sequence,event.agent,event.checkpoint||'RETURN',event.to_agent?`${event.agent} → ${event.to_agent}`:'—',event.version||'—',event.reason||'']){const cell=document.createElement('td');cell.textContent=value;row.appendChild(cell);}row.className=event.checkpoint==='NO-GO'?'nogo':'go';el('events').appendChild(row);}
el('details').textContent=JSON.stringify(data,null,2);}
async function command(intent,args){if(!el('key').value)throw Error('Enter your Wondrue access key.');
const response=await fetch('/wondrue/command',{method:'POST',headers:{'Content-Type':'application/json','X-Wondrue-Key':el('key').value},body:JSON.stringify({mode:intent==='inspect_handoff_demo'?'read-only':'full',target:'demo',use_live:false,intent:{intent,args}})});
const body=await response.json();if(!response.ok)throw Error(body.detail||'Request failed');render(body.data);return body.data;}
async function advance(){return command('advance_handoff_demo',{run_key:el('run').value,expected_sequence:state?state.sequence:0});}
async function perform(task){if(busy)return;controls(true);try{await task();}catch(error){el('status').textContent=error.message;}finally{controls(false);}}
el('new').onclick=()=>perform(async()=>{const token=crypto.randomUUID().replaceAll('-','');el('run').value='demo-handoff-ui-'+token;state=null;await command('start_handoff_demo',{run_key:el('run').value,fail_at:el('scenario').value});});
el('step').onclick=()=>perform(advance);
el('inspect').onclick=()=>perform(()=>command('inspect_handoff_demo',{run_key:el('run').value}));
el('stop').onclick=()=>{stopped=true;el('status').textContent='Playback stopped; any in-flight step may finish. Stored progress is preserved.';};
el('play').onclick=()=>perform(async()=>{stopped=false;if(!state||state.run_key!==el('run').value)await command('inspect_handoff_demo',{run_key:el('run').value});for(let count=0;count<28&&!stopped;count++){if(['complete_go','returned_to_gabriel'].includes(state.phase))break;await advance();await new Promise(resolve=>setTimeout(resolve,450));}});
</script></body></html>'''
