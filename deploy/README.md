# Relay private deployment package

Scope: one Linux amd64 host, one shared SQLite database, authenticated API bound to the host's loopback interface, a local follow-up worker and backup maintenance. This package does not provision AWS, enable email sending, or expose a public participant site.

## Build

The Dockerfile pins its Python base by registry digest. `deploy/requirements.lock` is exported from `uv.lock`; pip verifies package hashes. The allowlisted Docker context excludes `.env`, `.data`, databases, grants, browser artifacts and the web UI. The runtime executes as UID/GID 10001, with a read-only root filesystem and no extra Linux capabilities.

```sh
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file deploy/requirements.lock
docker build --platform linux/amd64 -f deploy/Dockerfile -t relay-rehearsal:release .
docker image inspect relay-rehearsal:release --format '{{.Id}}'
```

Use the resulting immutable local image ID in a local rehearsal. For a cloud release, publish to the reviewed private registry and use its immutable digest; that registry/upload is not created here. Rebuild and rerun the rehearsal after changing runtime source.

## Run the isolated package test

```sh
uv run python -m scripts.rehearse_package --image relay-rehearsal:release --output docs/package-rehearsal-new.json
```

The script creates a unique Compose project, fresh synthetic volumes and a random loopback port. It uses no AWS credentials, model calls or real participant data. Its explicit local-test configuration disables remote-backup requirements. It removes only its own test containers and volumes, including on failure.

## Storage provisioning and first initialization

Cloud prerequisites, before launch: encrypted persistent data disk mounted at `/srv/relay`, a recorded filesystem UUID, `/srv/relay/data` and `/srv/relay/backups` owned by UID/GID 10001 with mode 0700, and a private backup bucket with the approved retention/lifecycle and IAM policies. Root and data disks must be distinguishable. Do not bind an unverified host directory and assume Docker proves it is persistent storage.

Copy `runtime.env.example` into the host's `/etc/relay/runtime.env` with mode 0600. Set a generated unique volume identity, immutable image reference, approved backup bucket/prefix and exact Cognito configuration. Never copy the laptop's `.env`, AWS login cache or access keys. Runtime AWS credentials come from the approved instance role. Cloud container access to IMDS must be explicitly verified with IMDSv2 and the appropriate hop limit before enabling model/backup calls. The private operator still needs SSM authorization separately.

Verify the host disk with `mountpoint` and `findmnt`, then initialize once:

```sh
cd /opt/relay
docker compose --env-file /etc/relay/runtime.env -f deploy/compose.yaml -f deploy/compose.host.yaml -p relay-rehearsal run --rm tools init
```

Initialization refuses a nonempty directory. It does not import or delete existing data. All services then require the mount, its matching identity marker, an existing database and absence of restore quarantine. Cloud Compose uses bind mounts from the verified persistent disk; default Compose named volumes are for isolated local tests.

For a synthetic rehearsal, the existing `relay_core.admin` CLI can provision expiring test grants into a mode-0600 file on the data volume. Run it only through the authorized operator session against the initialized database; it is not a public signup route or real participant authentication. Configure actual participants through the existing invitation/Cognito flow. Keep administrative commands stopped during restore; the shared-lock mechanism covers the managed API and worker entrypoints, not arbitrary privileged database scripts.

## Supervision and readiness

Install the reviewed `relay-compose.service` and the dedicated-host Docker drop-in `docker-data-volume.conf`. Configure `/etc/relay/host.env` with `RELAY_FILESYSTEM_UUID=<the actual filesystem UUID>`. The host startup script checks mount identity before starting services. The Docker drop-in prevents container restart policies from racing the disk mount at boot. Do not install this drop-in on the user's development machine or an unrelated shared Docker host.

Compose restarts exited services and rotates container logs. The managed worker handles SIGTERM between bounded batches; maintenance handles SIGTERM between backup cycles. The API uses Uvicorn's shutdown handling. All managed processes hold a shared file lock; restore/release operations require an exclusive lock. One API process and one worker are the initial supported layout.

`/health` is liveness. `/ready` probes database write-lock availability, worker heartbeat (30 seconds), maintenance heartbeat (90 seconds) and last successful backup (30 minutes). Failure returns 503 without database contents or credentials. Readiness is a signal; Docker marks unhealthy containers but does not restart a merely unhealthy process. External alerting/operator response still needs deployment configuration.

Keep the API port forwarded through SSM to `localhost:8765`; no inbound application or SSH security-group rules are required for this architecture. Stop the laptop API first to avoid a port collision. Verify the Cognito callback through the tunnel. This is not access for ordinary external participants without tunnel authorization.

## Backups

Maintenance creates an online SQLite backup every 15 minutes and runs an integrity check. It records a SHA-256 manifest, uses restrictive local permissions and keeps the latest eight successful local backups. In cloud configuration, both the backup and manifest must upload to S3 with server-side encryption before backup health is refreshed. `RELAY_REQUIRE_REMOTE_BACKUP=1` is the default. An absent required destination fails before writing new backups. Repeated incomplete backups are bounded at eight files and require operator attention; failed backups never refresh health.

Local files and S3 objects include sensitive application state. The host volume and cloud bucket must be encrypted and access-restricted. Configure remote retention, monitoring, object verification and disaster-recovery objectives before relying on this operationally. S3 behavior is unit-tested with a fake client here; real bucket/IAM/upload verification is still a deployment gate. Do not call local backups off-host protection.

## Restore and quarantine

1. Stop **all** managed services. Block operator requests and leave external email sending disabled.
2. Retrieve the selected backup and its matching `.json` manifest into the backup mount; verify provenance and recovery point. No automatic download is supplied.
3. Run the restore tool against the selected absolute path:

```sh
docker compose --env-file /etc/relay/runtime.env -f deploy/compose.yaml -f deploy/compose.host.yaml -p relay-rehearsal run --rm tools restore --source /var/backups/relay/SELECTED.sqlite3
```

The tool rejects an active-service lock, checks the backup checksum/integrity, writes a durable quarantine marker before replacing the database, and clears restored health timestamps. API and workers refuse startup while quarantined. An interrupted partial restore remains fenced and needs inspection. Existing local backup files are not deleted by restore.

4. Reconcile post-backup custody, reservations, receipts, grants, commands, notifications and ambiguous provider outcomes. Database rollback cannot undo food movement or an email already sent. Do not replay sending jobs automatically.
5. Record an operator-approved JSON file with `backup_sha256`, `reconciled: true`, a nonempty `reviewer`, and explanatory `notes`. These fields record an accountable human assertion, not automatic proof of reconciliation. Mount this file read-only into a one-off tools container and run `release --evidence /tmp/review.json`. Release requires offline locking and a matching backup hash, appends the approval record, then removes quarantine. Never use the synthetic test assertion on real data.
6. Restart services, verify fresh readiness and review the specific rescue before resuming activity. Preserve the manifest and approval record with the incident evidence.

## Bounded rehearsal and teardown

The supplied stop service/timer stops Relay and powers down the host after 24 hours of host uptime. It must be installed/enabled and verified on the Linux host. A reboot resets this uptime timer; it is not an absolute wall-clock expiration or guaranteed billing cap. Verify EC2 shutdown behavior is **stop**, and add an independent absolute expiry/teardown mechanism in the infrastructure launch plan before budget approval.

Stopping compute does not delete disks, snapshots, S3 objects, registry images or public-IP allocations. Inventory and explicitly retain/delete those resources under the approved teardown plan. Do not use `compose down --volumes` on real deployment data. No host units, timer, registry, bucket or cloud resources have been installed by preparing this package.

## Remaining launch gates

Verify Linux host mount and systemd integration, IMDS/IAM, real remote backup, Cognito through SSM, alarm delivery, absolute expiry and retained-resource teardown. The package's local container tests do not satisfy those cloud-specific gates. Public HTTPS/origin configuration, application request budgets/rate limits and practitioner validation remain outside this private rehearsal package.
