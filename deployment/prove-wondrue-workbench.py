#!/usr/bin/env python3
"""Three isolated targeted proofs. No application deployment or providers."""
from pathlib import Path
import argparse,os,socket,subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='/var/www/growup/current');parser.add_argument('--proof',choices=['all','gate','demo','sandbox'],default='all');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':raise SystemExit('Run with sudo on securecode-dev only.')
    source=Path(args.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-workbench-review-'):raise SystemExit('Reviewed staging source or active development release required.')
    for name,label,envs,flags,selection in (
        ('gate','Gate contract and private read views',['growup.env'],['DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','GROWUP_GUIDED_REVIEW_PG=0'],'pg_retained'),
        ('demo','Isolated synthetic lifecycle and interruption',['handoff-demo.env'],['GROWUP_PG_INTEGRATION=0','GROWUP_GUIDED_REVIEW_PG=1'],'pg_rehearsal'),
        ('sandbox','Isolated named alternatives',['wondrue-sandbox.env'],['GROWUP_PG_INTEGRATION=0','GROWUP_GUIDED_REVIEW_PG=0','GROWUP_SANDBOX_PROOF=1'],'pg_named')):
        if args.proof not in ('all',name):continue
        print(label+'; no operational business writes or external network.',flush=True)
        command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
            '-p','WorkingDirectory='+str(source),'-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=240']
        for env in envs:command+=['-p','EnvironmentFile=/var/www/growup/shared/'+env]
        command+=['/usr/bin/env','MVP_WORKFLOW_ENABLED=1','META_SPEND_APPROVED=0','OBS_ENABLED=0',
            'DOSSIER_COORDINATOR_EMERGENCY_STOP=0','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source)]+flags+[
            str(active/'.venv/bin/python'),'-B','-m','pytest','tests/test_wondrue_workbench.py','-q','-p','no:cacheprovider','--tb=short','--maxfail=1','-k',selection]
        subprocess.run(command,check=True)
    print('PASS: requested isolated PostgreSQL proofs. Existing operational records and application release unchanged.')


if __name__=='__main__':main()
