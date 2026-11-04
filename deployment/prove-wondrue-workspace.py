#!/usr/bin/env python3
"""Focused offline PostgreSQL cockpit proof; never runs against operational data."""
from pathlib import Path
import argparse
import os
import socket
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default='/var/www/growup/current')
    args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':
        raise SystemExit('Run with sudo on securecode-dev only.')
    source=Path(args.source).resolve(strict=True)
    active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-wondrue-review-'):
        raise SystemExit('Reviewed staging directory or active development release required.')
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
             '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/growup.env',
             '-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=120',
             '/usr/bin/env','-u','BRAVE_API_KEY','-u','META_API_KEY','-u','BASETEN_API_KEY','-u','RESEND_API_KEY',
             'DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','PYTHONDONTWRITEBYTECODE=1',
             'OBS_ENABLED=0','META_SPEND_APPROVED=0','PYTHONPATH='+str(source),
             str(active/'.venv/bin/python'),'-B','-m','pytest','tests/test_wondrue_workspace.py',
             '-q','-p','no:cacheprovider','--tb=short','--maxfail=1']
    print('Focused cockpit proof: growup_mvp_gate only; external network blocked; active release unchanged.',flush=True)
    subprocess.run(command,check=True)
    print('PASS. Disposable chat/review fixtures retained in growup_mvp_gate; no property acquisition or external actions.')


if __name__=='__main__':main()
