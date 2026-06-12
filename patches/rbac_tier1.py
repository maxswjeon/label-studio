"""Tier-1 community RBAC hardening for Label Studio (community edition).

Community LS grants every permission to any authenticated org member (see
core/permissions.py: every permission resolves to ``rules.is_authenticated``),
and the ``permission_required`` declarations on the viewsets are inert in the
community edition. This module adds a surgical denylist so that "external
annotator" accounts (identified by email suffix) cannot perform destructive or
onboarding actions over the API.

Enforcement: we monkeypatch ``rest_framework.views.APIView.get_permissions`` to
append ``RestrictedAnnotatorPermission`` to *every* DRF view. This is done
instead of appending to ``DEFAULT_PERMISSION_CLASSES`` because DRF binds each
view's ``permission_classes`` at class-definition (import) time, so a late
append to the default list never reaches already-defined views. ``get_permissions``
is evaluated per-request, so the patch is immune to import ordering and also
covers views that override ``permission_classes``.

Because it runs inside the DRF request pipeline (after auth resolves
``request.user``), it covers BOTH browser/session access and API-token access
(a plain Django middleware would be bypassable via the annotator's personal
API token).

Scope (Tier 1): blocks project create/delete/modify, org invite link
fetch+reset, and storage/webhook writes. It deliberately does NOT hide other
projects' data or block annotation create/edit -- that requires queryset-level
changes (Tier 2) and is not attempted here.
"""

import logging
import os
import re

from rest_framework.permissions import BasePermission

logger = logging.getLogger('rbac_tier1')

# Accounts whose email ends with this suffix are treated as restricted.
RESTRICTED_EMAIL_SUFFIX = os.environ.get('RBAC_RESTRICTED_EMAIL_SUFFIX', '@ext.example.com')

# (compiled regex matched against request.path via re.search, set of HTTP methods).
# Matched with search() so they are agnostic to any FORCE_SCRIPT_NAME prefix;
# end-anchored where a collection vs. detail distinction matters.
_DENY_RULES = [
    (re.compile(r'/api/projects/?$'), {'POST'}),                       # create project
    (re.compile(r'/api/projects/\d+/?$'), {'DELETE', 'PATCH', 'PUT'}),  # delete / modify project
    (re.compile(r'/api/invite/?$'), {'GET'}),                          # fetch org invite link
    (re.compile(r'/api/invite/reset-token/?$'), {'POST'}),             # rotate invite token
    (re.compile(r'/api/storages/'), {'POST', 'PATCH', 'PUT', 'DELETE'}),  # storage create/change/sync/delete
    (re.compile(r'/api/webhooks/'), {'POST', 'PATCH', 'PUT', 'DELETE'}),  # webhook create/change/delete
]


def _is_restricted(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    # Never restrict staff / superusers.
    if getattr(user, 'is_superuser', False) or getattr(user, 'is_staff', False):
        return False
    return (getattr(user, 'email', '') or '').endswith(RESTRICTED_EMAIL_SUFFIX)


class RestrictedAnnotatorPermission(BasePermission):
    message = 'Action not allowed for external annotator accounts.'

    def has_permission(self, request, view):
        if not _is_restricted(request.user):
            return True
        path = request.path or ''
        method = request.method or ''
        for rx, methods in _DENY_RULES:
            if method in methods and rx.search(path):
                logger.warning('rbac_tier1: denied %s %s for %s', method, path, getattr(request.user, 'email', '?'))
                return False
        return True


_PATCH_FLAG = '_tier1_rbac_patched'


def install():
    """Patch APIView.get_permissions to always enforce the denylist.

    Idempotent and defensive: never raises (logs on failure) so it cannot abort
    application start-up.
    """
    try:
        from rest_framework.views import APIView
    except Exception as e:  # pragma: no cover
        logger.error('rbac_tier1: could not import APIView; patch NOT installed: %s', e)
        return False

    if getattr(APIView, _PATCH_FLAG, False):
        return True

    _orig_get_permissions = APIView.get_permissions

    def _patched_get_permissions(self):
        perms = _orig_get_permissions(self)
        perms.append(RestrictedAnnotatorPermission())
        return perms

    APIView.get_permissions = _patched_get_permissions
    setattr(APIView, _PATCH_FLAG, True)
    logger.warning('rbac_tier1: installed APIView.get_permissions patch (restricted suffix=%s)', RESTRICTED_EMAIL_SUFFIX)
    return True
