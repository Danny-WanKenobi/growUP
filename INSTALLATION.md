# Grow Up deployment fix

Windows is the entry point: `deployment/deploy-dev.ps1`.

## Installation on Ubuntu (once, manually as ubuntu with sudo)

Copy `remote-install-release.sh` and `growup-install-release` from Windows to
`/tmp` on Ubuntu. Install them as root-owned files:

```
sudo install -d -o root -g root -m 755 /usr/local/libexec/growup
sudo install -o root -g root -m 755 /tmp/growup-install-release /usr/local/sbin/growup-install-release
sudo install -o root -g root -m 755 /tmp/remote-install-release.sh /usr/local/libexec/growup/remote-install-release.sh
sudo visudo -f /etc/sudoers.d/growup-deployer
```

In visudo, add exactly:

```
ubuntu ALL=(root) NOPASSWD: /usr/local/sbin/growup-install-release *
```

The controller validates the single release-name argument; the sudoers rule
only permits the root-owned controller. Verify with:

```
sudo visudo -cf /etc/sudoers.d/growup-deployer
sudo -l -U ubuntu
```

**Important**: Do not grant `growup` sudo privileges. Do not enable SSH login
for `growup`. The root-owned controller calls the preparation script as
`growup` and restarts only `growup.service`.

## First deployment prerequisites

The committed `main` branch must include `app/main.py`, `requirements.txt`,
and a working GET `/health` endpoint. The `growup.service` unit must be
configured to run `app.main:app` on `127.0.0.1:8001`.

The PowerShell script requires a clean Git working tree and the exact HEAD
commit already pushed to GitHub `origin/main`.

From Windows:

```
.\deployment\deploy-dev.ps1
```

No first deployment should be attempted until application code exists.

## Windows GUADE (Windows development only)

GUADE runs only on Windows. This section is separate from the application
deployment above. The authoritative operator detail is
[the GUADE operating instructions](guade/OPERATING.md); do not recreate
the activation trial artifact (`documentation/guade-activation-trial.md`),
and preserve the exhausted historical ledgers (`guade/state/`,
`guade/state-operating/`, and the overnight ledger) read-only.

### Project venv

Run every command from the Windows project root with the project venv
interpreter, not a system Python. Global options precede the subcommand:

```powershell
Set-Location -LiteralPath 'C:\Code\growUP'
.\.venv\Scripts\python.exe -B -m guade.cli --state-dir guade/state-validation --config guade/config.validation.example.json check
```

### Credential presence check (no secret values)

The `check` command above is read-only: it creates no ledger, runs no
engineering CLI, and makes no provider or authentication call. It reports
only `BASETEN_API_KEY: SET` or `ABSENT` plus the source label, and never
prints a key value. The loader checks, in order, the process environment,
the Windows user environment, then `GUADE_ENV_FILE` /
`~/.config/growup/guade.env` / the readable Ubuntu shared file (file
alias `META_API_KEY`). It does not read Windows Credential Manager.
Presence is not live authentication and grants no spending authority;
`check` also reports `BLOCKED` when the ledger envelope is exhausted.

### Separate spending approval

`check`, a config comment, and `--approved-live` each acknowledge a grant;
none creates owner permission. Any live run (`run-task`, `run-campaign`,
or `run-demo`) requires separate owner spending approval plus
`--approved-live`, and only an uppercase `APPROVE` (optionally followed by
` - reason`) authorizes dispatch. Use a new `guade/state-*` directory;
never reset or reuse a spent ledger. The dollar figure in a config limits
estimated planning exposure only — it is not a provider-enforced billing
cap, and Meta dollar spend is not enforced by this controller.

### Exact task allowlists

`run-task` accepts exactly one task; `run-campaign` processes a finite,
owner-reviewed list in order and generates no follow-on work. Each JSON
task requires exactly `id`, `title`, `prompt`, `allowed_files`, and
`tests` (existing `tests/*.py` files, never shell commands).
`allowed_files` are exact repository paths, checked before and after
tests; runtime controls and secret paths cannot be allowed, and
unexpected edits stop execution for owner inspection. These are
fail-closed completion checks, not an OS sandbox. Example, only after
separate approval:

```powershell
.\.venv\Scripts\python.exe -B -m guade.cli --state-dir guade/state-validation --config guade/config.validation.example.json run-task --tasks guade/tasks.validation.example.json --approved-live
```

### STOP-file handling

To stop, from another PowerShell window write `STOP` into the active
state directory (use `guade/state-campaign` for an approved campaign):

```powershell
Set-Content -LiteralPath 'C:\Code\growUP\guade\state-validation\STOP' -Value 'owner stop'
```

Stop checks run before/after coach calls, before dispatch, during
subprocess polling, before tests, and between tasks. Afterwards inspect
`ledger.json`, `audit.jsonl`, `authorization.json`, `tasks.json`, and
`mission.json` in that directory. Do not remove `STOP`, reset a spent
ledger, raise limits, or remove a stale `guade/mission.lock` to bypass
reconciliation; confirm processes stopped and reconcile ledger/provider
outcomes first.

### Windows-to-Git-to-Ubuntu boundary

Windows hosts development and GUADE execution. Git/GitHub transfers
owner-approved revisions only. Ubuntu verifies the deployed application
and PostgreSQL (see the deployment sections above). Do not install or
authenticate an engineering CLI on Ubuntu for GUADE execution. GUADE
itself performs no Git writes, deployment, network outreach, or database
actions; promotion and deployment remain separate owner-approved steps.
