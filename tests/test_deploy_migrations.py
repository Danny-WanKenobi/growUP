"""Focused deployment safety checks; no live provider or production access."""
from pathlib import Path
import hashlib
import importlib.util
import os
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]

def configurator():
    spec=importlib.util.spec_from_file_location('configure_server',ROOT/'deployment/configure-server.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_mode_update_preserves_secrets_and_existing_provider_policy():
    tool=configurator();original='DATABASE_PASSWORD="private-value"\nDEMO_MODE=yes\nRESEND_TRACKING_ENABLED=1\n'
    changed,report=tool.revised(original,'srv1577410')
    assert 'DATABASE_PASSWORD="private-value"' in changed and 'DEMO_MODE=yes' in changed
    assert 'RESEND_TRACKING_ENABLED=1' in changed and 'SERVER_MODE=LIVE_SERVER' in changed
    assert 'private-value' not in str(report)
    assert tool.revised(changed,'srv1577410')[0]==changed
    dev,_=tool.revised(original,'securecode-dev',True)
    assert 'SERVER_MODE=DEV_SERVER' in dev and 'RESEND_TRACKING_ENABLED=0' in dev


def test_pending_inventory_is_read_only_and_closes_connection(monkeypatch):
    from app import migrate
    sql=[]
    class Cursor:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def execute(self,text): sql.append(text)
        def fetchone(self): return {'name':None}
    class Conn:
        closed=False;rolled=False
        def cursor(self): return Cursor()
        def rollback(self): self.rolled=True
        def close(self): self.closed=True
    conn=Conn();monkeypatch.setattr(migrate,'get_connection',lambda:conn)
    result=migrate.pending_migrations()
    assert result and sql[0]=='SET TRANSACTION READ ONLY'
    assert all(s.startswith(('SELECT','SET TRANSACTION')) for s in sql)
    assert conn.closed and conn.rolled


def test_callback_route_is_exact_and_keeps_private_preview_gate():
    spec=importlib.util.spec_from_file_location('route_setup',ROOT/'deployment/configure-resend-route.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    original='server {\n    auth_basic "Grow Up - private preview";\n    location / {\n        proxy_pass http://127.0.0.1:8010;\n    }\n}\n'
    result=module.revised(original)
    assert original.replace('    location / {',module.BLOCK+'    location / {')==result
    assert 'location = /integrations/resend/webhook' in result
    assert result.count('auth_basic off;')==1
    assert module.revised(result)==result


@pytest.mark.skipif(os.name=='nt',reason='Bash deployment rollback proof runs on Ubuntu')
@pytest.mark.parametrize('fault',['none','migration','health'])
def test_controller_activation_and_rollback_with_mocked_system_tools(tmp_path,fault):
    root=tmp_path/'growup';deployment=root/'deployment';deployment.mkdir(parents=True)
    incoming=root/'incoming';incoming.mkdir();shared=root/'shared';shared.mkdir()
    (shared/'growup.env').write_text('KEEP=original\n')
    previous=root/'releases/previous';previous.mkdir(parents=True);(root/'current').symlink_to(previous)
    release='20261104T120000Z_'+'a'*7;archive=incoming/(release+'.tar');archive.write_bytes(b'fixture-only')
    archive.with_suffix('.tar.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+release+'.tar\n')
    installer=deployment/'prepare.sh'
    installer.write_text('#!/bin/bash\nset -e\nr="'+str(root)+'/releases/$1"\nmkdir -p "$r/.venv/bin" "$r/app"\nprintf "#!/bin/sh\nexit 0\n" > "$r/.venv/bin/python"\nchmod +x "$r/.venv/bin/python"\ntouch "$r/app/migrate.py" "$r/RELEASE-MANIFEST.txt"\n')
    configure=deployment/'configure.py';configure.write_text('from pathlib import Path\nPath('+repr(str(shared/'growup.env'))+').write_text("KEEP=changed\\n")\n')
    bin=tmp_path/'bin';bin.mkdir()
    def executable(name,body):
        p=bin/name;p.write_text('#!/bin/bash\n'+body+'\n');p.chmod(0o755)
    executable('hostname','echo srv1577410')
    executable('runuser','while [[ "$1" != -- ]]; do shift; done; shift; exec "$@"')
    executable('install','if [[ "$1" == -d ]]; then for p in "$@"; do [[ "$p" == /* ]] && mkdir -p "$p"; done; else cp "${@: -2:1}" "${@: -1}"; fi')
    executable('pg_dump','printf fake-backup')
    executable('pg_restore','echo fake-inventory')
    executable('systemd-run','if [[ "$*" == *"app.migrate --check"* && "$FAULT" == migration ]]; then exit 3; fi; exit 0')
    executable('systemctl','echo "$*" >> "'+str(tmp_path/'service.log')+'"; exit 0')
    executable('curl','[[ "$FAULT" != health ]]')
    executable('sleep','exit 0')
    text=(ROOT/'deployment/growup-install-release').read_text().replace('exec 9>/run/lock/growup-deploy.lock','exec 9>'+str(tmp_path/'lock'))
    text=text.replace('ROOT=/var/www/growup','ROOT='+str(root)).replace('INSTALLER=/usr/local/libexec/growup/remote-install-release.sh','INSTALLER='+str(installer)).replace('CONFIGURATOR=/usr/local/libexec/growup/configure-server.py','CONFIGURATOR='+str(configure))
    script=tmp_path/'controller';script.write_text(text)
    result=subprocess.run(['/bin/bash',str(script),release,'a'*40],env={**os.environ,'PATH':str(bin)+':'+os.environ['PATH'],'FAULT':fault},capture_output=True,text=True)
    if fault=='none':
        assert result.returncode==0,result.stderr
        assert (root/'current').resolve()==root/'releases'/release
        assert 'status=healthy' in (root/'current/RELEASE-MANIFEST.txt').read_text()
    else:
        assert result.returncode!=0
        assert (root/'current').resolve()==previous
        assert (shared/'growup.env').read_text()=='KEEP=original\n'
        if fault=='migration': assert not (tmp_path/'service.log').exists()
        else: assert (tmp_path/'service.log').read_text().count('restart growup.service')==2
    assert (root/'deploy-backups'/release/'database.dump').exists()
