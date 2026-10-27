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
