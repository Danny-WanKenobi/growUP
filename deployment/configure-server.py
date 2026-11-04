#!/usr/bin/env python3
"""Host-pinned, atomic nonsecret server-mode configuration; never prints secrets."""
from datetime import datetime,timezone
from pathlib import Path
import argparse
import json
import os
import socket
import tempfile

PROFILES={
    'securecode-dev':('DEV_SERVER',8001,'growup_dev','https://REPLACE_ME_CALLBACK_HOST.example.invalid/integrations/resend/webhook'),
    'srv1577410':('LIVE_SERVER',8010,'growup_prod','https://growup.securecodeusa.com/integrations/resend/webhook'),
}


def revised(text,host,disable_resend=False):
    mode,port,database,callback=PROFILES[host]
    updates={'SERVER_MODE':mode,'RESEND_PUBLIC_CALLBACK_URL':callback}
    if disable_resend: updates.update(RESEND_TRACKING_ENABLED='0',RESEND_POLL_ENABLED='0',RESEND_POLL_ACCESS_APPROVED='0')
    kept=[]
    for line in text.splitlines():
        key,sep,_=line.strip().partition('=')
        if not (sep and key in updates): kept.append(line)
    result='\n'.join(kept).rstrip()+'\n'+'\n'.join(k+'='+v for k,v in updates.items())+'\n'
    # Repeating a deployment with already-correct settings must not create backups.
    values={}
    for line in text.splitlines():
        key,sep,value=line.strip().partition('=')
        if sep and key in updates: values.setdefault(key,[]).append(value)
    if all(values.get(k)==[v] for k,v in updates.items()): result=text
    return result,dict(server_mode=mode,port=port,expected_database=database,callback_url=callback)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true');parser.add_argument('--disable-resend',action='store_true')
    args=parser.parse_args();host=socket.gethostname().split('.')[0]
    if host not in PROFILES or os.geteuid()!=0: raise SystemExit('STOP: root on a recognized Grow Up server required.')
    path=Path('/var/www/growup/shared/growup.env')
    if path.is_symlink() or not path.is_file(): raise SystemExit('STOP: regular private service environment required.')
    metadata=path.stat()
    if metadata.st_mode&0o077: raise SystemExit('STOP: service environment must be private (0600).')
    text=path.read_text();replacement,report=revised(text,host,args.disable_resend)
    report.update(changed=replacement!=text,applied=False,secrets_displayed=False)
    if args.apply and replacement!=text:
        directory=Path('/var/www/growup/deploy-backups');directory.mkdir(mode=0o700,exist_ok=True)
        os.chmod(directory,0o700)
        backup=directory/('growup.env.'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        with backup.open('xb') as output: output.write(path.read_bytes())
        os.chmod(backup,0o600)
        fd,name=tempfile.mkstemp(prefix='.growup-env-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as output:
                output.write(replacement);output.flush();os.fsync(output.fileno())
            os.chmod(name,0o600);os.chown(name,metadata.st_uid,metadata.st_gid);os.replace(name,path)
        finally:
            if os.path.exists(name): os.unlink(name)
        report.update(applied=True,backup_path=str(backup))
    print(json.dumps(report))


if __name__=='__main__': main()
