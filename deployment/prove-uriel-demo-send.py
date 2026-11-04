#!/usr/bin/env python3
"""Owner console: isolated PG/mock proof and preview, optional one-shot email."""
from pathlib import Path
import argparse
import os
import socket
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--send',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':
        raise SystemExit('Run with local sudo on securecode-dev only.')
    source=Path(__file__).resolve().parent.parent
    release=Path('/var/www/growup/current').resolve(strict=True)
    if release.parent!=Path('/var/www/growup/releases'): raise SystemExit('Recognized development release required.')
    base=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/growup.env',
        '-p','EnvironmentFile=/var/www/growup/shared/handoff-demo.env','-p','TimeoutStartSec=120']
    env=['/usr/bin/env','-u','BRAVE_API_KEY','-u','META_API_KEY','-u','BASETEN_API_KEY',
        'PYTHONPATH='+str(source),'PYTHONDONTWRITEBYTECODE=1','OBS_ENABLED=0','META_SPEND_APPROVED=0',
        'DEMO_MODE=yes','RESEND_TRACKING_ENABLED=0','RESEND_POLL_ENABLED=0']
    python=str(release/'.venv/bin/python')
    print('Isolated demo DB/mock proof; no real email in this test. Application release unchanged.',flush=True)
    subprocess.run(base+['-p','IPAddressDeny=any','-p','IPAddressAllow=localhost']+env+
        ['URIEL_DEMO_PG_PROOF=1',python,'-B','-m','pytest',
         'tests/test_uriel_demo_send.py::test_pg_claim_commits_before_transport_and_blocks_concurrent_replay',
         '-q','-p','no:cacheprovider','--tb=short','--maxfail=1'],check=True)
    command=[python,'-B','-m','app.uriel_demo_send',
        '--run-key','demo-handoff-us-20261104T061029Z-us-hudson-sky',
        '--start-at','2026-11-05T10:00:00-06:00','--timezone','America/Chicago']
    subprocess.run(base+['-p','IPAddressDeny=any','-p','IPAddressAllow=localhost']+env+command,check=True)
    if args.send:
        print('ONE authorized real review email to werenotrandom@protonmail.com; no other outreach. No automatic retries.',flush=True)
        subprocess.run(base+env+['URIEL_DEMO_SEND_APPROVED=1',
            'URIEL_DEMO_SEND_AUTHORIZATION=owner-uriel-demo-20261104-v1']+command+['--send'],check=True)
    else:
        print('PREVIEW COMPLETE. No real provider calls or emails. Add --send only for the approved one-shot delivery.',flush=True)


if __name__=='__main__': main()
