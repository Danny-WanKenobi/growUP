#!/usr/bin/env python3
"""Owner-run installation of reviewed deployment tools from an existing release."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
import os
import socket
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();host=socket.gethostname().split('.')[0]
    if os.geteuid()!=0 or host not in ('securecode-dev','srv1577410'): raise SystemExit('Root on a recognized Grow Up server required.')
    release=Path('/var/www/growup/current').resolve(strict=True)
    if release.parent!=Path('/var/www/growup/releases'): raise SystemExit('Active Grow Up release required.')
    source=release/'deployment'
    mapping={'growup-install-release':Path('/usr/local/sbin/growup-install-release'),
        'remote-install-release.sh':Path('/usr/local/libexec/growup/remote-install-release.sh'),
        'configure-server.py':Path('/usr/local/libexec/growup/configure-server.py')}
    for name in mapping:
        candidate=source/name
        if candidate.is_symlink() or not candidate.is_file(): raise SystemExit('Reviewed deployment source missing.')
    for name in ('growup-install-release','remote-install-release.sh'):
        subprocess.run(['/bin/bash','-n',str(source/name)],check=True)
    print('Target:',host,'; source:',release,'; tools:',', '.join(mapping),flush=True)
    if not args.apply:
        print('PREVIEW ONLY; no changes.');return
    backup=Path('/var/www/growup/deploy-backups')/('tools-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True,mode=0o700);os.chmod(backup.parent,0o700)
    def replace(path,content,metadata):
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(prefix='.growup-tool-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as output: output.write(content);output.flush();os.fsync(output.fileno())
            os.chmod(temporary,metadata[0]);os.chown(temporary,metadata[1],metadata[2]);os.replace(temporary,path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    originals={}
    for destination in (*mapping.values(),Path('/var/www/growup/shared/growup.env')):
        if destination.exists():
            m=destination.stat();originals[destination]=(destination.read_bytes(),(m.st_mode&0o777,m.st_uid,m.st_gid))
            copy=backup/destination.name;copy.write_bytes(originals[destination][0]);os.chmod(copy,0o600)
        else: originals[destination]=None
    try:
        for name,destination in mapping.items(): replace(destination,(source/name).read_bytes(),(0o755,0,0))
        subprocess.run(['/usr/bin/python3',str(mapping['configure-server.py']),'--apply'],check=True)
        subprocess.run(['systemctl','restart','growup.service'],check=True)
        port='8001' if host=='securecode-dev' else '8010'
        import time
        for _ in range(15):
            checks=[subprocess.run(['curl','--fail','--silent','--max-time','2','http://127.0.0.1:'+port+p],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode for p in ('/health','/ready')]
            if checks==[0,0]:
                print('TOOLS INSTALLED; server mode explicit; health/readiness passed. Backup:',backup);return
            time.sleep(2)
        raise RuntimeError('health check failed')
    except BaseException:
        for path,original in originals.items():
            if original: replace(path,*original)
            elif path.exists(): path.unlink()
        subprocess.run(['systemctl','restart','growup.service'],check=False)
        raise SystemExit('STOP: tool/configuration installation failed and previous files restored. Backups retained: '+str(backup))


if __name__=='__main__': main()
