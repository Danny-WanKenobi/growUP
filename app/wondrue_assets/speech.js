'use strict';
// Local browser synthesis only: no paid adapter, microphone or remote voice.
(()=>{
 const synth=window.speechSynthesis,select=document.getElementById('wondrue-voice');
 const status=document.getElementById('wondrue-speech-status');let voices=[],generation=0;
 const tell=text=>{status.textContent=text;};
 function stop(){generation++;if(synth)synth.cancel();tell('Speech stopped.');}
 function refresh(){
  const previous=select.value;voices=synth?synth.getVoices().filter(v=>v.localService):[];
  select.replaceChildren();
  for(const v of voices){const option=document.createElement('option');option.value=v.voiceURI;option.textContent=v.name+' · local';select.append(option);}
  if(voices.some(v=>v.voiceURI===previous))select.value=previous;
  else{const preferred=voices.find(v=>/Zira|Samantha|Aria|Jenny/.test(v.name))||voices.find(v=>/^en/i.test(v.lang))||voices[0];if(preferred)select.value=preferred.voiceURI;}
  if(!voices.length)tell('No local voice available. Written replies remain available.');
 }
 function speak(text){
  stop();refresh();const voice=voices.find(v=>v.voiceURI===select.value);
  if(!synth||!voice||typeof text!=='string'||!text.trim()){tell('No local voice available. Written replies remain available.');return false;}
  const token=generation,parts=text.slice(0,12000).match(/[\s\S]{1,1500}/g)||[];
  function next(index){if(token!==generation)return;if(index>=parts.length){tell('Finished speaking.');return;}
   const utterance=new SpeechSynthesisUtterance(parts[index]);utterance.voice=voice;utterance.lang=voice.lang;utterance.rate=.95;
   utterance.onend=()=>next(index+1);utterance.onerror=()=>{if(token===generation)tell('Speech unavailable. Written reply remains available.');};
   tell('Wondrue is speaking.');synth.speak(utterance);
  }next(0);return true;
 }
 window.WondrueSpeech={speak,stop};
 document.getElementById('wondrue-stop-speaking').onclick=stop;
 document.getElementById('wondrue-read-aloud').onchange=event=>{if(!event.target.checked)stop();};
 window.addEventListener('pagehide',stop);
 if(synth)synth.addEventListener('voiceschanged',refresh);refresh();
})();
