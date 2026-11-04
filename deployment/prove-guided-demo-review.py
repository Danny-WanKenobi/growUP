#!/usr/bin/env python3
"""Bounded review proof on the existing isolated synthetic database only."""
from pathlib import Path
import argparse
import os
import socket
import subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='/var/www/growup/current');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':
        raise SystemExit('Run with sudo on securecode-dev only.')
    source=Path(args.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-guided-review-'):
        raise SystemExit('Reviewed staging source or active development release required.')
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/handoff-demo.env',
        '-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=180',
        '/usr/bin/env','MVP_WORKFLOW_ENABLED=1','META_SPEND_APPROVED=0','OBS_ENABLED=0',
        'GROWUP_GUIDED_REVIEW_PG=1','GROWUP_PG_INTEGRATION=0','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),
        str(active/'.venv/bin/python'),'-B','-m','pytest','tests/test_guided_demo_review.py','-q','-p','no:cacheprovider','--tb=short','--maxfail=1']
    print('Isolated guided review: growup_handoff_demo / growup_demo only; external network blocked.',flush=True)
    subprocess.run(command,check=True)
    print('PASS: explicit approval, canonical linked reassessment, needs-evidence return, custody verification, interruption rollback and concurrent resume. No operational writes or provider calls.')


if __name__=='__main__':main()
