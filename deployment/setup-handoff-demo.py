#!/usr/bin/env python3
"""Owner-invoked Ubuntu-only setup. No operational DB migrations or provider calls.

Run from the reviewed, deployed release with --apply. Generates secrets into a
root-only dedicated env file; never prints them. Existing credentials persist.
"""
import argparse
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import tempfile
import urllib.request

DB = "growup_handoff_demo"
ROLE = "growup_demo"
ENV = Path("/var/www/growup/shared/handoff-demo.env")
DROPIN = Path("/etc/systemd/system/growup.service.d/40-handoff-demo.conf")
DROPIN_TEXT = "[Service]\nEnvironmentFile=/var/www/growup/shared/handoff-demo.env\n"


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def sql(text):
    return command(["sudo","-u","postgres","psql","-X","-A","-t","-v","ON_ERROR_STOP=1","-d","postgres"],input=text)


def write_private(values):
    fd, name = tempfile.mkstemp(prefix=".handoff-demo-", dir=ENV.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as handle:
            handle.write("".join(k+"="+v+"\n" for k,v in values.items()))
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name,ENV)
    finally:
        if os.path.exists(name): os.unlink(name)


def read_private():
    fd=os.open(ENV,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o600:
            raise RuntimeError("existing demo env must be a root-owned regular file with mode 0600")
        with os.fdopen(fd,"r",encoding="utf-8") as handle:
            fd=-1
            return dict(line.strip().split("=",1) for line in handle if line.strip())
    finally:
        if fd!=-1: os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release",required=True)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    release=Path(args.release).resolve(strict=True)
    if release.parent!=Path("/var/www/growup/releases") or not (release/"app/handoff_demo.py").is_file():
        raise RuntimeError("select a reviewed Grow Up development release containing the demo implementation")
    print("Target: isolated",DB,"as",ROLE,"— operational databases and provider settings unchanged.")
    if not args.apply:
        print("Preview only. --apply provisions the role/database/env, initializes DEMO schema and restarts growup.service.")
        return
    if socket.gethostname().split(".")[0]!="securecode-dev":
        raise RuntimeError("setup is restricted to the development VM securecode-dev")
    if os.geteuid()!=0:
        raise RuntimeError("owner must invoke setup with sudo")
    if DROPIN.is_symlink() or (DROPIN.exists() and DROPIN.read_text()!=DROPIN_TEXT):
        raise RuntimeError("existing conflicting demo service drop-in; preserve it for owner review")
    role=sql("SELECT rolcanlogin::text || ',' || rolsuper::text || ',' || rolcreatedb::text || ',' || rolcreaterole::text || ',' || rolreplication::text || ',' || rolbypassrls::text FROM pg_roles WHERE rolname='growup_demo';")
    exists=ENV.exists() or ENV.is_symlink()
    if role and not exists:
        raise RuntimeError("role already exists without this setup's credential file; refusing password rotation")
    if role and role!="true,false,false,false,false,false":
        raise RuntimeError("existing demo role has unexpected privilege flags")
    if exists:
        values=read_private()
    else:
        values={"HANDOFF_DEMO_ENABLED":"1","HANDOFF_DEMO_DATABASE_HOST":"127.0.0.1",
            "HANDOFF_DEMO_DATABASE_PORT":"5432","HANDOFF_DEMO_DATABASE_NAME":DB,
            "HANDOFF_DEMO_DATABASE_USER":ROLE,"HANDOFF_DEMO_DATABASE_PASSWORD":secrets.token_hex(24),
            "HANDOFF_DEMO_HMAC_KEY":secrets.token_hex(32)}
        write_private(values)  # durable before role creation, so interruption preserves credentials
    expected_keys={"HANDOFF_DEMO_ENABLED","HANDOFF_DEMO_DATABASE_HOST","HANDOFF_DEMO_DATABASE_PORT",
                   "HANDOFF_DEMO_DATABASE_NAME","HANDOFF_DEMO_DATABASE_USER","HANDOFF_DEMO_DATABASE_PASSWORD","HANDOFF_DEMO_HMAC_KEY"}
    if (set(values)!=expected_keys or values.get("HANDOFF_DEMO_DATABASE_NAME")!=DB or values.get("HANDOFF_DEMO_DATABASE_USER")!=ROLE
            or values.get("HANDOFF_DEMO_DATABASE_HOST")!="127.0.0.1"
            or values.get("HANDOFF_DEMO_DATABASE_PORT")!="5432"
            or values.get("HANDOFF_DEMO_ENABLED")!="1"
            or not all(c in "0123456789abcdef" for c in values.get("HANDOFF_DEMO_DATABASE_PASSWORD",""))
            or len(values.get("HANDOFF_DEMO_DATABASE_PASSWORD",""))<48
            or len(values.get("HANDOFF_DEMO_HMAC_KEY",""))<64
            or not all(c in "0123456789abcdef" for c in values.get("HANDOFF_DEMO_HMAC_KEY",""))):
        raise RuntimeError("existing demo configuration is unexpected; refusing to replace it")
    if not role:
        sql("CREATE ROLE growup_demo LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+values["HANDOFF_DEMO_DATABASE_PASSWORD"]+"';")
    owner=sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='growup_handoff_demo';")
    if owner and owner!=ROLE:
        raise RuntimeError("existing demo database has another owner; refusing to modify it")
    if not owner:
        sql("CREATE DATABASE growup_handoff_demo OWNER growup_demo;")
    sql("REVOKE CONNECT ON DATABASE growup_handoff_demo FROM PUBLIC;")
    command(["systemd-run","--wait","--pipe","--collect","--quiet","-p","User=growup","-p","Group=growup",
        "-p","WorkingDirectory="+str(release),"-p","EnvironmentFile="+str(ENV),
        "/usr/bin/env","PYTHONDONTWRITEBYTECODE=1","OBS_ENABLED=0",
        str(release/".venv/bin/python"),"-B","-m","app.handoff_demo","init"])
    DROPIN.parent.mkdir(parents=True,exist_ok=True)
    newly_created=not DROPIN.exists()
    DROPIN.write_text(DROPIN_TEXT,encoding="utf-8")
    try:
        command(["systemctl","daemon-reload"])
        command(["systemctl","restart","growup.service"])
        command(["systemctl","is-active","--quiet","growup.service"])
        import time
        for attempt in range(15):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8001/ready",timeout=2) as response:
                    if response.status==200: break
            except Exception:
                if attempt==14: raise RuntimeError("application readiness did not recover")
                time.sleep(1)
    except Exception:
        if newly_created:
            DROPIN.unlink()
            command(["systemctl","daemon-reload"])
            command(["systemctl","restart","growup.service"])
        raise
    print("DEMO DATABASE READY. Secrets retained in root-only handoff-demo.env; no fixture scenarios launched.")
    print("Open https://securecodeusa.local:8443/wondrue/handoff-demo using the existing Wondrue operator key.")


if __name__=="__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("STOP: setup command failed. No secret values displayed; preserve state for owner review.")
