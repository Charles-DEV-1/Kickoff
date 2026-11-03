import json
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

import jwt


class JwksUnavailable(Exception):
    pass


class UnknownSigningKey(Exception):
    pass


class JwksKeyProvider:
    def __init__(self, url, allowed_algorithms, ttl_seconds=600, timeout_seconds=3):
        self.url = url
        self.allowed_algorithms = frozenset(allowed_algorithms)
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._keys = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def _fetch(self):
        if not self.url:
            raise JwksUnavailable
        request = Request(
            self.url,
            headers={"Accept": "application/json", "User-Agent": "kickoff-api"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(65537)
        except (URLError, TimeoutError, OSError) as error:
            raise JwksUnavailable from error

        if len(raw) > 65536:
            raise JwksUnavailable

        try:
            document = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise JwksUnavailable from error

        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise JwksUnavailable

        parsed = {}
        for item in document["keys"]:
            if not isinstance(item, dict):
                continue
            kid = item.get("kid")
            algorithm = item.get("alg")
            if (
                not isinstance(kid, str)
                or not kid
                or algorithm not in self.allowed_algorithms
                or item.get("use", "sig") != "sig"
            ):
                continue
            try:
                parsed[kid] = (jwt.PyJWK.from_dict(item, algorithm=algorithm).key, algorithm)
            except (jwt.PyJWTError, ValueError, TypeError):
                continue

        if not parsed:
            raise JwksUnavailable

        self._keys = parsed
        self._fetched_at = time.monotonic()

    def get_public_key(self, kid, algorithm):
        if algorithm not in self.allowed_algorithms or not kid:
            raise UnknownSigningKey

        with self._lock:
            now = time.monotonic()
            known = kid in self._keys
            stale = now - self._fetched_at >= self.ttl_seconds
            cooldown_complete = now - self._last_attempt >= 5
            should_refresh = not self._keys or (stale and cooldown_complete)
            unknown_kid_refresh = not known and cooldown_complete

            if should_refresh or unknown_kid_refresh:
                had_cached_key = known
                self._last_attempt = now
                try:
                    self._fetch()
                except JwksUnavailable:
                    if had_cached_key and kid in self._keys:
                        return self._keys[kid]
                    raise

            entry = self._keys.get(kid)
            if entry is None or entry[1] != algorithm:
                raise UnknownSigningKey
            return entry[0]
