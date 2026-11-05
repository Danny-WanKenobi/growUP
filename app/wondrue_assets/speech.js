'use strict';
// Local Zira synthesis and user-started browser dictation; no paid adapter.
(()=>{
 const synth=window.speechSynthesis;
 const status=document.getElementById('wondrue-speech-status');let voices=[],generation=0;
 const tell=text=>{status.textContent=text;};
 function stop(){generation++;if(synth)synth.cancel();tell('Speech stopped.');}
 function refresh(){
  voices=synth?synth.getVoices().filter(v=>v.localService&&/Microsoft Zira/i.test(v.name)):[];
  if(!voices.length)tell('Microsoft Zira is unavailable in this browser. Written replies remain available.');
 }
 function speak(text){
  stop();refresh();const voice=voices[0];
  if(!synth||!voice||typeof text!=='string'||!text.trim()){tell('Microsoft Zira is unavailable in this browser. Written replies remain available.');return false;}
  const token=generation,parts=text.slice(0,12000).match(/[\s\S]{1,1500}/g)||[];
  function next(index){if(token!==generation)return;if(index>=parts.length){tell('Finished speaking.');return;}
   const utterance=new SpeechSynthesisUtterance(parts[index]);utterance.voice=voice;utterance.lang=voice.lang;utterance.rate=.95;
   utterance.onend=()=>next(index+1);utterance.onerror=()=>{if(token===generation)tell('Speech unavailable. Written reply remains available.');};
   tell('Wondrue is speaking.');synth.speak(utterance);
  }next(0);return true;
 }
 let recognition=null;
 const microphone=document.getElementById('wondrue-microphone');
 function stopListening(){if(recognition){recognition.abort();recognition=null;}microphone.textContent='Microphone';microphone.setAttribute('aria-pressed','false');}
 microphone.onclick=()=>{
  if(recognition){stopListening();tell('Dictation stopped.');return;}
  const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!Recognition){tell('Browser dictation unavailable. Use Windows voice typing: click the message box and press Windows+H.');return;}
  stop();const session=new Recognition();recognition=session;session.lang='en-US';session.continuous=false;session.interimResults=false;
  const prompt=document.getElementById('prompt'),original=prompt.value;
  session.onresult=event=>{if(recognition!==session)return;let text='';for(let i=event.resultIndex||0;i<event.results.length;i++)if(event.results[i].isFinal)text+=event.results[i][0].transcript+' ';
   if(text.trim()){prompt.value=(original+(original?' ':'')+text.trim()).slice(0,4000);prompt.focus();tell('Dictation added. Review your message, then press Send.');}};
  session.onerror=event=>{if(recognition!==session)return;tell(event.error==='not-allowed'?'Microphone blocked. Allow microphone access in the address-bar site settings, then retry.':'Dictation unavailable ('+event.error+'). Click the message box and use Windows+H.');};
  session.onend=()=>{if(recognition===session){recognition=null;microphone.textContent='Microphone';microphone.setAttribute('aria-pressed','false');}};
  try{session.start();microphone.textContent='Stop microphone';microphone.setAttribute('aria-pressed','true');tell('Listening. Your browser may process speech online.');}catch{stopListening();tell('Could not start dictation. Click the message box and use Windows+H.');}
 };
 window.WondrueSpeech={speak,stop:()=>{stop();stopListening();}};
 document.getElementById('wondrue-stop-speaking').onclick=stop;
 document.getElementById('wondrue-read-aloud').onchange=event=>{if(!event.target.checked)stop();};
 window.addEventListener('pagehide',()=>{stop();stopListening();});
 if(synth)synth.addEventListener('voiceschanged',refresh);refresh();
})();
