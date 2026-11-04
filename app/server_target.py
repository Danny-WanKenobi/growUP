"""Deployment destination, independent of data provenance and external-action mode.

Selecting LIVE_SERVER never deploys, changes credentials, migrates data, grants
permissions, enables providers or makes a callback publicly reachable.
"""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ServerTarget:
    mode: str
    origin: str
    ssh_host: str
    operational_database: str
    callback_available: bool
    callback_origin: str | None = None

    def url(self,path):
        if path not in ('/','/owner-auth','/health','/ready','/wondrue','/wondrue/command','/wondrue/handoff-demo','/integrations/resend/webhook','/integrations/resend/enroll'):
            raise ValueError('unsupported internal service endpoint')
        return (self.callback_origin if path=='/integrations/resend/webhook' and self.callback_origin else self.origin)+path


def get_server_target(environ=None):
    env=os.environ if environ is None else environ
    mode=env.get('SERVER_MODE','DEV_SERVER').strip().upper()
    if mode=='DEV_SERVER': return ServerTarget(mode,'https://growup.local:8443','securecode-dev','growup_dev',False)
    if mode=='LIVE_SERVER': return ServerTarget(mode,'https://growup.securecodeusa.com:8443','myvps','growup_prod',True,'https://growup.securecodeusa.com')
    raise ValueError('SERVER_MODE must be DEV_SERVER or LIVE_SERVER')


def inspect(environ=None):
    env=os.environ if environ is None else environ
    target=get_server_target(env)
    demo=env.get('DEMO_MODE','yes').strip().lower()
    if demo not in ('','yes','no'): raise ValueError('DEMO_MODE must be yes or no')
    database=env.get('DATABASE_NAME','').strip()
    blockers=[]
    if database and database!=target.operational_database and not (target.mode=='DEV_SERVER' and database in ('growup_mvp_gate','growup_test','growup_handoff_demo')):
        blockers.append('Configured operational database does not match the selected server target')
    return dict(server_mode=target.mode,origin=target.origin,http_origin=target.origin.replace('https://','http://').replace(':8443',':8080'),
        endpoints={name:target.url(path) for name,path in dict(landing='/',login='/owner-auth',health='/health',readiness='/ready',cockpit='/wondrue',command='/wondrue/command',demo_viewer='/wondrue/handoff-demo',private_enrollment='/integrations/resend/enroll').items()},
        resend_callback_url=target.url('/integrations/resend/webhook') if target.callback_available else None,
        callback_policy='requires_verified_public_routing_and_signature' if target.callback_available else 'unavailable_private_dev_server_use_offline_tests_or_bounded_polling',
        external_action_mode='simulated' if demo in ('','yes') else 'live_requires_separate_permissions',
        dataset_policy='Synthetic dossiers use the isolated demo connector; DEMO_MODE alone never relabels or switches an operational database.',
        blockers=blockers,configuration_matches_server=not blockers,
        public_routing_verified=False,provider_access_granted=False,provider_calls=0,database_writes=0)


if __name__=='__main__':
    import json
    try: print(json.dumps(inspect(),indent=2))
    except ValueError: raise SystemExit('STOP: invalid server/action mode configuration; no changes made.')
