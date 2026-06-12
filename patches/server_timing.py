"""Server-Timing instrumentation for Label Studio.

Adds a ``Server-Timing`` response header (visible in browser devtools → Network →
a request's Timing tab) so the ORIGIN processing time can be read directly and
separated from network/proxy latency. This is the clean way to answer "is a slow
request slow at the app/DB, or in the Cloudflare→HAProxy→origin path?": compare
the browser-observed duration against ``total`` below — the difference is the
network+proxy tax.

Header emitted, e.g.:
    Server-Timing: app;dur=92.4, db;dur=12.6;desc="34 queries", total;dur=105.0
Also mirrored as ``X-Origin-Time-Ms`` for easy curl/grep.

  app   = wall time spent in Python (total minus DB wait)
  db    = cumulative time blocked on the default DB connection, with query count
  total = wall time this middleware saw for the whole request

DB time is measured via ``connection.execute_wrapper`` (works with DEBUG=False,
unlike ``connection.queries``). The middleware is inserted at the FRONT of
MIDDLEWARE (see 44-server-timing.sh) so it wraps the maximum of the request
(session/auth/RBAC middleware queries included). Fail-open: any error here must
never break a response, so the header is best-effort and the timing wrapper
degrades to a no-op.
"""

import time

from django.db import connections


class ServerTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        db = {'t': 0.0, 'n': 0}

        # NOTE: must be chain-safe. When >1 execute_wrapper is active, Django calls the
        # inner wrapper as wrapper(execute, sql, params, many) WITHOUT context, and the
        # wrapper must forward context onward. So: default context=None (never crash on a
        # 4-arg call) and forward (sql, params, many, context) to the next callable (the
        # real _execute ignores the extras via *ignored_wrapper_args).
        def _wrapper(execute, sql, params, many, context=None):
            start = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                db['t'] += time.perf_counter() - start
                db['n'] += 1

        start = time.perf_counter()
        try:
            with connections['default'].execute_wrapper(_wrapper):
                response = self.get_response(request)
        except Exception:
            # execute_wrapper unavailable or connection issue: still serve the request.
            response = self.get_response(request)

        try:
            total_ms = (time.perf_counter() - start) * 1000.0
            db_ms = db['t'] * 1000.0
            app_ms = max(0.0, total_ms - db_ms)
            response['Server-Timing'] = (
                f'app;dur={app_ms:.1f}, '
                f'db;dur={db_ms:.1f};desc="{db["n"]} queries", '
                f'total;dur={total_ms:.1f}'
            )
            response['X-Origin-Time-Ms'] = f'{total_ms:.1f}'
        except Exception:
            pass
        return response
