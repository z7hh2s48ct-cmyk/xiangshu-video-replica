# PostgreSQL 16 staging/production contract

Customer V3 uses PostgreSQL as its only data source. The staging environment
must use PostgreSQL 16 with HA rather than a local developer container. A
managed HA service is preferred; a self-managed cluster is acceptable only
when it exposes one stable primary/read-write endpoint and has automatic
failover owned by the database layer.

## Required boundary

- Keep every database node and the primary endpoint on a private network. Do
  not expose PostgreSQL to the 公网.
- Require TLS on the application DSN, restrict the security group/firewall to
  the API/Worker hosts, and use a dedicated least-privilege application role.
  Customer production rejects a missing `sslmode` and the libpq downgrade-capable
  `disable`, `allow` and `prefer` modes. Deploy the provider/private CA as a
  read-only file and use `sslmode=verify-full&sslrootcert=...`, so both the
  certificate chain and the HA endpoint hostname are verified.
- Point every API and Worker at the same read/write primary endpoint. This
  application does not route reads to replicas.
- Budget connections for two API processes, four Workers, maintenance and one
  migration session. Tune the per-process `VIDEO_REPLICA_PG_POOL_MAX` so the
  total remains below the service connection limit with failover headroom.
- Run `deploy/postgres/migrate.sh` from one designated migration host before
  rolling application processes. Its `flock` is host-local and is not a
  distributed migration lock. The script runs the customer PostgreSQL/TLS
  validator before Alembic can touch the target.
- After the complete migration, a truly empty database may use the guarded
  `app.bootstrap provision-empty-customer` one-shot command to atomically seed
  its first administrator and encrypted COS settings. The command refuses any
  target with users, provider settings, audit history, or a previously updated
  runtime-settings row; never use it as an operator or settings update path.
- Never place a real DSN, password, snapshot URL or database dump in Git or CI
  logs.

## T38 physical backup and point-in-time recovery

Customer production uses physical PostgreSQL base backups plus a continuous,
externally retrievable WAL archive. `pg_dump` and application-managed exports
are not recovery inputs for this task. The designated database owner must first
enable `wal_level=replica` (or `logical`), `archive_mode=on` (or `always`) and
an `archive_command` or `archive_library`, then prove a forced WAL segment is
retrievable before taking the first base backup.

`pitr-preflight.sh` and `pitr-backup.sh` use a separate libpq service file:

- `/etc/video-replica/backup.pg_service.conf` and its referenced password
  source are root/postgres controlled and never appear in `customer.env`, Git,
  process arguments or logs.
- The backup identity is distinct from `VIDEO_REPLICA_DATABASE_URL`; give it
  only the PostgreSQL backup/monitor permissions required by the managed
  service. Do not grant the application role physical backup privileges.
- The local backup and recovery roots are `0700` and owned by `postgres`.
  `video-replica-pitr-backup.service` runs as that account with `UMask=0077`.

The operator supplies one root-owned executable at
`VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER`. It is deliberately outside this
repository so the selected backup platform retains its own encryption keys,
immutable-object controls, and cross-region credentials. Its fixed contract is:

```text
assert-wal WAL
put-wal POSTGRESQL_SOURCE_PATH WAL
put-base LABEL LOCAL_DIRECTORY
assert-base LABEL
get-base LABEL EMPTY_LOCAL_DIRECTORY
get-wal WAL POSTGRESQL_DESTINATION
```

`assert-*` must fail until the object is independently readable from the
designated off-site archive. `get-wal` must write only the destination provided
by PostgreSQL and return nonzero on absence or integrity failure. The helper
must never print credentials, DSNs, object URLs, or raw customer records.
Configure the database-side archive command (or its equivalent archive
library) to call `put-wal` with PostgreSQL's quoted `%p` and `%f` placeholders;
the helper must preserve the WAL filename, including eight-hex-digit timeline
`.history` files.

Run the daily base backup only through
`video-replica-pitr-backup.timer`; the legacy
`video-replica-backup.timer` is internal SQLite P0 tooling and must not be
installed for customer PostgreSQL. The staging recovery procedure and its exact
100-fact verifier are run as part of the operator runbook. Until that controlled
drill succeeds and its timing/evidence is recorded, T38 remains
`AUTOMATED_VERIFIED`, not `STAGING_VERIFIED` or `PRODUCTION_GO`.
