#!/usr/bin/env bash
# Storage health-check hardening hook.
#
# Installs a null-job-id guard around StorageInfo.job_health_check (see
# storage_health_patch.py). Without it, a storage stuck IN_PROGRESS/QUEUED with
# last_sync_job=None makes the storage-list API call Job.fetch(None) -> TypeError
# -> 500, and every source storage disappears from the project UI. This is only
# reachable once Redis is enabled (job_health_check runs only when redis_connected).
#
# Appends storage_health_patch.install() to the active settings module. The patch
# targets a model mixin, so install() defers to the first request if the app
# registry isn't ready yet. Idempotent (marker guard) and fail-open for STARTUP.
#
# Mounted into both docker-entrypoint.d/app/ (web) and app-docker/ (worker), like
# the other patches.

SETTINGS=/label-studio/label_studio/core/settings/label_studio.py
MODULE=/label-studio/label_studio/storage_health_patch.py
MARKER="# storage-health-guard"

log() { echo "storage_health_patch: $*" >&2; }

if [ ! -f "$MODULE" ]; then
  log "ERROR: $MODULE not mounted; storage-list 500 guard will be missing. Skipping."
  exit 0
fi
if [ ! -w "$SETTINGS" ]; then
  log "ERROR: $SETTINGS not writable; cannot install guard. Skipping."
  exit 0
fi
if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
  log "already applied; nothing to do."
  exit 0
fi

cat >> "$SETTINGS" <<'PYEOF'

# storage-health-guard
# Guard StorageInfo.job_health_check against last_sync_job=None (would otherwise
# 500 the storage-list endpoint and hide all storages). Deferred to app-ready.
try:
    import storage_health_patch as _storage_health_patch
    _storage_health_patch.install()
except Exception as _storage_health_e:  # pragma: no cover
    import logging as _storage_health_log
    _storage_health_log.getLogger('storage_health_patch').error('install failed: %s', _storage_health_e)
PYEOF

log "registered storage-health guard into $SETTINGS"
exit 0
