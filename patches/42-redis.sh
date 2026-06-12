#!/usr/bin/env bash
# Redis / RQ enablement hook.
#
# Label Studio 1.23 OSS hardcodes REDIS_ENABLED=False and pins RQ_QUEUES to
# localhost, so storage sync runs as a thread inside a uWSGI worker and dies on
# worker recycle ("last ping time is too old"). This appends
# redis_enable.apply(globals()) to the end of the active settings module so the
# flag is flipped and the queues point at an external Redis container -- letting a
# dedicated rqworker process the sync instead of the web workers.
#
# Gated on REDIS_HOST (read inside redis_enable.apply): if unset, this is a no-op,
# so mounting the patch without a redis service / REDIS_HOST env changes nothing.
# Idempotent (marker guard) and fail-open for STARTUP (never aborts boot), but it
# logs loudly on failure so a silent loss of the change is visible.
#
# Mounted into BOTH docker-entrypoint.d/app/ (CMD "label-studio-uwsgi" -> the web
# container) and app-docker/ (the fallback dir used when CMD is the rqworker ->
# the worker container), so the SAME settings change applies in both. Mirrors the
# 41-db-perf.sh pattern.

SETTINGS=/label-studio/label_studio/core/settings/label_studio.py
MODULE=/label-studio/label_studio/redis_enable.py
MARKER="# redis-enable"

log() { echo "redis_enable: $*" >&2; }

if [ ! -f "$MODULE" ]; then
  log "ERROR: $MODULE not mounted; Redis will NOT be enabled. Skipping."
  exit 0
fi
if [ ! -w "$SETTINGS" ]; then
  log "ERROR: $SETTINGS not writable; cannot enable Redis. Skipping."
  exit 0
fi
if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
  log "already applied; nothing to do."
  exit 0
fi

cat >> "$SETTINGS" <<'PYEOF'

# redis-enable
# Flip REDIS_ENABLED and repoint RQ_QUEUES at the external Redis (gated on
# REDIS_HOST). globals() is the settings module namespace, so REDIS_ENABLED and
# RQ_QUEUES (defined above by base.py) are mutated in place.
try:
    import redis_enable as _redis_enable
    _redis_enable.apply(globals())
except Exception as _redis_enable_e:  # pragma: no cover
    import logging as _redis_enable_log
    _redis_enable_log.getLogger('redis_enable').error('redis-enable apply failed: %s', _redis_enable_e)
PYEOF

log "registered Redis enablement hook into $SETTINGS"
exit 0
