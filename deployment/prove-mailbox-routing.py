#!/usr/bin/env python3
"""Owner-run, predeployment PostgreSQL proof using a staged source snapshot."""
from pathlib import Path
import os
import socket
import subprocess


def main():
    if os.geteuid() != 0 or socket.gethostname().split('.')[0] != 'securecode-dev':
        raise SystemExit('Run with local sudo on securecode-dev only.')
    source = Path(__file__).resolve().parent.parent
    if not (source / 'tests/test_mailbox_routing.py').is_file():
        raise SystemExit('Complete staged source required.')
    release = Path('/var/www/growup/current').resolve(strict=True)
    if release.parent != Path('/var/www/growup/releases'):
        raise SystemExit('Recognized development release required.')
    print('Staged mailbox proof only: growup_mvp_gate; application release unchanged; external network blocked.', flush=True)
    command = ['systemd-run', '--wait', '--pipe', '--collect', '--quiet',
        '-p', 'User=growup', '-p', 'Group=growup', '-p', 'WorkingDirectory=' + str(source),
        '-p', 'EnvironmentFile=/var/www/growup/shared/growup.env',
        '-p', 'IPAddressDeny=any', '-p', 'IPAddressAllow=localhost', '-p', 'TimeoutStartSec=120',
        '/usr/bin/env', '-u', 'BRAVE_API_KEY', '-u', 'META_API_KEY', '-u', 'BASETEN_API_KEY',
        '-u', 'RESEND_API_KEY', '-u', 'RESEND_WEBHOOK_SECRET',
        'PYTHONPATH=' + str(source), 'PYTHONDONTWRITEBYTECODE=1', 'OBS_ENABLED=0',
        'DATABASE_HOST=127.0.0.1', 'DATABASE_NAME=growup_mvp_gate', 'GROWUP_PG_INTEGRATION=1',
        'META_SPEND_APPROVED=0', 'RESEND_POLL_ENABLED=0',
        str(release / '.venv/bin/python'), '-B', '-m', 'pytest',
        'tests/test_mailbox_routing.py::test_pg_direct_mailbox_persistence_concurrency_and_atomic_rollback',
        '-q', '-p', 'no:cacheprovider', '--tb=short', '--maxfail=1']
    subprocess.run(command, check=True)
    print('MAILBOX POSTGRESQL PROOF PASSED. No deployment, migration, provider call or email.', flush=True)
    print('Fresh UUID test records and audit history remain only in growup_mvp_gate.', flush=True)


if __name__ == '__main__':
    main()
