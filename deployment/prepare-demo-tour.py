"""Bounded owner-authorized isolated package preparation and proof."""
from pathlib import Path
import os,socket,subprocess,json,argparse


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='/var/www/growup/current');p.add_argument('--prove',action='store_true');a=p.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':raise SystemExit('Development host only.')
    source=Path(a.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-demo-prep-'):raise SystemExit('Reviewed staging or current development release only.')
    python=source/'.venv/bin/python'
    if not python.is_file():raise SystemExit('Reviewed isolated staging environment required.')
    base=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup','-p','WorkingDirectory='+str(source),
        '-p','EnvironmentFile=/var/www/growup/shared/handoff-demo.env','-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=600',
        '/usr/bin/env','-u','BRAVE_API_KEY','-u','META_API_KEY','-u','BASETEN_API_KEY','-u','OPENAI_API_KEY','MVP_WORKFLOW_ENABLED=1','META_SPEND_APPROVED=0',
        'OBS_ENABLED=0','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),str(python),'-B']
    if a.prove:
        subprocess.run(base+['-m','pytest','tests/test_demo_tour.py','-q','-p','no:cacheprovider','--tb=short'],check=True)
    script="from app.demo_story import build; from app.demo_tour import service; import json; r=build(); t=service(); assert len(t['cases'])==r['executed_scenarios']; assert t['database']=='growup_handoff_demo'; print(json.dumps(r))"
    completed=subprocess.run(base+['-c',script],capture_output=True,text=True)
    if completed.returncode:print(completed.stdout);print(completed.stderr);raise SystemExit('STOP: isolated proof failed; retained synthetic history is preserved. No operational fallback.')
    result=json.loads(completed.stdout);assert result['result']=='PASS' and result['operational_writes']==0 and result['external_actions']==0
    proof=Path('/home/ubuntu/growup-demo-tour-proof.json');proof.write_text(json.dumps(result,indent=2));os.chmod(proof,0o644)
    print(json.dumps(result,indent=2));print('Proof: '+str(proof));print('Open https://growup.local:8443/wondrue/demo-tour')


if __name__=='__main__':main()
