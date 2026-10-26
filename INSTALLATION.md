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
