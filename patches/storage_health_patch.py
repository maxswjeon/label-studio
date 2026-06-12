"""Null-job-id guard for Label Studio storage health checks.

When Redis/RQ is enabled, the storage list endpoint runs
``StorageInfo.job_health_check`` for every storage whose status is IN_PROGRESS or
QUEUED (io_storages/api.py get_queryset -> ensure_storage_statuses -> health_check
-> job_health_check). Upstream (1.23.0) does:

    sync_job = Job.fetch(self.last_sync_job, connection=queue.connection)

with NO guard on ``last_sync_job`` being None. A storage can legitimately reach
IN_PROGRESS/QUEUED with ``last_sync_job=None`` -- e.g. a pre-Redis *threaded*
sync (which never stored an RQ job id) or an enqueue interrupted before the id
was saved. ``Job.fetch(None)`` then raises ``TypeError: argument of type
'NoneType' is not iterable`` (rq.job.parse_job_id does ``':' in None``), which is
NOT caught by the method's ``except NoSuchJobError`` -- so the whole storage-list
API 500s and every source storage vanishes from the UI for that project.

This patch wraps job_health_check: when ``last_sync_job`` is falsy it skips the
Job.fetch entirely and self-heals the row to FAILED (the terminal state upstream
would have reached via its 'not found' branch), then returns. The normal path
(real job id) is delegated unchanged to the original method.

Patching a *model mixin* means we must wait until Django's app registry is
populated -- unlike the rbac patch (DRF APIView, importable at settings-import
time). install() therefore applies immediately if apps are already ready, else
defers to the first request_started signal (registry guaranteed populated by
then). Idempotent and never raises, so it cannot abort boot.
"""

import logging

logger = logging.getLogger('storage_health_patch')

_PATCH_FLAG = '_null_jobid_guard_patched'
_UID = 'storage_health_patch'


def _apply_patch():
    """Wrap StorageInfo.job_health_check with the null-job-id guard. Idempotent."""
    from io_storages.base_models import StorageInfo

    if getattr(StorageInfo, _PATCH_FLAG, False):
        return True

    _orig_job_health_check = StorageInfo.job_health_check

    def _patched_job_health_check(self):
        # Only divert the broken combo upstream cannot handle: active status but
        # no RQ job id to fetch. Everything else uses the stock implementation.
        if not self.last_sync_job:
            Status = self.Status
            if self.status in (Status.IN_PROGRESS, Status.QUEUED):
                self.status = Status.FAILED
                self.traceback = (
                    'Sync was marked active but no RQ job id was recorded '
                    '(e.g. a pre-Redis threaded sync, or an enqueue interrupted '
                    'before the job id was saved). Marked failed; re-run the sync.'
                )
                self.save(update_fields=['status', 'traceback'])
                logger.info(
                    'storage_health_patch: storage %s had status=%s with no '
                    'last_sync_job; moved to failed', self, self.status,
                )
            return
        return _orig_job_health_check(self)

    StorageInfo.job_health_check = _patched_job_health_check
    setattr(StorageInfo, _PATCH_FLAG, True)
    logger.warning('storage_health_patch: installed job_health_check null-job-id guard')
    return True


def install():
    """Install the guard now if apps are ready, otherwise on first request."""
    try:
        from django.apps import apps
        if apps.ready:
            return _apply_patch()
    except Exception as e:  # pragma: no cover
        logger.error('storage_health_patch: ready-check failed, will defer: %s', e)

    try:
        from django.core.signals import request_started

        def _on_request(sender, **kwargs):
            try:
                _apply_patch()
            except Exception as e:  # pragma: no cover
                logger.error('storage_health_patch: apply on request_started failed: %s', e)
            finally:
                request_started.disconnect(dispatch_uid=_UID)

        request_started.connect(_on_request, weak=False, dispatch_uid=_UID)
        logger.warning('storage_health_patch: scheduled on first request (apps not yet ready)')
        return True
    except Exception as e:  # pragma: no cover
        logger.error('storage_health_patch: could not schedule patch: %s', e)
        return False
