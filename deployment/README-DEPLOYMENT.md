# Grow Up Ubuntu development deployment (draft)

These scripts are **not yet configured for unattended use**. Nothing is executed by copying them.

## Architecture
- Windows: edit with Muse/VS Code; commit and push to public GitHub.
- Ubuntu VM: separate `growup` Linux user, `/var/www/growup`, Python venv, `growup.service`, loopback port 8001.
- PostgreSQL: existing instance; separate `growup_dev` database and `growup_app` login. Do not modify iiiBooks database.
- Production deployment is deliberately disabled.

## Required manual prerequisites (after server inspection)
1. Confirm SSH alias `securecode-dev` resolves to the intended VM; do not assume the old alias is correct.
2. Verify installed Python, PostgreSQL version, existing ports/services, and free disk space.
3. Create the dedicated Linux user `growup` and give it ownership of `/var/www/growup` only; do not change permissions or ownership of `/var/www` or any sibling application directory.
4. Copy `remote-install-release.sh` to `/var/www/growup/deployment/remote-install-release.sh` and `setup-postgres.sh` to a temporary administrator-only location.
5. Run `setup-postgres.sh` in dry-run mode; review, then use `--apply` to create separate DB/role.
6. Store `DATABASE_URL` and future Meta API credentials in `/var/www/growup/shared/growup.env` with root-owned restricted permissions; never commit them.
7. Install `growup.service` after inspecting template and allow only a narrowly scoped sudoers rule for restarting this specific service (if using `deploy-dev.ps1`).
8. Once Muse creates `app/main.py`, `requirements.txt`, and `deployment/growup_server.py`, run a local smoke test first.
9. Place deployment files under the project's `deployment/` directory, commit and push approved changes, then run `deploy-dev.ps1` from Windows.

## Important notes
- `deploy-dev.ps1` packages exactly the committed GitHub `main` revision; it will refuse uncommitted changes or unpushed commits.
- Installer assumes `GET /health` returns HTTP 2xx and `app.main:app` exists.
- Service and database are **not installed** by the deploy script.
- Rollback restores the previous application release when available; it does **not** roll back database migrations.
- The existing Shorashim SSH helper was intentionally not reused: its verification function hardcodes `shorashim.service`.
- No Nginx route is configured yet. Access should remain local or through SSH port forwarding until deliberately configured.

## Migration before activation (OPS-2)

The deployment controller (`deployment/growup-install-release`, installed
as `/usr/local/sbin/growup-install-release`) runs pending PostgreSQL
migrations **before activation**, in this order:

1. Validate the release name, archive, and checksum.
2. Stage the archive and prepare the new release as `growup`.
3. Execute `python -m app.migrate` in a transient unit via
   `systemd-run --wait --pipe` with `User=growup`, `Group=growup`,
   `WorkingDirectory=/var/www/growup/releases/<RELEASE>`,
   `EnvironmentFile=/var/www/growup/shared/growup.env`, and the
   **new** release's interpreter
   (`/var/www/growup/releases/<RELEASE>/.venv/bin/python`).
   systemd parses the env file as `KEY=VALUE` assignments; it is never
   sourced as shell code. `--wait --pipe` propagates the migration
   exit status, so any nonzero result aborts before activation.
4. Abort with exit 1 if migrations fail — the previous release stays
   active and the service is not restarted. No rollback is needed
   because nothing was activated.
5. Only after migrations succeed: activate the symlink, restart
   `growup.service`, and verify health (existing rollback on failure).

Migration output (`applied: <file>` or `up to date: no pending
migrations`) appears in the deploy log before the activation message;
its absence on a release carrying a new migration means the installed
controller predates this fix.

Migration compatibility rule: a migration executes while the **old**
release is still serving, so every migration must be backward compatible
with the running application (additive changes only — new tables,
nullable columns; no renames, drops, or tightening constraints relied
on by old code). The runner (`app/migrate.py`) applies all pending
files in one transaction and records them in `schema_migrations`, so a
failed run leaves the schema unchanged; reruns are safe. There are no
automatic down-migrations: application rollback never reverts schema
changes.

Installing a reviewed controller update (privileged, manual — the live
controller is outside Git, so editing the repository does not update it):

```bash
# 1. From the deployed release, diff the reviewed source against live.
diff -u /usr/local/sbin/growup-install-release \
  /var/www/growup/current/deployment/growup-install-release
# 2. Syntax-check the reviewed source (no execution).
bash -n /var/www/growup/current/deployment/growup-install-release
# 3. Install with root ownership and restrictive mode; verify.
sudo install -o root -g root -m 755 \
  /var/www/growup/current/deployment/growup-install-release \
  /usr/local/sbin/growup-install-release
ls -l /usr/local/sbin/growup-install-release
sha256sum /usr/local/sbin/growup-install-release \
  /var/www/growup/current/deployment/growup-install-release
# 4. Confirm exit-status propagation on the host (no app involved):
sudo systemd-run --wait --pipe --quiet --collect -p User=growup \
  -- /bin/false; test $? -ne 0 && echo PROPAGATION_OK
# 5. Verify on the next deploy: the log must show the migration step
#    before "release activated", or "release not activated" with the
#    previous release still live if migrations fail.
```

Rollback of a controller update: re-install the previous reviewed copy
from the still-active release's `deployment/` directory and re-verify
with `bash -n` and `diff`.

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
