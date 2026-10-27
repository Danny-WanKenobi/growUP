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
