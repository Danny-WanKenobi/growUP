# Grow Up deployment: development VM and live VPS

## Supported targets

| Launcher | SSH alias / expected host | Mode | Loopback port | Operational DB |
| --- | --- | --- | --- | --- |
| deploy-dev.ps1 | securecode-dev / securecode-dev | DEV_SERVER | 8001 | growup_dev |
| deploy-live.ps1 | myvps / srv1577410 | LIVE_SERVER | 8010 | growup_prod |

Both wrappers use deploy.ps1 and propagate failures. Hostname, branch main,
exact origin/main SHA and at least 1 GiB free space are checked before upload.
A read-only preview is `./deployment/deploy-live.ps1 -PlanOnly`. Use the matching
launcher without PlanOnly to deploy. The archive contains committed GitHub HEAD
only. Dirty files block deployment unless explicitly reviewed with
`-AllowDirtyWorktree`; that option neither stages nor changes those files.

The live launcher also compares SHA256 of the installed trusted controller,
preparer and server configurator against the reviewed repository copies. Drift
or missing tools stops deployment before upload. No automatic sudoers expansion
or production host override is performed.

## Trusted tools and one-time installation

Repository-managed tools are growup-install-release, remote-install-release.sh,
configure-server.py and install-deployment-tools.py. The controller/preparer/
configurator are root-installed under /usr/local; application code runs as growup.
After deploying a release carrying these files, a local administrator can preview
and apply the reviewed tool installation:

```bash
sudo /usr/bin/python3 /var/www/growup/current/deployment/install-deployment-tools.py
sudo /usr/bin/python3 /var/www/growup/current/deployment/install-deployment-tools.py --apply
```

The installer validates shell syntax, preserves tool backups, configures the
host-pinned SERVER_MODE and callback URL without showing secrets, restarts only
growup.service, and checks health/readiness. It does not enable Resend or spending.
The private environment's owner and 0600 permissions are retained. Environment
backups are root-private. On the dev VM this one-time step may require the owner's
local sudo authentication; existing release deployment remains available meanwhile.
For the initial VPS bootstrap, reviewed tool files can be transferred over SSH,
hash-verified, backed up and root-installed before the first live launch.

## Release lifecycle and rollback

1. Verify target host and exact GitHub revision, upload git archive and SHA256.
2. Root controller locks deployment, validates release/commit/checksum and stages
   a root-controlled copy. Unknown hosts and unsafe archive members are refused.
3. Live deployments create a private PostgreSQL custom-format backup plus an
   inspected archive inventory and checksum. Preserve previous release and env.
4. The growup preparer builds a new venv, installs existing requirements, runs
   pip check and compilation, and records pip freeze/requirements checksum.
   Requirements remain version ranges; the package inventory records what was
   actually installed. Previous release environments are retained for rollback.
5. Configure the host's explicit server profile. A transient systemd process uses
   the new release interpreter and systemd-parsed private EnvironmentFile; no
   environment file is sourced as shell code. Validate expected mode/database.
6. Development applies pending migrations before activation. Live runs the new
   `python -m app.migrate --check` read-only inventory and REFUSES pending SQL.
   A production migration needs its own reviewed data/backup checkpoint; no
   unreviewed production SQL is applied by this launcher.
7. Atomically switch current, restart Grow Up, and require BOTH health and database
   readiness on the correct server port. Record commit/mode/port/status in the
   release manifest using the service account, not root writes into app files.
8. Migration/check failure leaves the previous release active. Post-activation
   failure restores the old symlink and environment and restarts it. INT/TERM/HUP
   also invoke recovery. Database backups are never automatically restored and
   application rollback does not undo schema changes. Uncertain interruption
   should be inspected before retrying with a fresh release identifier.

## Resend routing and activation

See RESEND-LIVE-ROUTING-PLAN.md for the exact-path exception. The reviewed
configure-resend-route.py supports preview and --apply on srv1577410 only. It
backs up only the Grow Up Nginx site, changes only the exact POST callback path,
validates nginx -t, reloads gracefully and restores the previous file on failure.
Ordinary pages, docs, command/enrollment routes retain private-preview auth.

No launcher registers a webhook, enables polling, changes provider keys or sends
email. DEV_SERVER reports private callback unavailability gracefully. LIVE_SERVER
routes the same signed callback service but remains disabled until the actual
signing secret, receiving scope and enrollment are configured and separately
activated. A disabled application JSON response is distinct from an Nginx login
failure. Do not remove the general preview gate or replace root-domain mail MX.

## Focused acceptance

Windows: PowerShell syntax parser plus tests/test_deploy_migrations.py,
tests/test_server_target.py and the focused Resend tests. Three Bash execution
cases run on Ubuntu with mocked tools in temporary directories: success, migration
refusal before activation, and health failure with release/environment recovery.
They do not touch systemd, real databases or production configuration. No full
regression is required for this deployment work.

## Shared /var/www layout
Grow Up uses `/var/www/growup` as its own sibling of `iiibooks`, `hebrewgospels`, and `securecodeusa`. Do not recursively chown or chmod `/var/www`; create only the new `/var/www/growup` subtree after confirming the target does not exist.

## Outbox recovery timer (manual install, after verification)

The recovery worker (`python -m app.observability.recovery`) drains the
SQLite outbox to PostgreSQL in bounded one-shot cycles. Templates:
`deployment/growup-outbox-recovery.service` (one-shot, `flock` against
overlap) and `deployment/growup-outbox-recovery.timer`. Deployment
never enables the timer; activate it manually only after the release is
verified healthy.

Scheduling semantics: the timer uses `OnUnitInactiveSec=30s`, so each
run starts 30 seconds after the previous run *finished* — slow cycles
delay the next tick instead of piling up (roughly one cycle per
30s+runtime). Exit codes: 0 ok, 1 partial (some delivered), 2 bad
config, 3 nothing delivered (queue intact, next tick retries), 4
outbox unreachable. Only exit 2 needs config action before retrying.

Prerequisites (run once, as appropriate users):

```bash
# 1. Outbox directory owned by the service account (mode 0700).
sudo -u growup mkdir -p /var/www/growup/shared/observability
sudo chmod 0700 /var/www/growup/shared/observability
# 2. Environment already contains the absolute outbox path; verify:
grep '^OBS_OUTBOX_PATH=' /var/www/growup/shared/growup.env
#    expected: OBS_OUTBOX_PATH=/var/www/growup/shared/observability/events-outbox.sqlite3
```

Install and dry-run (replace `growup-vm` SSH alias as needed):

```bash
# 3. Copy the reviewed templates into systemd (from the deployed release).
sudo cp /var/www/growup/current/deployment/growup-outbox-recovery.service \
        /var/www/growup/current/deployment/growup-outbox-recovery.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
# 4. Dry-run one cycle as the service account; expect exit 0 and a
#    one-line metrics summary. No payloads or secrets are printed.
sudo -u growup /var/www/growup/current/.venv/bin/python \
  -m app.observability.recovery
# 5. Confirm the outbox file exists with 0600 growup:growup ownership.
ls -l /var/www/growup/shared/observability/
```

Activate (only after steps 1–5 pass on the verified release):

```bash
sudo systemctl enable --now growup-outbox-recovery.timer
systemctl list-timers growup-outbox-recovery.timer
journalctl -u growup-outbox-recovery.service --since '5 min ago'
```

Rollback / disable:

```bash
sudo systemctl disable --now growup-outbox-recovery.timer
sudo rm /etc/systemd/system/growup-outbox-recovery.timer \
        /etc/systemd/system/growup-outbox-recovery.service
sudo systemctl daemon-reload
```

Notes:

- Pending outbox rows survive timer downtime; re-enabling the timer
  drains them (oldest first, idempotent delivery).
- `flock` contention exits nonzero by design so overruns surface in
  `systemctl status`; the next tick retries.
- If the release venv lives somewhere other than
  `/var/www/growup/current/.venv`, adjust `ExecStart` before installing.
- Never point two environments (or two timers) at one outbox file.

## Identity reconciliation timer (manual install, after verification)

The reconciler (`python -m app.identity_review_reconcile`) sweeps
research/dossier verdict events already delivered to PostgreSQL and
converts dispute-claiming verdicts into durable identity-review
occurrences. It never reads SQLite, never delivers events, and never
invokes Gabriel or any provider: outbox recovery moves verdicts *into*
PostgreSQL, while reconciliation reads them *out of* PostgreSQL into
the `identity_reviews` queue. The two timers are fully independent —
no ordering between them is required, both are idempotent, and either
may run while the other is stopped.

Templates: `deployment/growup-identity-reconcile.service` (one-shot,
`flock` against overlap on its own lock file) and
`deployment/growup-identity-reconcile.timer`. Deployment never enables
the timer; activate it manually only after the release is verified
healthy. Repository deployment (Windows `deploy-dev.ps1` plus the
remote installer) only ships these templates inside the release
archive; all privileged host configuration below is a separate manual
procedure performed over SSH.

Scheduling semantics: the timer uses `OnUnitInactiveSec=5min`, so
each sweep starts 5 minutes after the previous sweep *finished* —
slow sweeps delay the next tick instead of piling up. Each sweep
examines the complete eligible backlog (no event limit) in bounded
100-row batches; already-reconciled rows are skipped by the database,
so steady-state sweeps are cheap. Exit codes: 0 ok, 1 individual
event failures occurred (details in the journal; failed rows retry
next sweep), 2 bad configuration, 3 PostgreSQL unavailable.

The service sets `TimeoutStartSec=30min`: full-backlog sweeps must
never be killed by systemd's default ~90s start timeout, while a
genuinely wedged run is still terminated instead of holding the lock
forever. Per-verdict commits keep partial progress; the next tick
resumes idempotently.

Prerequisites (run once, as appropriate users):

```bash
# 1. The shared environment file must carry the PostgreSQL variables
#    (no new secrets are needed; reconciliation reuses the app config).
#    Verify NAMES only — never print values:
grep '^DATABASE_' /var/www/growup/shared/growup.env | cut -d= -f1
#    expected names: DATABASE_HOST DATABASE_PORT DATABASE_NAME
#                    DATABASE_USER DATABASE_PASSWORD
#    (DATABASE_HOST/PORT may fall back to localhost/5432.)
# 2. The lock directory is the already-writable shared tree; confirm:
ls -ld /var/www/growup/shared
```

Install and dry-run:

```bash
# 3. Copy the reviewed templates into systemd (from the deployed release).
sudo cp /var/www/growup/current/deployment/growup-identity-reconcile.service \
        /var/www/growup/current/deployment/growup-identity-reconcile.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
# 4. Dry-run one sweep as the service account; expect exit 0 and a
#    one-line counts summary. No payloads or secrets are printed.
sudo -u growup /var/www/growup/current/.venv/bin/python \
  -m app.identity_review_reconcile
```

Activate (only after steps 1–4 pass on the verified release):

```bash
sudo systemctl enable --now growup-identity-reconcile.timer
systemctl list-timers growup-identity-reconcile.timer
journalctl -u growup-identity-reconcile.service --since '15 min ago'
```

Interpreting output: the summary line reports examined, reconciled,
already-reconciled, skipped, failed, and whether more work may
remain. Each `failed <event_id>: <reason>` line names one malformed
or database-failed verdict; failed rows are retried every sweep and
never block later rows. A persistent exit 1 with the same event IDs
means permanently malformed verdict rows need operator inspection
(see the reconciliation design notes); exit 3 means PostgreSQL was
unreachable and the next tick retries.

Rollback / disable:

```bash
sudo systemctl disable --now growup-identity-reconcile.timer
sudo rm /etc/systemd/system/growup-identity-reconcile.timer \
        /etc/systemd/system/growup-identity-reconcile.service
sudo systemctl daemon-reload
```

Notes:

- The units resolve `/var/www/growup/current` (a symlink swapped
  atomically on each release) fresh at every tick, so new releases
  are picked up automatically; old release directories are kept, so
  an in-flight sweep is never broken by a deploy.
- Installed units are copies: after any release that changes these
  templates, re-copy them from the new `current/deployment/`
  directory and `daemon-reload` (the timer need not be
  disabled/enabled again unless its schedule changed).
- If the release venv lives somewhere other than
  `/var/www/growup/current/.venv`, adjust `ExecStart` before installing.
- Never enable this timer on two hosts against one database with the
  expectation of partitioned work — overlapping sweeps are correct
  but duplicate effort; one active timer per database is the
  intended topology.

## Bounded asset monitor (owner activation)

After the deployed release passes PostgreSQL acceptance, run:

```bash
sudo /usr/bin/python3 /var/www/growup/current/deployment/setup-asset-monitor.py --release /var/www/growup/current --apply
```

This installs only `growup-asset-monitor.service` / `.timer`, enables a 15-minute
completion-based cadence and runs one bounded internal check. It uses existing
service credentials without changing them. Its worker is restricted to
`growup_dev/growup_app`, at most 20 dossiers and a 60-second work budget (90-second
systemd limit); no providers or external business actions. The shared emergency
stop and Sabbath window apply. Manual deployment does not enable the timer.
Inspect result with `systemctl --no-pager show growup-asset-monitor.service -p Result
-p ExecMainStatus` and `systemctl list-timers growup-asset-monitor.timer`.
Stop future ticks using `sudo systemctl disable --now growup-asset-monitor.timer`;
stop an active cycle separately using `sudo systemctl stop growup-asset-monitor.service`.
The durable cursor/checkpoints survive stops and release updates. Root README
contains detailed bounds, daily deduplication and failure semantics.

## Expanded isolated US demo catalogue

After normal code/regression acceptance, populate the existing dedicated demo DB:

```bash
sudo /usr/bin/python3 /var/www/growup/current/deployment/prove-demo-catalogue.py
```

This runs 29 bounded synthetic cases using the existing demo-only environment,
role and signing key, and prints stored run keys plus a sanitized proof path.
No provisioning, migrations, operational records, shared configuration, services,
providers or deliveries are changed. Failure preserves partial demo progress;
retry with the same printed `--run-key` to resume. See the root README for the
16-property catalogue, 12-agent GO/NO-GO matrix, synthetic owner-obligation/vault
story and the deliberately unsupported outdoor-admission boundary.

## Targeted downstream acceptance (no full regression)

```bash
sudo /usr/bin/python3 /var/www/growup/current/deployment/prove-downstream-upgrades.py
```

This owner-run development-only wrapper executes one focused canonical recovery
PostgreSQL case plus the adviser, room-portfolio and planning-summary checks in
`growup_mvp_gate`, with provider keys removed and non-loopback networking denied.
It then invokes the existing isolated demo catalogue proof using the separate demo
credentials. It does not run `tests/` or modify application environment files,
service units, operational records or production. The command is deferred when
local sudo authentication is unavailable; Windows targeted results and development
health checks do not stand in for that PostgreSQL proof.


## Public landing, login drawer and CSR (current URLs)

- DEV_SERVER: http://growup.local:8080/ redirects to https://growup.local:8443/.
- LIVE_SERVER: http://growup.securecodeusa.com:8080/ redirects to https://growup.securecodeusa.com:8443/.
- LOGIN retains the existing Basic gateway, issues a short-lived secure owner
  session and opens /wondrue on the same HTTPS host/port. Wondrue API grants remain
  separate. Other operational routes are not exposed by public landing access.
- Registered callback remains https://growup.securecodeusa.com/integrations/resend/webhook
  on standard 443. It must not redirect provider POSTs to UI port 8443.

After deploying reviewed code, root may preview/apply
`python3 /var/www/growup/current/deployment/configure-public-site.py --apply`.
The helper keeps private backups, validates nginx/readiness and preserves existing
credentials/action-mode flags. Dev requires its trusted growup.local certificate
and existing owner gateway file first; Windows needs its growup.local hosts entry.
Then `python3 /var/www/growup/current/deployment/install-csr-worker.py --apply`
installs bounded one-minute queue recovery. Neither command enables actual email
delivery. CSR-only signed intake can be enabled without general campaign tracking.
DEMO_MODE=yes retains replies as drafts; live CSR reply delivery needs an explicit
DEMO_MODE=no and CSR_EMAIL_DELIVERY_APPROVED=1. Do not change global action mode
merely to demonstrate the public page. No new migrations are required.
