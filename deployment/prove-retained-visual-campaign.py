"""One approved existing synthetic source; isolated DB, no provider or send access."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='/var/www/growup/current')
    args=parser.parse_args();source=Path(args.source).resolve(strict=True)
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='securecode-dev':
        raise SystemExit('Development host and local sudo required.')
    active=Path('/var/www/growup/current').resolve(strict=True)
    if source!=active and not str(source).startswith('/tmp/growup-visual-proof-'):
        raise SystemExit('Active development release or reviewed staging required.')
    output=Path('/var/www/growup/validation/retained-visual-campaign')
    if output.is_symlink() or output.resolve().parent!=Path('/var/www/growup/validation').resolve():
        raise SystemExit('Private proof output path is not the expected directory.')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    account=pwd.getpwnam('growup');os.chown(output,account.pw_uid,account.pw_gid)
    command=['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(source),'-p','EnvironmentFile=/var/www/growup/shared/handoff-demo.env',
        '-p','IPAddressDeny=any','-p','IPAddressAllow=localhost','-p','TimeoutStartSec=180',
        '/usr/bin/env','-u','RESEND_API_KEY','-u','RESEND_RECEIVING_API_KEY','-u','BRAVE_SEARCH_API_KEY',
        '-u','META_API_KEY','-u','BASETEN_API_KEY','URIEL_DEMO_SEND_APPROVED=0','RESEND_POLL_ENABLED=0',
        'PYTHONDONTWRITEBYTECODE=1','PYTHONPATH='+str(source),'OBS_ENABLED=0',
        str(source/'.venv/bin/python'),'-B','-m','app.visual_campaign_proof','--output',str(output)]
    completed=subprocess.run(command,capture_output=True,text=True)
    if completed.returncode:
        print(completed.stdout);print(completed.stderr)
        raise SystemExit('STOP: retained-source proof failed; preserve output and historical records.')
    result=json.loads(completed.stdout)
    # An independent repeat must reuse the same committed proof. The module
    # re-verifies current source custody and exact dossier/report digests.
    repeated=subprocess.run(command,capture_output=True,text=True,check=True)
    replay=json.loads(repeated.stdout)
    if not replay['replayed'] or result['artifact_digest']!=replay['artifact_digest']:
        raise SystemExit('STOP: exact proof replay failed.')
    owner=pwd.getpwnam('ubuntu')
    review=Path('/home/ubuntu')/('growup-hudson-visual-review-'+result['artifact_digest'][:12])
    if review.is_symlink():raise SystemExit('Owner review path must not be a symlink.')
    if any(path.is_symlink() for path in output.rglob('*')):
        raise SystemExit('Proof output must not contain symlinks.')
    shutil.copytree(output,review,symlinks=True,dirs_exist_ok=True)
    for path in [review,*review.rglob('*')]:
        if path.is_symlink():raise SystemExit('Owner review must not contain symlinks.')
        os.chown(path,owner.pw_uid,owner.pw_gid,follow_symlinks=False);os.chmod(path,0o700 if path.is_dir() else 0o600)
    print(json.dumps({**result,'exact_replay_verified':True,'review_directory':str(review)},indent=2))


if __name__=='__main__':main()
