#!/usr/bin/env python3
"""Install the bounded CSR recovery timer; never enables live email delivery."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import os
import socket
import subprocess

SERVICE='''[Unit]
Description=Grow Up bounded customer-service recovery
After=network.target growup.service
[Service]
Type=oneshot
User=growup
Group=growup
WorkingDirectory=/var/www/growup/current
EnvironmentFile=/var/www/growup/shared/growup.env
Environment=PYTHONDONTWRITEBYTECODE=1 OBS_ENABLED=0 META_SPEND_APPROVED=0
ExecStart=/var/www/growup/current/.venv/bin/python -B -m app.csr_worker
TimeoutStartSec=90
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/www/growup/shared
'''
TIMER='''[Unit]
Description=Recover bounded Grow Up customer-service jobs every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
RandomizedDelaySec=3
Unit=growup-csr.service
[Install]
WantedBy=timers.target
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0] not in ('securecode-dev','srv1577410'):
        raise SystemExit('Root on an approved Grow Up server required.')
    if not Path('/var/www/growup/current/app/csr_worker.py').is_file():raise SystemExit('Deploy reviewed CSR code first.')
    if not args.apply:print('PREVIEW: four CSR workers, twenty web jobs/ten email jobs per invocation; no provider enablement.');return
    backup=Path('/var/www/growup/deploy-backups')/('csr-worker-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700)
    for name,text in [('growup-csr.service',SERVICE),('growup-csr.timer',TIMER)]:
        path=Path('/etc/systemd/system')/name
        if path.exists():(backup/name).write_bytes(path.read_bytes())
        path.write_text(text);os.chmod(path,0o644)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','growup-csr.timer'],check=True)
    subprocess.run(['systemctl','start','growup-csr.service'],check=True)
    print('CSR recovery timer installed. Live delivery flags and action mode unchanged. Stop: systemctl disable --now growup-csr.timer')


if __name__=='__main__':main()
