"""Focused PostgreSQL specialist-chat/channel proof; no providers or full suite."""
from pathlib import Path
import argparse,os,socket,subprocess


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',default='/var/www/growup/current');a=p.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':raise SystemExit('Development host only.')
    source=Path(a.source).resolve(strict=True);active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-specialist-review-'):raise SystemExit('Reviewed staging or active development release only.')
    python=source/'.venv/bin/python'
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup','-p','WorkingDirectory='+str(source),
        '-p','EnvironmentFile=/var/www/growup/shared/growup.env','-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=180',
        '/usr/bin/env','DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','DOSSIER_COORDINATOR_EMERGENCY_STOP=0','OBS_ENABLED=0',
        'PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),str(python),'-B','-m','pytest','tests/test_specialist_chat.py','tests/test_customer_service.py',
        '-k','test_pg_','-q','-p','no:cacheprovider','--tb=short','--maxfail=1']
    print('Dedicated growup_mvp_gate only; external network blocked; no emails, calls or full regression.',flush=True)
    subprocess.run(command,check=True)
    print('PASS: bounded parallel conversations, ordered handover, interrupted-write rollback, replay and scoped history.')


if __name__=='__main__':main()
