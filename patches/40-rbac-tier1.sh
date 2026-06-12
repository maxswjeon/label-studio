#!/usr/bin/env bash
# Tier-1 community RBAC hardening hook.
#
# Registers the RestrictedAnnotatorPermission DRF permission class by appending
# one line to the active settings module. Idempotent (guarded by a marker) and
# fail-open for STARTUP (never aborts boot) -- but it logs loudly on failure so a
# silent loss of the control is visible. The control itself is fail-CLOSED at
# request time (denylisted actions are denied).
#
# Mounted into /label-studio/deploy/docker-entrypoint.d/app-docker/ which the
# stock entrypoint sources (CMD "label-studio" -> else-branch) before exec.

SETTINGS=/label-studio/label_studio/core/settings/label_studio.py
MODULE=/label-studio/label_studio/rbac_tier1.py
MARKER="# tier1-rbac-registration"

log() { echo "tier1-rbac: $*" >&2; }

if [ ! -f "$MODULE" ]; then
  log "ERROR: $MODULE not mounted; permission class will be missing. Skipping settings patch."
  exit 0
fi
if [ ! -w "$SETTINGS" ]; then
  log "ERROR: $SETTINGS not writable; cannot register permission class. Skipping."
  exit 0
fi
if grep -q "$MARKER" "$SETTINGS" 2>/dev/null; then
  log "already registered; nothing to do."
  exit 0
fi

cat >> "$SETTINGS" <<'PYEOF'

# tier1-rbac-registration
# Install the restricted-annotator enforcement by patching APIView.get_permissions
# (per-request, so immune to import ordering and per-view permission_classes overrides).
try:
    import rbac_tier1 as _tier1_rbac
    _tier1_rbac.install()
except Exception as _tier1_e:  # pragma: no cover
    import logging as _tier1_log
    _tier1_log.getLogger('rbac_tier1').error('tier1-rbac install failed: %s', _tier1_e)
PYEOF

log "installed RBAC hook into $SETTINGS"
exit 0
