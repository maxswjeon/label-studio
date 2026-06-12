# Label Studio (OSS) — production Docker setup with runtime patches

A self-hosted [Label Studio](https://labelstud.io/) **1.23.0** (community/OSS)
deployment, tuned for performance and hardened with a set of **runtime patches**
that fix sync reliability, latency, and access-control gaps in the OSS image —
**without forking or rebuilding it**.

The stock `heartexlabs/label-studio:1.23.0` image is used as-is. Each patch is a
small Python module plus an entrypoint shell hook, mounted read-only into the
container and applied at boot. This keeps the setup transparent and trivial to
upgrade: bump the image tag, and the patches re-apply (or become no-ops) on the
next start.

## Topology

```
                   ┌──────────────────────────────────────────┐
   browser ──────► │  labelstudio (uWSGI web, :1800→:8000)    │
                   │   - serves HTTP, runs DB migrations      │
                   └───────────────┬──────────────────────────┘
                                   │ enqueues sync/export jobs
                                   ▼
   ┌─────────────┐         ┌───────────────┐        ┌────────────────────┐
   │   redis     │ ◄─────► │   worker      │ ──────►│  postgres:17       │
   │ (RQ broker) │         │ (rqworker;    │        │  (local DB; bind-  │
   │  7-alpine   │         │  not recycled)│        │  mounted ./pgdata) │
   └─────────────┘         └───────────────┘        └────────────────────┘
```

- **`labelstudio`** — production uWSGI server (multi-worker). Serves HTTP on host
  port `1800`. Its entrypoint runs DB migrations.
- **`worker`** — a dedicated RQ worker (`manage.py rqworker`). Storage sync and
  async exports run here, in a process uWSGI never recycles. Scale with
  `docker compose up -d --scale worker=N` (after removing the worker's
  `container_name`).
- **`redis`** — RQ job broker only (no persistence, `noeviction` so queued jobs
  are never dropped under memory pressure).
- **`postgres:17`** — local DB, co-located on the compose bridge so queries are
  loopback round-trips instead of cross-network calls. Bind-mounted to `./pgdata`
  so the host can back it up directly. (Replaces a prior AWS RDS backend.)

The web and worker containers share **one image, one env block, and the same
patch mounts** via the `x-ls-common` YAML anchor; they differ only in
command/ports.

## Quick start

```bash
cp .env.example .env          # then fill in real values
# also create data/.env with: SECRET_KEY=<random 50-char string>
docker compose up -d
# Label Studio is now on http://localhost:1800
```

Generate the Django secret key:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(50))" > data/.env
```

> **Secrets** live in `.env` (DB + AWS) and `data/.env` (`SECRET_KEY`); both are
> gitignored. See `.env.example` for the full list of variables.

## How the patches are applied

The stock entrypoint sources any `*.sh` it finds in
`/label-studio/deploy/docker-entrypoint.d/<dir>/` before `exec`-ing the main
command, where `<dir>` is:

- `app/` when the command is `label-studio-uwsgi` (the **web** container), or
- `app-docker/` for any other command, e.g. the worker's `rqworker`.

Each patch ships **one shell hook mounted into BOTH dirs**, so the same change
applies to web and worker alike. Every hook:

1. Checks its Python module is mounted and the settings file is writable.
2. Is **idempotent** — guarded by a marker comment, so re-running (or a restart)
   is a no-op.
3. Is **fail-open at startup** — it never aborts boot, but logs loudly on failure
   so a silently-lost patch is visible in the logs.

The hook appends a few lines to the active settings module
(`core/settings/label_studio.py`), which `import`s the mounted Python module and
calls its `apply()` / `install()`. Because this runs *after* `base.py` has
defined `DATABASES`, `MIDDLEWARE`, `RQ_QUEUES`, etc., the modules mutate those
in place.

## The patches

| # | Files | What it fixes |
|---|-------|---------------|
| 40 | `rbac_tier1.py` / `40-rbac-tier1.sh` | **Tier-1 RBAC.** OSS grants every permission to any authenticated org member. Monkeypatches `APIView.get_permissions` to append a denylist permission that blocks **external annotators** (identified by email suffix) from destructive/onboarding actions: project create/delete/modify, org invite fetch+reset, storage/webhook writes. Per-request, so it covers both session and API-token access. |
| 41 | `db_perf.py` / `41-db-perf.sh` | **Persistent DB connections.** LS 1.23 leaves `CONN_MAX_AGE` unset, so Django reopens the Postgres connection every request (~55 ms TLS+auth handshake against the old RDS endpoint). Sets `CONN_MAX_AGE` + health checks in place. Tunable via `LS_CONN_MAX_AGE` / `LS_CONN_HEALTH_CHECKS`. |
| 42 | `redis_enable.py` / `42-redis.sh` | **Enable Redis-backed RQ.** OSS hardcodes `REDIS_ENABLED=False` and pins queues to localhost, so storage sync runs as a *thread inside a uWSGI worker* and dies on worker recycle ("last ping time is too old"). Flips the flag and repoints `RQ_QUEUES` at the `redis` service, so a dedicated rqworker processes the job. **Gated on `REDIS_HOST`** — unset it and this is a no-op. |
| 43 | `storage_health_patch.py` / `43-storage-health.sh` | **Null-job-id guard.** Once Redis is on, a storage stuck `IN_PROGRESS`/`QUEUED` with `last_sync_job=None` makes the storage-list endpoint call `Job.fetch(None)` → `TypeError` → 500, and every source storage vanishes from the UI. Wraps `job_health_check` to self-heal such rows to `FAILED` instead. |
| 44 | `server_timing.py` / `44-server-timing.sh` | **Server-Timing header.** Prepends middleware that emits `Server-Timing: app;dur=…, db;dur=…, total;dur=…` (and `X-Origin-Time-Ms`) on every response, so true origin processing time can be read in devtools and separated from network/proxy latency. No-op on the worker (no HTTP). |

> **Load-bearing note:** once patch 42 enables Redis, the `worker` is required —
> *every* feature using `start_job_async_or_sync` (storage sync, async exports)
> enqueues to RQ. If no worker is running, those jobs queue silently instead of
> running inline. Keep at least one worker up.

## Performance tuning

Set via the `environment` block in `docker-compose.yml` (sized for a
16-core / 32 GB host):

- **uWSGI:** `UWSGI_PROCESSES=8`, raised RSS-reload ceiling, longer worker
  lifetime, and a `300s` harakiri for the heavy sync-trigger request.
- **Postgres:** `shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=64MB`,
  `max_connections=200` (covers 8 uWSGI workers holding persistent connections +
  the worker(s) + headroom). The cluster is `initdb`'d UTF8 / `en_US.UTF-8`.
- `LATEST_VERSION_CHECK=false` skips a per-request pypi.org lookup.

## Backups

`scripts/backup_pg.sh` takes a daily custom-format (`-Fc`) logical dump of the
local Postgres into `./backups/` (gitignored), with 14-day rotation. Install via
the user crontab. Dumps live **outside** `./pgdata` so a corrupt data dir doesn't
take the backups with it. Restore with:

```bash
pg_restore --no-owner --no-acl -d <db> <file>
```

## Upgrading Label Studio

1. Bump the image tag in `docker-compose.yml` (`x-ls-common.image`).
2. `docker compose up -d`.
3. The patch hooks re-apply on boot (or no-op via their markers). **Re-verify the
   patches still apply** against the new version — the OSS internals they target
   (settings module path, `StorageInfo` mixin, `RQ_QUEUES` shape) can shift
   between releases. Each module is defensive and fail-open, so a broken patch
   degrades gracefully and logs the failure rather than blocking startup.

## Repository layout

```
docker-compose.yml      # web + worker + redis + postgres (shared via YAML anchor)
.env.example            # template for the secrets in .env (and data/.env)
patches/                # runtime patches: <NN>-<name>.sh hook + <name>.py module
scripts/backup_pg.sh    # daily pg_dump with rotation
```

Runtime data (`data/`, `pgdata/`, `backups/`) is gitignored.
