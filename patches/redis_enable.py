"""Enable Redis-backed RQ for Label Studio OSS (run storage sync off the web workers).

Label Studio 1.23 hardcodes ``REDIS_ENABLED = False`` in core/settings/base.py
("OSS version does not support Redis") and pins every RQ queue to
``localhost:6379``. Consequently ``start_job_async_or_sync`` never enqueues to
RQ -- storage sync runs as a background *thread inside a uWSGI worker*, so a
worker recycle (UWSGI_WORKER_RELOAD_ON_RSS / MAX_LIFETIME) kills the sync
mid-flight and the job is marked failed with a stale ``time_last_ping`` (the
"last ping time is too old" error seen on project 5's ~102k-task sync).

The RQ machinery (django_rq, the four queues, the ``rqworker`` management
command) is all present in the OSS image; only the ``REDIS_ENABLED`` flag and the
hardcoded queue host gate it. This module flips the flag and repoints RQ_QUEUES
at an external Redis container, so the sync job (queue ``low``,
job_timeout=RQ_LONG_JOB_TIMEOUT=36000s/10h) is processed by a dedicated rqworker
process that uWSGI never recycles -- the stale-ping failure mode disappears and
multiple syncs can run in parallel across worker replicas.

Applied by appending ``redis_enable.apply(globals())`` to the active settings
module (see 42-redis.sh), after base.py has defined REDIS_ENABLED / RQ_QUEUES.
``globals()`` IS the settings module namespace, so both names are mutated in
place. Gated on REDIS_HOST being set, idempotent, and never raises so it cannot
abort boot.

NOTE: once enabled, the rqworker is load-bearing. EVERY feature that uses
start_job_async_or_sync (storage sync, async exports, etc.) now enqueues to RQ
and needs a worker up to make progress -- if all workers are down, those jobs
queue silently instead of running inline. Keep at least one worker running.

Env:
    REDIS_HOST       Redis hostname (REQUIRED to enable; e.g. 'redis'). Unset => no-op.
    REDIS_PORT       default 6379
    REDIS_DB         default 0
    REDIS_PASSWORD   optional; cleared from queues if unset
"""

import logging
import os

logger = logging.getLogger('redis_enable')


def _int_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error('redis_enable: %s=%r is not an int; using default %s', name, raw, default)
        return default


def apply(settings):
    """Set REDIS_ENABLED=True and point RQ_QUEUES at REDIS_HOST, in place. Never raises.

    ``settings`` is the settings module's globals() dict.
    """
    try:
        host = (os.environ.get('REDIS_HOST') or '').strip()
        if not host:
            logger.warning('redis_enable: REDIS_HOST not set; leaving Redis DISABLED (no-op)')
            return
        port = _int_env('REDIS_PORT', 6379)
        db = _int_env('REDIS_DB', 0)
        password = os.environ.get('REDIS_PASSWORD') or None

        queues = settings.get('RQ_QUEUES') or {}
        for name, cfg in queues.items():
            if not isinstance(cfg, dict):
                continue
            cfg['HOST'] = host
            cfg['PORT'] = port
            cfg['DB'] = db
            if password:
                cfg['PASSWORD'] = password
            else:
                cfg.pop('PASSWORD', None)

        settings['REDIS_ENABLED'] = True
        logger.warning(
            'redis_enable: Redis-backed RQ ENABLED (host=%s:%s db=%s; queues=%s). '
            'rqworker is now load-bearing for sync/exports.',
            host, port, db, ', '.join(sorted(queues)) or '(none)',
        )
    except Exception as e:  # pragma: no cover - must never abort boot
        logger.error('redis_enable: failed to enable Redis: %s', e)
