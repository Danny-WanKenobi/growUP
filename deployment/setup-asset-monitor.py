#!/usr/bin/env python3
"""Owner-invoked timer installation only; no credentials or application env edits."""
import argparse
from pathlib import Path
import os
import subprocess

ROOT=Path('/var/www/growup/releases')
NAMES=('growup-asset-monitor.service','growup-asset-monitor.timer')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--release',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    release=Path(args.release).resolve(strict=True)
    if release.parent!=ROOT or release!=Path('/var/www/growup/current').resolve(strict=True): raise SystemExit('STOP: active development release required')
    sources=[release/'deployment'/name for name in NAMES]
    if not all(p.is_file() and not p.is_symlink() for p in sources): raise SystemExit('STOP: timer templates missing')
    print('Development-only internal monitor: 20 dossiers/60 seconds; 15-minute cadence; no provider or external actions.')
    if not args.apply: print('PREVIEW ONLY. Add --apply to install and activate.');return
    if os.geteuid()!=0: raise SystemExit('STOP: local sudo authentication required')
    for name,source in zip(NAMES,sources):
        target=Path('/etc/systemd/system')/name
        if target.is_symlink(): raise SystemExit('STOP: unexpected existing timer symlink')
        text=source.read_text(encoding='utf-8')
        if target.exists() and target.read_text(encoding='utf-8')!=text: raise SystemExit('STOP: existing timer differs; preserve for review')
    for name,source in zip(NAMES,sources):
        target=Path('/etc/systemd/system')/name
        if not target.exists():
            with target.open('x',encoding='utf-8') as output: output.write(source.read_text(encoding='utf-8'))
            os.chmod(target,0o644)
    subprocess.run(['systemd-analyze','verify',*[str(Path('/etc/systemd/system')/n) for n in NAMES]],check=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now','growup-asset-monitor.timer'],check=True)
    subprocess.run(['systemctl','start','growup-asset-monitor.service'],check=True)
    subprocess.run(['systemctl','--no-pager','show','growup-asset-monitor.service','-p','Result','-p','ExecMainStatus'],check=True)
    subprocess.run(['systemctl','--no-pager','list-timers','growup-asset-monitor.timer'],check=True)
    print('MONITOR INSTALLED. Inspect owner briefs for dated automatic checks. Stop: sudo systemctl disable --now growup-asset-monitor.timer')


if __name__=='__main__': main()
