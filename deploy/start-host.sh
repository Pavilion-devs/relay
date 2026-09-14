#!/bin/sh
set -eu
# Host-side verification complements the container marker check. No sourcing of env files.
[ "$#" -eq 2 ] || { echo 'Usage: start-host.sh ENV_FILE EXPECTED_FILESYSTEM_UUID' >&2; exit 2; }
mountpoint -q /srv/relay || { echo 'Persistent host mount absent' >&2; exit 1; }
[ -n "$2" ] && [ "$(findmnt -n -o UUID --target /srv/relay)" = "$2" ] || { echo 'Host filesystem identity mismatch' >&2; exit 1; }
for path in /srv/relay/data /srv/relay/backups; do
  [ -d "$path" ] && [ ! -L "$path" ] || { echo 'Provision storage directories explicitly first' >&2; exit 1; }
done
cd /opt/relay
exec docker compose --env-file "$1" -f deploy/compose.yaml -f deploy/compose.host.yaml -p relay-rehearsal up -d api worker maintenance
