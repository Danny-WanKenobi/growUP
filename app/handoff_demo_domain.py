"""Explicit synthetic custody realm: separate DB, role and signing key."""
from contextlib import contextmanager
from contextvars import ContextVar

DATABASE = "growup_handoff_demo"
ROLE = "growup_demo"
DOMAIN = "synthetic_handoff_demo"
_KEY = ContextVar("handoff_demo_key", default=None)


def assert_database(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() AS db, current_user AS role")
        row = cur.fetchone()
    if not row or row["db"] != DATABASE or row["role"] != ROLE:
        raise ValueError("synthetic handoff requires growup_handoff_demo as growup_demo; operational databases are forbidden")


def current_key():
    return _KEY.get()


def guard_payload(conn, payload):
    if not isinstance(payload,dict):
        return
    dossier = payload.get("dossier") or {}
    synthetic = payload.get("synthetic") is True or payload.get("domain") == DOMAIN
    if isinstance(dossier,dict):
        synthetic = synthetic or dossier.get("synthetic") is True or dossier.get("domain")==DOMAIN
    if synthetic:
        assert_database(conn)
        if current_key() is None:
            raise ValueError("synthetic custody requires an explicit isolated demo scope")


@contextmanager
def scope(conn, key):
    assert_database(conn)
    if not isinstance(key,str) or len(key)<32:
        raise ValueError("HANDOFF_DEMO_HMAC_KEY must contain at least 32 characters; no operational-key fallback")
    token = _KEY.set(key.encode("utf-8"))
    try:
        yield
    finally:
        _KEY.reset(token)
