"""Focused development PostgreSQL proof; all external traffic denied."""
import argparse
import os
from pathlib import Path
import socket
import subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='/var/www/growup/current')
    source=Path(parser.parse_args().source).resolve(strict=True)
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':
        raise SystemExit('Development host and local sudo required.')
    active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-visual-review-'):
        raise SystemExit('Active development release or reviewed visual staging required.')
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/growup.env',
        '-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=180','/usr/bin/env',
        'DATABASE_NAME=growup_mvp_gate','GROWUP_PG_INTEGRATION=1','OBS_ENABLED=0',
        'PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),'GABRIEL_IMAGE_CACHE_ENABLED=0',
        str(source/'.venv/bin/python'),'-B','-m','pytest','tests/test_outreach_visuals.py',
        '-q','-p','no:cacheprovider','--tb=short','--maxfail=1']
    print('Dedicated growup_mvp_gate; mocked image transport; no source acquisition, Canva, Resend or full suite.',flush=True)
    subprocess.run(command,check=True)
    print('PASS: photo compression/isolation, durable pre-request charging/replay, dossier binding and illustrated package.')


if __name__=='__main__':main()
