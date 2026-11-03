"""Small per-process IP limiter for public, cacheable template reads.

For horizontal/serverless deployment, put an IP limiter at the trusted edge as
well. This in-process guard is a defense in depth and local-development guard;
it is intentionally not presented as a shared persistent counter.
"""

import hashlib
import threading
import time

from flask import current_app, request

from app.common.errors import api_error


class PublicIPRateLimiter:
    def __init__(self, *, limit=60, window_seconds=60, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._windows = {}

    def check(self, bucket):
        remote_ip = request.remote_addr or "unknown"
        ip_key = hashlib.sha256(remote_ip.encode("utf-8", errors="replace")).hexdigest()
        now = self.clock()
        key = (bucket, ip_key)
        with self._lock:
            start, count = self._windows.get(key, (now, 0))
            if now - start >= self.window_seconds:
                start, count = now, 0
            if count >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - start)))
                return False, retry_after
            self._windows[key] = (start, count + 1)
            if len(self._windows) > 10000:
                cutoff = now - self.window_seconds
                self._windows = {
                    stored_key: value for stored_key, value in self._windows.items()
                    if value[0] >= cutoff
                }
            return True, max(1, int(self.window_seconds - (now - start)))


def public_rate_limited(bucket):
    def decorate(handler):
        from functools import wraps

        @wraps(handler)
        def wrapped(*args, **kwargs):
            limiter = current_app.extensions["kickoff_public_ip_limiter"]
            allowed, retry_after = limiter.check(bucket)
            if not allowed:
                return api_error(429, "rate_limited", headers={"Retry-After": str(retry_after)})
            return handler(*args, **kwargs)
        return wrapped
    return decorate
