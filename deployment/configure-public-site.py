#!/usr/bin/env python3
"""Reviewed VPS public landing + existing Basic gateway/session bridge.

Keeps all unlisted endpoints private. Does not enable live email delivery or
change DEMO_MODE, provider credentials, database targets or the htpasswd file.
"""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import os
import secrets
import socket
import subprocess
import tempfile

PROXY='''        proxy_pass http://127.0.0.1:8010;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
'''


def blocks(port=8010):
    result='''    # BEGIN Grow Up public landing (all other endpoints retain owner authentication).
    include /etc/nginx/growup-login-bridge.inc;
    auth_request /_growup_session;
    satisfy any;
    location = /_growup_session {
        internal;
        auth_basic off;
        auth_request off;
        proxy_pass http://127.0.0.1:8010/public/session-check;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header Cookie $http_cookie;
    }
    location = /owner-auth {
        auth_request off;
        satisfy all;
        limit_req zone=growup_login burst=5 nodelay;
        client_max_body_size 2k;
        error_page 401 = @growup_login_denied;
        proxy_set_header X-Growup-Authenticated-User $remote_user;
        proxy_set_header X-Growup-Login-Bridge $growup_login_bridge;
'''+PROXY+'''    }
    location @growup_login_denied {
        auth_basic off;
        auth_request off;
        default_type application/json;
        return 403 '{"detail":"invalid_login"}';
    }
'''
    for path,method in [('/','GET'),('/support/start','POST'),('/support/message','POST'),
                        ('/support/appointment','POST'),('/support/logout','POST')]:
        result+=f'''    location = {path} {{
        auth_basic off;
        auth_request off;
        limit_except {method} {{ deny all; }}
        limit_req zone=growup_support burst=12 nodelay;
        client_max_body_size 8k;
'''+PROXY+'''    }
'''
    result+='''    location ^~ /public-assets/ {
        auth_basic off;
        auth_request off;
        limit_except GET { deny all; }
'''+PROXY+'''    }
    # END Grow Up public landing
'''
    return result.replace('127.0.0.1:8010','127.0.0.1:'+str(port))


def revised(text):
    marker='    # BEGIN Grow Up public landing'
    if marker in text:
        if blocks() not in text:raise ValueError('Existing public routing requires review')
        return text
    expected='    auth_basic_user_file /etc/nginx/growup.htpasswd;\n'
    if text.count(expected)!=1 or text.count('location = /integrations/resend/webhook')!=1:
        raise ValueError('Expected existing gateway and signed callback configuration')
    # Callback must override the newly inherited session check too.
    callback='    location = /integrations/resend/webhook {\n        auth_basic off;'
    if callback not in text:raise ValueError('Expected reviewed callback block')
    text=text.replace(callback,callback+'\n        auth_request off;')
    text=text.replace('    listen 443 ssl;','    listen 443 ssl;\n    listen 8443 ssl;',1)
    redirect='''server {
    listen 8080;
    server_name growup.securecodeusa.com;
    return 308 https://growup.securecodeusa.com:8443$request_uri;
}
'''
    return redirect+text.replace(expected,expected+blocks())


def development_route():
    return '''server {
    listen 80;
    listen 8080;
    server_name growup.local;
    return 308 https://growup.local:8443$request_uri;
}
server {
    listen 443 ssl;
    listen 8443 ssl;
    server_name growup.local;
    ssl_certificate /etc/nginx/ssl/growup.local.crt;
    ssl_certificate_key /etc/nginx/ssl/growup.local.key;
    auth_basic "Grow Up - private preview";
    auth_basic_user_file /etc/nginx/growup.htpasswd;
'''+blocks(8001)+'''    location / {
'''+PROXY.replace('127.0.0.1:8010','127.0.0.1:8001')+'''    }
}
'''


def atomic(path,text,mode=0o600):
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    host=socket.gethostname().split('.')[0]
    if os.geteuid()!=0 or host not in ('srv1577410','securecode-dev'):raise SystemExit('Root on an approved Grow Up host required.')
    dev=host=='securecode-dev'
    route=Path('/etc/nginx/sites-available')/('growup.local' if dev else 'growup.securecodeusa.com')
    env=Path('/var/www/growup/shared/growup.env')
    if route.is_symlink() or env.is_symlink() or env.stat().st_mode&0o077:raise SystemExit('Regular reviewed route and private environment required.')
    original=route.read_text() if route.exists() else ''
    if dev and original and original!=development_route():raise SystemExit('Existing growup.local route needs review.')
    updated=development_route() if dev else revised(original)
    if not Path('/etc/nginx/growup.htpasswd').is_file():raise SystemExit('Existing gateway credential file required.')
    if dev and not Path('/etc/nginx/ssl/growup.local.crt').is_file():raise SystemExit('Trusted growup.local certificate required first.')
    if not args.apply:
        print('PREVIEW: public root, assets and support; Basic credentials retained behind login drawer. No changes.');return
    backup=Path('/var/www/growup/deploy-backups')/('public-site-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(mode=0o700)
    (backup/'nginx.conf').write_text(original);(backup/'growup.env').write_bytes(env.read_bytes())
    os.chmod(backup/'growup.env',0o600)
    values={}
    for line in env.read_text().splitlines():
        k,sep,v=line.partition('=')
        if sep:values[k.strip()]=v.strip().strip('"\'')
    bridge=values.get('PUBLIC_LOGIN_BRIDGE_SECRET') or secrets.token_urlsafe(48)
    session=values.get('PUBLIC_SESSION_SECRET') or secrets.token_urlsafe(48)
    updates={'PUBLIC_LOGIN_BRIDGE_SECRET':bridge,'PUBLIC_SESSION_SECRET':session,'CSR_PUBLIC_ENABLED':'1',
             'RESEND_RECEIVING_DOMAIN':'growup.securecodeusa.com'}
    lines=[line for line in env.read_text().splitlines() if line.strip().partition('=')[0] not in updates]
    config='\n'.join(lines).rstrip()+'\n'+'\n'.join(k+'='+v for k,v in updates.items())+'\n'
    include=Path('/etc/nginx/growup-login-bridge.inc')
    limits=Path('/etc/nginx/conf.d/growup-public-limits.conf')
    saved={p:p.read_bytes() if p.exists() else None for p in (include,limits)}
    hosts=Path('/etc/hosts');old_hosts=hosts.read_text() if dev else None
    enabled=Path('/etc/nginx/sites-enabled/growup.local')
    linked=False
    try:
        atomic(env,config)
        atomic(include,'set $growup_login_bridge "'+bridge+'";\n')
        atomic(limits,'limit_req_zone $binary_remote_addr zone=growup_login:1m rate=10r/m;\nlimit_req_zone $binary_remote_addr zone=growup_support:1m rate=5r/s;\n',0o644)
        atomic(route,updated,0o644)
        if dev and not enabled.exists():enabled.symlink_to(route);linked=True
        if dev and not any('growup.local' in line.split('#',1)[0].split()[1:] for line in old_hosts.splitlines()):
            with hosts.open('a') as stream:stream.write('\n127.0.0.1 growup.local\n')
        subprocess.run(['nginx','-t'],check=True)
        subprocess.run(['systemctl','restart','growup.service'],check=True)
        import time,urllib.request
        for attempt in range(10):
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+('8001' if dev else '8010')+'/ready',timeout=3) as response:
                    if response.status==200:break
            except Exception:
                if attempt==9:raise
                time.sleep(1)
        subprocess.run(['systemctl','reload','nginx'],check=True)
    except BaseException:
        atomic(env,(backup/'growup.env').read_text())
        atomic(route,original,0o644)
        if linked:enabled.unlink(missing_ok=True)
        if dev:hosts.write_text(old_hosts)
        for p,prior in saved.items():
            if prior is None:p.unlink(missing_ok=True)
            else:atomic(p,prior.decode(),0o600 if p==include else 0o644)
        subprocess.run(['systemctl','restart','growup.service'],check=False)
        subprocess.run(['nginx','-t'],check=False);subprocess.run(['systemctl','reload','nginx'],check=False)
        raise
    print('Public landing/support installed. Existing gateway preserved; other endpoints remain private.')
    if not dev:
        status=subprocess.run(['ufw','status'],capture_output=True,text=True,check=True)
        if 'Status: active' in status.stdout:
            subprocess.run(['ufw','allow','8080/tcp','comment','Grow Up HTTP landing redirect'],check=True)
            subprocess.run(['ufw','allow','8443/tcp','comment','Grow Up HTTPS landing'],check=True)
    print('Private rollback checkpoint: '+str(backup))


if __name__=='__main__':main()
