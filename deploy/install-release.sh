#!/bin/bash
# Dedicated prepared host only. The operator verifies the outer archive SHA first.
set -euo pipefail
umask 077
[ "$#" -eq 2 ] || { echo 'Usage: install-release.sh VERIFIED_BUNDLE_DIR BACKUP_BUCKET' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || exit 1
[ -f /etc/relay/host-prepared ] || { echo 'Host preparation has not completed' >&2; exit 1; }
/usr/local/sbin/relay-expiry-check
bundle=$(realpath "$1")
bucket=$2
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || exit 1
[ ! -e /etc/relay/runtime.env ] || { echo 'Existing installation; review upgrades separately' >&2; exit 1; }
cd "$bundle"
sha256sum --check SHA256SUMS
docker load --input image.tar
# Docker's containerd image store may report a manifest ID, while a classic daemon
# loads the config ID. Derive the portable ID from the already checksum-verified tar.
expected_image=$(python3 - <<'PY'
import hashlib, json, tarfile
with tarfile.open('image.tar') as archive:
    manifest = json.load(archive.extractfile('manifest.json'))
    assert len(manifest) == 1
    raw = archive.extractfile(manifest[0]['Config']).read()
    config = json.loads(raw)
    assert (config['architecture'], config['os']) == ('amd64', 'linux')
    print('sha256:' + hashlib.sha256(raw).hexdigest())
PY
)
[[ "$expected_image" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 1
[ "$(docker image inspect "$expected_image" --format '{{.Architecture}}/{{.Os}}')" = amd64/linux ] || exit 1
install -d /opt/relay/deploy
install -m 0644 deploy/compose.yaml deploy/compose.host.yaml /opt/relay/deploy/
install -m 0755 deploy/start-host.sh /opt/relay/deploy/
install -m 0644 deploy/relay-compose.service /etc/systemd/system/
install -d /etc/systemd/system/relay-compose.service.d
cat > /etc/systemd/system/relay-compose.service.d/expiry.conf <<'UNIT'
[Service]
ExecStartPre=/usr/local/sbin/relay-expiry-check
UNIT
volume_identity=$(cat /proc/sys/kernel/random/uuid)
cat > /etc/relay/runtime.env <<ENV
RELAY_VOLUME_ID=$volume_identity
RELAY_IMAGE=$expected_image
RELAY_PORT=8765
RELAY_REQUIRE_REMOTE_BACKUP=1
RELAY_BACKUP_BUCKET=$bucket
RELAY_BACKUP_PREFIX=relay
RELAY_MODEL_ID=
RELAY_COGNITO_ISSUER=${RELAY_COGNITO_ISSUER:-}
RELAY_COGNITO_CLIENT_ID=${RELAY_COGNITO_CLIENT_ID:-}
RELAY_COGNITO_DOMAIN=${RELAY_COGNITO_DOMAIN:-}
RELAY_LOGIN_CALLBACK=http://localhost:8765/identity/callback
ENV
# Enforce the UUID before the one-off initialization as well as normal startup.
mountpoint -q /srv/relay
expected_uuid=$(sed -n 's/^RELAY_FILESYSTEM_UUID=//p' /etc/relay/host.env)
[ -n "$expected_uuid" ] && [ "$(findmnt -n -o UUID --target /srv/relay)" = "$expected_uuid" ] || exit 1
cd /opt/relay
docker compose --env-file /etc/relay/runtime.env -f deploy/compose.yaml \
  -f deploy/compose.host.yaml -p relay-rehearsal run --rm tools init
systemctl daemon-reload
systemctl enable --now relay-compose.service
echo 'Services started. Verify readiness, remote backup, identity and recovery before declaring success.'
