#!/usr/bin/env python3
"""Exact-path VPS callback routing only; no Resend registration or enablement."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import os
import re
import socket
import subprocess
import tempfile

BLOCK='''    # Grow Up signed Resend callback; private preview remains inherited elsewhere.
    location = /integrations/resend/webhook {
        auth_basic off;
        limit_except POST { deny all; }
        client_max_body_size 32k;
        proxy_pass http://127.0.0.1:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_connect_timeout 5s;
        proxy_read_timeout 60s;
    }
'''


def revised(text):
    if 'location = /integrations/resend/webhook' in text:
        if BLOCK not in text and BLOCK.replace('        auth_basic off;','        auth_basic off;\n        auth_request off;') not in text:
            raise ValueError('Existing callback location requires manual review')
        return text
    pattern=r'(?m)^    location / \{\s*\n        proxy_pass http://127\.0\.0\.1:8010;'
    matches=list(re.finditer(pattern,text))
    if len(matches)!=1 or 'auth_basic "Grow Up - private preview";' not in text:
        raise ValueError('Expected reviewed VPS preview/backend configuration')
    at=matches[0].start()
    return text[:at]+BLOCK+text[at:]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if os.geteuid()!=0 or socket.gethostname().split('.')[0]!='srv1577410': raise SystemExit('Root on the live VPS required.')
    path=Path('/etc/nginx/sites-available/growup.securecodeusa.com')
    if path.is_symlink(): raise SystemExit('Regular reviewed Nginx file required.')
    original=path.read_text();replacement=revised(original)
    if replacement==original: print('Exact callback route already installed; no change.');return
    if not args.apply: print('PREVIEW: add exact POST callback route; preserve preview gate elsewhere. No changes.');return
    directory=Path('/etc/nginx/growup-route-backups');directory.mkdir(mode=0o700,exist_ok=True)
    backup=directory/('growup.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.conf')
    backup.write_text(original);os.chmod(backup,0o600)
    fd,name=tempfile.mkstemp(prefix='.growup-nginx-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as output: output.write(replacement);output.flush();os.fsync(output.fileno())
        os.chmod(name,0o644);os.replace(name,path)
        subprocess.run(['nginx','-t'],check=True)
        subprocess.run(['systemctl','reload','nginx'],check=True)
    except BaseException:
        path.write_text(original)
        subprocess.run(['nginx','-t'],check=True)
        subprocess.run(['systemctl','reload','nginx'],check=True)
        raise
    finally:
        if os.path.exists(name): os.unlink(name)
    print('EXACT CALLBACK ROUTE INSTALLED. Preview authentication preserved. Resend remains unregistered. Backup:',backup)


if __name__=='__main__': main()
