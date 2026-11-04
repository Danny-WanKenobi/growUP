#!/usr/bin/env bash
# Trusted root-installed preparation script; runs as growup, never as root.
set -Eeuo pipefail
umask 0027
RELEASE=${1:?release required}
ARCHIVE=${2:?archive required}
ROOT=/var/www/growup
[[ "$RELEASE" =~ ^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{7}$ ]] || exit 2
[[ "$ARCHIVE" == "$ROOT/.deploy-staging/$RELEASE.tar" && -f "$ARCHIVE" ]] || exit 2
DEST="$ROOT/releases/$RELEASE"
[[ ! -e "$DEST" ]] || { echo 'Release already exists' >&2; exit 1; }
mkdir -m 750 "$DEST"
python3 - "$ARCHIVE" <<'PY'
import pathlib,sys,tarfile
with tarfile.open(sys.argv[1],'r:') as archive:
    for item in archive:
        name=pathlib.PurePosixPath(item.name)
        if name.is_absolute() or '..' in name.parts or not (item.isfile() or item.isdir()):
            raise SystemExit('Unsafe archive member')
PY
tar -xf "$ARCHIVE" -C "$DEST" --no-same-owner --no-same-permissions
[[ -f "$DEST/requirements.txt" && -f "$DEST/app/main.py" ]] || exit 1
/usr/bin/python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/python" -m pip install --disable-pip-version-check -r "$DEST/requirements.txt"
"$DEST/.venv/bin/python" -m pip check
"$DEST/.venv/bin/python" -m pip freeze --all > "$DEST/RELEASE-PACKAGES.txt"
sha256sum "$DEST/requirements.txt" > "$DEST/REQUIREMENTS.sha256"
"$DEST/.venv/bin/python" -m compileall -q "$DEST/app"
printf 'release=%s\n' "$RELEASE" > "$DEST/RELEASE-MANIFEST.txt"
echo "Prepared release: $DEST"
