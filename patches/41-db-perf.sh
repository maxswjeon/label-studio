#!/usr/bin/env bash
# Database connection tuning hook.
#
# Enables persistent Postgres connections (CONN_MAX_AGE) by appending a call to
# db_perf.apply(DATABASES) at the end of the active settings module. Label Studio
# 1.23 leaves CONN_MAX_AGE unset, so every request reopens the RDS connection
# (~55 ms TLS+auth handshake measured against the Aurora cluster endpoint).
# Idempotent (guarded by a marker) and fail-open for STARTUP (never aborts boot)
# -- but it logs loudly on failure so a silent loss of the tuning is visible.
#
# Mounted into both docker-entrypoint.d/app/ (CMD "label-studio-uwsgi") and
# app-docker/ (CMD "label-studio"), so it applies whichever command is used. The
# stock entrypoint sources the matching dir before exec; the marker keeps it a
# no-op if it somehow runs twice.

SETTINGS=/label-studio/label_studio/core/settings/label_studio.py
MODULE=/label-studio/label_studio/db_perf.py
MARKER="# db-perf-tuning"

log() { echo "db_perf: $*" >&2; }

if [ ! -f "$MODULE" ]; then
  log "ERROR: $MODULE not mounted; persistent connections will NOT be enabled. Skipping."
  exit 0
fi
if [ ! -w "$SETTINGS" ]; then
  log "ERROR: $SETTINGS not writable; cannot enable persistent connections. Skipping."
  exit 0
fi
if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
  log "already applied; nothing to do."
  exit 0
fi

cat >> "$SETTINGS" <<'PYEOF'

# db-perf-tuning
# Enable persistent Postgres connections. DATABASES is defined above (base.py),
# so we mutate it in place; per-request, Django reads CONN_MAX_AGE from settings.
try:
    import db_perf as _db_perf
    _db_perf.apply(DATABASES)
except Exception as _db_perf_e:  # pragma: no cover
    import logging as _db_perf_log
    _db_perf_log.getLogger('db_perf').error('db-perf apply failed: %s', _db_perf_e)
PYEOF

log "registered DB tuning hook into $SETTINGS"
exit 0
