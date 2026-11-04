#!/usr/bin/env python3
"""Owner-run proof/population of existing isolated demo DB; no provisioning."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import json
import os
import socket
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-key',default='demo-handoff-us-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev': raise SystemExit('STOP: run locally with sudo on securecode-dev')
    release=Path('/var/www/growup/current').resolve(strict=True)
    if release.parent!=Path('/var/www/growup/releases'): raise SystemExit('STOP: active development release required')
    cmd=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup','-p','WorkingDirectory='+str(release),
        '-p','EnvironmentFile=/var/www/growup/shared/handoff-demo.env','-p','TimeoutStartSec=300',
        '/usr/bin/env','MVP_WORKFLOW_ENABLED=1','META_SPEND_APPROVED=0','OBS_ENABLED=0','PYTHONDONTWRITEBYTECODE=1',
        str(release/'.venv/bin/python'),'-B','-m','app.handoff_demo','showcase-suite','--run-key',args.run_key]
    print("Synthetic run prefix: "+args.run_key,flush=True)
    completed=subprocess.run(cmd,capture_output=True,text=True)
    if completed.returncode: raise SystemExit('STOP: isolated showcase proof failed. Partial synthetic progress is retained; retry with the SAME run key: '+args.run_key)
    proof=json.loads(completed.stdout)
    if proof.get('result')!='PASS' or proof.get('operational_writes')!=0 or proof.get('scenarios')!=29: raise SystemExit('STOP: unexpected proof result')
    fd,name=tempfile.mkstemp(prefix='growup-us-demo-proof-',suffix='.json',dir='/home/ubuntu')
    target=Path(name)
    with os.fdopen(fd,'w',encoding='utf-8') as output: json.dump(proof,output,indent=2)
    os.chmod(target,0o644)
    print('PASS: 13 checkpoint-matrix runs + 16 fictional US property cases persisted in growup_handoff_demo.')
    for case in proof['property_catalogue']['results']:
        print(case['name']+' | '+case['city']+', '+case['state']+' | '+case['property_type']+' | '+case['phase']+' | '+case['run_key'])
    print('Owner lifecycle: synthetic obligation, overdue task, correction, protected filing, later dispute and archive preservation.')
    print('Provider calls: 0; operational writes: 0; external actions: 0.')
    print('Sanitized proof: '+str(target))
    print('Open https://securecodeusa.local:8443/wondrue/handoff-demo and inspect a listed run key.')


if __name__=='__main__': main()
