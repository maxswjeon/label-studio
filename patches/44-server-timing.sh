#!/usr/bin/env bash
# Server-Timing instrumentation hook.
#
# Prepends server_timing.ServerTimingMiddleware to MIDDLEWARE so every response
# carries a Server-Timing header (origin app vs DB time). Lets you read the true
# origin processing time in browser devtools and subtract it from the
# browser-observed latency to isolate the Cloudflare->HAProxy->origin network tax.
#
# Inserted at the FRONT of MIDDLEWARE so it wraps the whole request (incl. auth /
# session / RBAC middleware DB queries). Idempotent (marker-guarded) and fail-open
# for startup, but logs loudly on failure so a silent loss is visible.
#
# Mounted into both docker-entrypoint.d/app/ (CMD "label-studio-uwsgi", the web)
# and app-docker/ (worker) for parity with the other patches; it's a no-op on the
# worker (no HTTP), and the marker keeps it a no-op if it runs twice.

SETTINGS=/label-studio/label_studio/core/settings/label_studio.py
MODULE=/label-studio/label_studio/server_timing.py
MARKER="# server-timing-instrumentation"

log() { echo "server_timing: $*" >&2; }

if [ ! -f "$MODULE" ]; then
  log "ERROR: $MODULE not mounted; Server-Timing header will NOT be added. Skipping."
  exit 0
fi
if [ ! -w "$SETTINGS" ]; then
  log "ERROR: $SETTINGS not writable; cannot add Server-Timing middleware. Skipping."
  exit 0
fi
if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
  log "already applied; nothing to do."
  exit 0
fi

cat >> "$SETTINGS" <<'PYEOF'

# server-timing-instrumentation
# MIDDLEWARE is defined above (base.py); prepend so our timer wraps the full
# request. Reference by import path string; Django resolves it at startup.
try:
    import server_timing as _server_timing  # noqa: F401  (presence check)
    MIDDLEWARE = ['server_timing.ServerTimingMiddleware', *list(MIDDLEWARE)]
except Exception as _server_timing_e:  # pragma: no cover
    import logging as _server_timing_log
    _server_timing_log.getLogger('server_timing').error(
        'server-timing apply failed: %s', _server_timing_e
    )
PYEOF

log "registered Server-Timing middleware into $SETTINGS"
exit 0
