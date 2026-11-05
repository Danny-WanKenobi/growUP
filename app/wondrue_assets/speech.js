'use strict';
// Local Zira synthesis and user-started browser dictation; no paid adapter.
(()=>{
 const synth=window.speechSynthesis;
 const status=document.getElementById('wondrue-speech-status');let voices=[],generation=0,speaking=false;
 const tell=text=>{status.textContent=text;};
 function stop(){generation++;speaking=false;if(synth)synth.cancel();updateControl();tell('Speech stopped.');}
 function refresh(){
  voices=synth?synth.getVoices().filter(v=>v.localService&&/Microsoft Zira/i.test(v.name)):[];
  if(!voices.length)tell('Microsoft Zira is unavailable in this browser. Written replies remain available.');
 }
 function speak(text){
  stop();refresh();const voice=voices[0];
  if(!synth||!voice||typeof text!=='string'||!text.trim()){tell('Microsoft Zira is unavailable in this browser. Written replies remain available.');return false;}
  const token=generation,parts=text.slice(0,12000).match(/[\s\S]{1,1500}/g)||[];
  function next(index){if(token!==generation)return;if(index>=parts.length){speaking=false;updateControl();tell('Finished speaking.');return;}
   const utterance=new SpeechSynthesisUtterance(parts[index]);utterance.voice=voice;utterance.lang=voice.lang;utterance.rate=.95;
   utterance.onend=()=>next(index+1);utterance.onerror=()=>{if(token===generation){speaking=false;updateControl();tell('Speech unavailable. Written reply remains available.');}};
   speaking=true;updateControl();tell('Wondrue is speaking.');synth.speak(utterance);
  }next(0);return true;
 }
 let recognition=null;
 const microphone=document.getElementById('send'),prompt=document.getElementById('prompt');
 function updateControl(){
  if(!microphone)return;
  const recording=Boolean(recognition),hasText=Boolean(prompt.value.trim());
  microphone.textContent=recording||speaking&&!hasText?'■':hasText?'↑':'🎙';
  microphone.setAttribute('aria-label',recording?'Finish dictation':hasText?'Send message':speaking?'Stop speaking':'Start microphone');
  microphone.setAttribute('aria-pressed',String(recording));
  microphone.title=recording?'Finish dictation':hasText?'Send message':speaking?'Stop speaking':'Dictate; your browser may process audio online';
 }
 function stopListening(){const old=recognition;recognition=null;if(old)old.abort();updateControl();}
 microphone.addEventListener('click',event=>{
  if(recognition){event.preventDefault();try{recognition.stop();tell('Finishing dictation…');}catch{stopListening();}return;}
  if(prompt.value.trim()){stop();return;}
  event.preventDefault();
  if(speaking){stop();return;}
  const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!Recognition){tell('Browser dictation unavailable. Click the message box and use Windows+H.');return;}
  stop();const session=new Recognition();recognition=session;session.lang='en-US';session.continuous=false;session.interimResults=true;
  const original=prompt.value;
  session.onresult=event=>{if(recognition!==session)return;let text='';for(let i=0;i<event.results.length;i++)text+=event.results[i][0].transcript+' ';
   prompt.value=(original+(original?' ':'')+text.trim()).slice(0,4000);updateControl();};
  session.onerror=event=>{if(recognition!==session)return;tell(event.error==='not-allowed'?'Allow microphone access in the address-bar site settings, then retry.':'Dictation unavailable ('+event.error+'). Use Windows+H in the message box.');};
  session.onend=()=>{if(recognition===session){recognition=null;updateControl();if(prompt.value.trim())tell('Review your words. Click ↑ to send.');}};
  try{session.start();updateControl();tell('Listening… click ■ when finished. Browser speech processing may be online.');}catch{stopListening();tell('Could not start dictation. Use Windows+H in the message box.');}
 });
 prompt.addEventListener('input',updateControl);
 document.getElementById('composer').addEventListener('submit',()=>{stopListening();setTimeout(updateControl,0);});
 window.WondrueSpeech={speak,stop:()=>{stop();stopListening();},refreshControl:updateControl};
 updateControl();
 window.addEventListener('pagehide',()=>{stop();stopListening();});
 if(synth)synth.addEventListener('voiceschanged',refresh);refresh();
})();
