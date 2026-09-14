#!/bin/bash
# Embedded by scripts/prepare_aws_launch.py; runs only on the new dedicated EC2 host.
set -euo pipefail
umask 077
expiry='@@EXPIRY@@'
volume_id='@@VOLUME@@'

# Install expiry before package installation or waiting for the data attachment.
install -d -m 0700 /etc/relay
printf '%s\n' "$expiry" > /etc/relay/expires-utc
cat > /usr/local/sbin/relay-expiry-check <<'CHECK'
#!/bin/bash
set -euo pipefail
deadline=$(date -u -d "$(cat /etc/relay/expires-utc)Z" +%s)
if [ "$(date -u +%s)" -ge "$deadline" ]; then
  shutdown -h now
  exit 1
fi
CHECK
chmod 0700 /usr/local/sbin/relay-expiry-check
cat > /etc/systemd/system/relay-expiry.service <<'UNIT'
[Unit]
Description=Enforce the absolute Relay rehearsal deadline
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/relay-expiry-check
UNIT
cat > /etc/systemd/system/relay-expiry.timer <<'UNIT'
[Unit]
Description=Check Relay expiry at boot and every minute
[Timer]
OnBootSec=15s
OnUnitActiveSec=60s
AccuracySec=1s
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now relay-expiry.timer
/usr/local/sbin/relay-expiry-check
systemctl enable --now amazon-ssm-agent
dnf install -y docker

# Select only the separately created volume by its Nitro serial, never by disk order.
expected_serial=$(printf '%s' "$volume_id" | tr -d '-')
device=''
for attempt in $(seq 1 120); do
  for serial_file in /sys/block/nvme*n1/device/serial; do
    [ -f "$serial_file" ] || continue
    serial=$(tr -d '[:space:]' < "$serial_file")
    if [ "$serial" = "$expected_serial" ]; then
      disk=$(basename "$(dirname "$(dirname "$serial_file")")")
      device=/dev/$disk
      break
    fi
  done
  [ -n "$device" ] && break
  sleep 2
done
[ -b "$device" ] || { echo 'Expected data EBS volume absent' >&2; exit 1; }
# A signature of any kind prevents automatic formatting. Existing filesystems need review.
[ -z "$(wipefs --noheadings --output TYPE "$device")" ] || {
  echo 'Data disk is not blank; refusing automatic initialization' >&2; exit 1;
}
[ "$(lsblk -nr -o NAME "$device" | wc -l)" -eq 1 ] || exit 1
mkfs.ext4 -q "$device"
uuid=$(blkid -s UUID -o value "$device")
[ -n "$uuid" ] || exit 1
install -d /srv/relay
printf 'UUID=%s /srv/relay ext4 defaults 0 2\n' "$uuid" >> /etc/fstab
mount /srv/relay
[ "$(findmnt -n -o UUID --target /srv/relay)" = "$uuid" ] || exit 1
install -d -m 0700 -o 10001 -g 10001 /srv/relay/data /srv/relay/backups
printf 'RELAY_FILESYSTEM_UUID=%s\n' "$uuid" > /etc/relay/host.env
install -d /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/relay-volume.conf <<'UNIT'
[Unit]
RequiresMountsFor=/srv/relay
[Service]
ExecStartPre=/usr/local/sbin/relay-expiry-check
UNIT
systemctl daemon-reload
systemctl enable --now docker

# Fixed official binary and independently recorded release checksum.
install -d /usr/local/lib/docker/cli-plugins
curl --fail --location --retry 3 --max-time 180 \
  https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose.partial
printf '%s  %s\n' \
  7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4 \
  /usr/local/lib/docker/cli-plugins/docker-compose.partial | sha256sum --check --status
mv /usr/local/lib/docker/cli-plugins/docker-compose.partial \
  /usr/local/lib/docker/cli-plugins/docker-compose
chmod 0755 /usr/local/lib/docker/cli-plugins/docker-compose
docker compose version
/usr/local/sbin/relay-expiry-check
touch /etc/relay/host-prepared
# App installation is a separate checksum-verified operator step, described in aws-launch.md.
