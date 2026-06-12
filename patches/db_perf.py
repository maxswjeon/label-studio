"""Database connection tuning for Label Studio (production / RDS Aurora).

Label Studio 1.23 builds ``DATABASES`` without ``CONN_MAX_AGE`` (see
core/settings/base.py), so Django closes and reopens the Postgres connection on
*every* request. Against the Aurora cluster endpoint that TCP + TLS + auth
handshake measured ~55 ms per request -- paid before any query runs, on top of
~4.7 ms round-trip per query. Enabling persistent connections keeps one
connection alive per uWSGI worker across requests, removing that reconnect cost.

Applied by appending a call to ``apply(DATABASES)`` at the end of the active
settings module (see 41-db-perf.sh), at which point ``DATABASES`` is already
defined by base.py. The mutation is in-place, idempotent, and never raises so it
cannot abort application start-up.

Persistent connections (not psycopg3 pooling) are used deliberately: the stock
uwsgi.ini runs *process-based* workers, each holding a single DB connection
(~8 total against Aurora's max_connections=401), so ``CONN_MAX_AGE`` is the
simpler, conflict-free choice. Django forbids combining a connection pool with
``CONN_MAX_AGE`` (raises ImproperlyConfigured), so do not set both.

Tunables (env):
    LS_CONN_MAX_AGE        seconds to keep a connection alive (default 600; 0 disables)
    LS_CONN_HEALTH_CHECKS  re-validate a reused connection at request start (default true)
"""

import logging
import os

logger = logging.getLogger('db_perf')


def _int_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        return int(raw)
    except ValueError:
        logger.error('db_perf: %s=%r is not an int; using default %s', name, raw, default)
        return default


def _bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def apply(databases):
    """Enable persistent Postgres connections in-place. Never raises."""
    try:
        conn_max_age = _int_env('LS_CONN_MAX_AGE', 600)
        health_checks = _bool_env('LS_CONN_HEALTH_CHECKS', True)
        applied = []
        for alias, cfg in (databases or {}).items():
            if not isinstance(cfg, dict):
                continue
            if 'postgresql' not in cfg.get('ENGINE', ''):
                continue
            # Pooling and persistent connections are mutually exclusive in Django.
            options = cfg.get('OPTIONS')
            if isinstance(options, dict) and options.get('pool'):
                logger.warning(
                    'db_perf: %s uses a connection pool; leaving CONN_MAX_AGE unset', alias
                )
                continue
            cfg['CONN_MAX_AGE'] = conn_max_age
            cfg['CONN_HEALTH_CHECKS'] = health_checks
            applied.append(alias)
        if applied:
            logger.warning(
                'db_perf: persistent connections enabled '
                '(CONN_MAX_AGE=%ss, health_checks=%s) for: %s',
                conn_max_age, health_checks, ', '.join(applied),
            )
        else:
            logger.warning('db_perf: no postgresql database aliases found; nothing changed')
    except Exception as e:  # pragma: no cover - must never abort boot
        logger.error('db_perf: failed to apply DB tuning: %s', e)
