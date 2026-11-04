#!/usr/bin/env python3
"""Provision only the owner hypothetical database; prove isolation before enablement.

No operational schema/data/privilege changes. Existing credentials are preserved.
"""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import tempfile

DB='growup_wondrue_sandbox'
ROLE='growup_sandbox'
ENV=Path('/var/www/growup/shared/wondrue-sandbox.env')


def sql(database,statement):
    result=subprocess.run(['runuser','-u','postgres','--','psql','-X','-At','-v','ON_ERROR_STOP=1','-d',database],
                          input=statement,text=True,capture_output=True)
    if result.returncode:raise RuntimeError('Sandbox database setup/proof refused; secret output suppressed.')
    return result.stdout.strip()


def private_env(password,enabled):
    text=f'WONDRUE_SANDBOX_ENABLED={int(enabled)}\nWONDRUE_SANDBOX_PASSWORD={password}\n'
    fd,name=tempfile.mkstemp(prefix='.wondrue-sandbox-',dir=ENV.parent)
    temporary=Path(name)
    with os.fdopen(fd,'w') as handle:handle.write(text)
    os.chmod(temporary,0o600);os.replace(temporary,ENV)


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    host=socket.gethostname().split('.')[0]
    if os.geteuid()!=0 or host not in ('securecode-dev','srv1577410'):
        raise SystemExit('Run with sudo on an approved Grow Up server only.')
    operational='growup_dev' if host=='securecode-dev' else 'growup_prod'
    print('Target: dedicated hypothetical database and restricted role; no operational database changes.',flush=True)
    if not args.apply:print('Preview only. --apply provisions, proves and enables the isolated workspace.');return
    if ENV.exists():
        info=ENV.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600:
            raise RuntimeError('Existing private sandbox configuration permissions refused.')
        values=dict(line.split('=',1) for line in ENV.read_text().splitlines() if '=' in line)
        password=values.get('WONDRUE_SANDBOX_PASSWORD','')
        if len(password)<32 or not all(c.isalnum() or c in '_-' for c in password):
            raise RuntimeError('Existing sandbox credential shape refused; no rotation performed.')
        backup=Path('/var/www/growup/deploy-backups')/('wondrue-sandbox-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.env')
        backup.parent.mkdir(exist_ok=True);backup.write_bytes(ENV.read_bytes());backup.chmod(0o600)
    else:
        if sql('postgres',f"SELECT count(*) FROM pg_roles WHERE rolname='{ROLE}'")!='0':
            raise RuntimeError('Sandbox role exists without retained credentials; refusing password rotation.')
        password=secrets.token_urlsafe(48)
    private_env(password,False)
    if sql('postgres',f"SELECT count(*) FROM pg_roles WHERE rolname='{ROLE}'")=='0':
        sql('postgres',f"CREATE ROLE {ROLE} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;")
    if sql('postgres',f"SELECT count(*) FROM pg_database WHERE datname='{DB}'")=='0':
        sql('postgres',f'CREATE DATABASE {DB} OWNER postgres;')
    if sql('postgres',f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='{DB}'")!='postgres':
        raise RuntimeError('Sandbox database ownership refused.')
    role_ok=sql('postgres',f"SELECT count(*) FROM pg_roles WHERE rolname='{ROLE}' AND rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit")
    membership=sql('postgres',f"SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname='{ROLE}')")
    if role_ok!='1' or membership!='0':raise RuntimeError('Sandbox role privilege proof failed.')
    sql(DB,f"""
      REVOKE ALL ON DATABASE {DB} FROM PUBLIC;
      GRANT CONNECT ON DATABASE {DB} TO {ROLE};
      REVOKE CREATE ON SCHEMA public FROM PUBLIC;
      GRANT USAGE ON SCHEMA public TO {ROLE};
      CREATE TABLE IF NOT EXISTS sandbox_scenarios(
        id uuid PRIMARY KEY,payload jsonb NOT NULL CHECK(length(payload::text)<262144),
        digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{{64}}$'),created_at timestamptz NOT NULL DEFAULT now());
      CREATE OR REPLACE FUNCTION sandbox_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Sandbox history is append-only'; END; $$;
      DROP TRIGGER IF EXISTS sandbox_immutable ON sandbox_scenarios;
      CREATE TRIGGER sandbox_immutable BEFORE UPDATE OR DELETE ON sandbox_scenarios
        FOR EACH ROW EXECUTE FUNCTION sandbox_append_only();
      REVOKE ALL ON sandbox_scenarios FROM PUBLIC,{ROLE};
      GRANT SELECT,INSERT ON sandbox_scenarios TO {ROLE};
    """)
    # Inspect effective privileges without changing any operational grants.
    forbidden=sql(operational,f"""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','S','f') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
        AND has_table_privilege('{ROLE}',c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')""")
    if forbidden!='0':raise RuntimeError('Operational-table isolation proof failed; sandbox stays disabled.')
    elevated_functions=sql(operational,f"""SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE p.prosecdef AND p.prorettype<>'trigger'::regtype AND n.nspname NOT LIKE 'pg_%'
        AND has_function_privilege('{ROLE}',p.oid,'EXECUTE')""")
    if elevated_functions!='0':raise RuntimeError('Operational security-definer function isolation proof failed.')
    # Credential authentication and effective read-only role restrictions proved in a network-blocked worker.
    release=Path('/var/www/growup/current').resolve(strict=True)
    proof_code="""import json,os
from uuid import uuid4
from app import wondrue_sandbox as s
with s.connection() as c:
 with c.cursor() as q:
  q.execute("SELECT has_table_privilege(current_user,'sandbox_scenarios','UPDATE') AS can_update, has_table_privilege(current_user,'sandbox_scenarios','DELETE') AS can_delete, has_schema_privilege(current_user,'public','CREATE') AS can_create")
  assert not any(q.fetchone().values())
reference={'kind':'dossier_version','run_id':str(uuid4()),'key':'proof','version':1,'digest':'ab'*32}
sid=str(uuid4());first=s.save(sid,reference,{'config':'bench','site':{'usable_area_m2':100},'crop':{}})
assert s.save(sid,reference,first['inputs'])==first
assert first['result']['hypothetical'] and not first['result']['authorization']
assert first['result']['engineering']['missing']
assert any(i['id']==sid for i in s.list_scenarios()['items'])
print('PASS: dedicated role, immutable storage, deterministic incomplete scenario, idempotency and retrieval.')
"""
    result=subprocess.run(['systemd-run','--wait','--pipe','--collect','--quiet','-p','User=growup','-p','Group=growup',
        '-p','WorkingDirectory='+str(release),'-p','EnvironmentFile='+str(ENV),'-p','IPAddressDeny=any','-p','IPAddressAllow=localhost',
        '/usr/bin/env','WONDRUE_SANDBOX_ENABLED=1','PYTHONDONTWRITEBYTECODE=1',str(release/'.venv/bin/python'),'-B','-'],
        input=proof_code,text=True,capture_output=True)
    if result.returncode:raise RuntimeError('Sandbox runtime proof failed; sandbox stays disabled; secret output suppressed.')
    print(result.stdout.strip())
    dropin=Path('/etc/systemd/system/growup.service.d/wondrue-sandbox.conf');dropin.parent.mkdir(exist_ok=True)
    dropin.write_text('[Service]\nEnvironmentFile='+str(ENV)+'\n')
    private_env(password,True)
    try:
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','restart','growup.service'],check=True)
        port='8001' if host=='securecode-dev' else '8010'
        import time
        for attempt in range(15):
            check=subprocess.run(['curl','--fail','--silent','http://127.0.0.1:'+port+'/ready'],capture_output=True)
            if check.returncode==0:break
            time.sleep(.5)
        else:raise RuntimeError('Application readiness failed; inspect service logs.')
    except BaseException:
        private_env(password,False)
        subprocess.run(['systemctl','restart','growup.service'],check=False,capture_output=True)
        raise
    proof={'result':'PASS','database':DB,'role':ROLE,'operational_table_privileges':0,
           'sandbox_enabled':True,'provider_calls':0,'external_actions':0,'operational_writes':0}
    print(json.dumps(proof))


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('STOP: sandbox setup/proof failed. Sandbox was not approved for execution; no secrets displayed.')
