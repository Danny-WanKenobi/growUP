#!/usr/bin/env python3
"""Selected evidence/rework PostgreSQL proof. No operational writes or providers."""
from pathlib import Path
import argparse,os,socket,subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='/var/www/growup/current');a=p.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':raise SystemExit('Development host only.')
    source=Path(a.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-milestones-review-'):raise SystemExit('Approved staging source or active release only.')
    python=source/'.venv/bin/python' if source!=active else active/'.venv/bin/python'
    if not python.is_file():raise SystemExit('Prepare the reviewed staging environment first.')
    print('Targeted milestone proof: growup_mvp_gate; external network blocked; operational dossiers untouched.',flush=True)
    subprocess.run(['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup','-p','WorkingDirectory='+str(source),
        '-p','EnvironmentFile=/var/www/growup/shared/growup.env','-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=180',
        '/usr/bin/env','DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','MVP_WORKFLOW_ENABLED=1','DOSSIER_COORDINATOR_EMERGENCY_STOP=0',
        'OBS_ENABLED=0','PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),str(python),'-B','-m','pytest','tests/test_owner_milestones.py',
        '-q','-p','no:cacheprovider','--tb=short','--maxfail=1','-k','test_pg_'],check=True)
    print('PASS: real PostgreSQL evidence links, source-bound reassessment, interrupted refresh rollback and ordered recovery. No real-property authorization is claimed.')


if __name__=='__main__':main()
