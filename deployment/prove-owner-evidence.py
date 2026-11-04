#!/usr/bin/env python3
"""Targeted private attachment proof on the dedicated development gate only."""
from pathlib import Path
import argparse,os,socket,subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='/var/www/growup/current');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':raise SystemExit('Run with sudo on securecode-dev only.')
    source=Path(args.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-evidence-review-'):raise SystemExit('Reviewed staging source or active development release required.')
    print('Attachment PostgreSQL proof: growup_mvp_gate only; external network blocked; active release unchanged.',flush=True)
    subprocess.run(['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/growup.env',
        '-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=120',
        '/usr/bin/env','DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','MVP_WORKFLOW_ENABLED=1',
        'OBS_ENABLED=0','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),
        str(active/'.venv/bin/python'),'-B','-m','pytest','tests/test_owner_evidence.py','-q','-p','no:cacheprovider',
        '--tb=short','--maxfail=1','-k','test_pg_'],check=True)
    print('PASS: immutable bytes, duplicate contention, interrupted-save rollback and unchanged dossier/custody. No operational business writes or providers.')


if __name__=='__main__':main()
