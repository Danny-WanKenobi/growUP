const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function setup(voices,Recognition){
 const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,{value:'',textContent:'',append(){},replaceChildren(){},setAttribute(){},focus(){}});return elements.get(id);};
 let cancellations=0;const spoken=[];const handlers={};
 const synth={getVoices:()=>voices,cancel:()=>cancellations++,speak:u=>spoken.push(u),addEventListener:()=>{}};
 const window={SpeechRecognition:Recognition,speechSynthesis:synth,addEventListener:(key,fn)=>handlers[key]=fn};
 vm.runInNewContext(fs.readFileSync('app/wondrue_assets/speech.js','utf8'),{window,document:{getElementById:get,createElement:()=>({})},SpeechSynthesisUtterance:function(text){this.text=text;}});
 return {window,spoken,get,handlers,cancellations:()=>cancellations};
}
test('real cockpit speaks using a local voice, chunks and stops queued continuation',()=>{
 const voice={voiceURI:'local',localService:true,name:'Microsoft Zira',lang:'en-US'},s=setup([voice]);
 assert.equal(s.window.WondrueSpeech.speak('a'.repeat(2000)),true);
 assert.equal(s.spoken[0].voice,voice);assert.equal(s.spoken[0].text.length,1500);
 s.spoken[0].onend();assert.equal(s.spoken.length,2);
 s.window.WondrueSpeech.stop();s.spoken[1].onend();assert.equal(s.spoken.length,2);
 s.handlers.pagehide();assert.ok(s.cancellations()>=3);
});
test('remote-only voices never speak and written fallback stays available',()=>{
 const s=setup([{voiceURI:'remote',localService:false,name:'Remote'}]);
 assert.equal(s.window.WondrueSpeech.speak('Hello'),false);assert.equal(s.spoken.length,0);
 assert.match(s.get('wondrue-speech-status').textContent,/Microsoft Zira is unavailable/);
});

test('fixed Zira never substitutes another local voice',()=>{
 const s=setup([{voiceURI:'other',localService:true,name:'Microsoft David'}]);
 assert.equal(s.window.WondrueSpeech.speak('Hello'),false);assert.equal(s.spoken.length,0);
});
test('microphone retains dictation for review without sending and can be stopped',()=>{
 let session,aborted=false;
 function Recognition(){session=this;this.start=()=>{};this.abort=()=>{aborted=true;};}
 const s=setup([],Recognition);s.get('prompt').value='Existing';s.get('wondrue-microphone').onclick();
 session.onresult({resultIndex:0,results:[Object.assign([{transcript:'spoken words'}],{isFinal:true})]});
 assert.equal(s.get('prompt').value,'Existing spoken words');
 assert.match(s.get('wondrue-speech-status').textContent,/Review your message/);
 s.get('wondrue-microphone').onclick();assert.equal(aborted,true);
});
test('unsupported microphone provides Windows voice typing fallback',()=>{
 const s=setup([]);s.get('wondrue-microphone').onclick();
 assert.match(s.get('wondrue-speech-status').textContent,/Windows\+H/);
});
