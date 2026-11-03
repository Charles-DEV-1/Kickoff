import hashlib
import json
import logging
import re
from functools import wraps
from uuid import UUID

from flask import current_app, g, jsonify, make_response, request
from werkzeug.exceptions import BadRequest

from app.common.errors import api_error
from app.common.supabase_data import SupabaseDataAPIError
from app.common.validation import MissingAuthContext, authenticated_user_id


logger = logging.getLogger(__name__)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class ProtectionStoreUnavailable(Exception):
    pass


def _rpc(function_name, token, parameters):
    data_api = current_app.extensions["kickoff_supabase_data"]
    try:
        result = data_api.rpc(function_name, token, parameters)
    except SupabaseDataAPIError:
        raise ProtectionStoreUnavailable from None
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict):
        raise ProtectionStoreUnavailable
    return result


class PersistentRateLimiter:
    """Atomic per-user rate limits persisted by a narrowly scoped Postgres RPC."""

    @staticmethod
    def consume(bucket, limit, window_seconds):
        user_id = authenticated_user_id()
        if not isinstance(bucket, str) or not bucket or len(bucket) > 80:
            raise ValueError("Invalid rate-limit bucket")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
            raise ValueError("Invalid rate limit")
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= 86400
        ):
            raise ValueError("Invalid rate-limit window")

        result = _rpc(
            "consume_api_rate_limit",
            g.auth_access_token,
            {
                "p_bucket": bucket,
                "p_limit": limit,
                "p_window_seconds": window_seconds,
            },
        )
        if result.get("user_id") != user_id or not isinstance(result.get("allowed"), bool):
            raise ProtectionStoreUnavailable
        retry_after = result.get("retry_after_seconds", 1)
        if isinstance(retry_after, bool) or not isinstance(retry_after, int):
            raise ProtectionStoreUnavailable
        return {
            "allowed": result["allowed"],
            "remaining": max(0, int(result.get("remaining", 0))),
            "retry_after_seconds": max(1, retry_after),
        }


class IdempotencyService:
    """Persist a request hash and minimal JSON result under the verified user ID."""

    @staticmethod
    def begin(key, request_hash):
        return _rpc(
            "begin_api_idempotency",
            g.auth_access_token,
            {"p_key": key, "p_request_hash": request_hash},
        )

    @staticmethod
    def complete(key, request_hash, lease_token, status, response_body):
        return _rpc(
            "complete_api_idempotency",
            g.auth_access_token,
            {
                "p_key": key,
                "p_request_hash": request_hash,
                "p_lease_token": lease_token,
                "p_response_status": status,
                "p_response_body": response_body,
            },
        )

    @staticmethod
    def release(key, request_hash, lease_token):
        return _rpc(
            "release_api_idempotency",
            g.auth_access_token,
            {
                "p_key": key,
                "p_request_hash": request_hash,
                "p_lease_token": lease_token,
            },
        )


def _request_hash():
    raw_body = request.get_data(cache=True)
    if raw_body:
        payload = request.get_json(silent=False, cache=True)
    else:
        payload = None
    canonical = {
        "method": request.method,
        "path": request.path,
        "query": sorted(request.args.items(multi=True)),
        "if_match": request.headers.get("If-Match"),
        "if_none_match": request.headers.get("If-None-Match"),
        "body": payload,
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rate_limited(bucket, *, limit, window_seconds):
    """Decorate an authenticated route with a persisted per-user limit."""
    def decorate(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                result = PersistentRateLimiter.consume(bucket, limit, window_seconds)
            except ProtectionStoreUnavailable:
                return api_error(503, "rate_limit_unavailable")
            if not result["allowed"]:
                return api_error(
                    429,
                    "rate_limited",
                    headers={"Retry-After": str(result["retry_after_seconds"])},
                )
            return handler(*args, **kwargs)
        return wrapped
    return decorate


def idempotent_write(handler):
    """Require a key and replay completed JSON responses for identical writes.

    Apply inside require_auth so the verified user context and user JWT are
    available. Only the request hash, status, response JSON, and short lease are
    persisted; request bodies and response contents are never logged.
    """
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            authenticated_user_id()
        except MissingAuthContext:
            return api_error(401, "token_invalid")

        key = request.headers.get("Idempotency-Key", "")
        if not key:
            return api_error(400, "idempotency_key_required")
        if not _IDEMPOTENCY_KEY.fullmatch(key):
            return api_error(
                422,
                "validation_failed",
                details=[{"field": "Idempotency-Key", "code": "invalid_value"}],
            )

        try:
            request_hash = _request_hash()
        except (BadRequest, TypeError, ValueError):
            return api_error(400, "invalid_json")

        try:
            claim = IdempotencyService.begin(key, request_hash)
        except ProtectionStoreUnavailable:
            return api_error(503, "idempotency_unavailable")

        state = claim.get("state")
        if state == "key_reused":
            return api_error(409, "idempotency_key_reused")
        if state == "in_progress":
            return api_error(409, "request_in_progress")
        if state == "replay":
            response_body = claim.get("response_body")
            if isinstance(response_body, dict) and isinstance(response_body.get("error"), dict):
                response_body["error"]["request_id"] = getattr(g, "request_id", None)
            response_status = claim.get("response_status", 200)
            response = make_response(
                "" if response_status == 204 else jsonify(response_body),
                response_status,
            )
            response.headers["Idempotent-Replay"] = "true"
            response.headers["Cache-Control"] = "private, no-store"
            return response
        if state != "new":
            return api_error(503, "idempotency_unavailable")
        try:
            lease_token = str(UUID(str(claim.get("lease_token"))))
        except (ValueError, TypeError, AttributeError):
            return api_error(503, "idempotency_unavailable")

        g.idempotency_context = {
            "key": key,
            "request_hash": request_hash,
            "lease_token": lease_token,
        }
        g.idempotency_atomic_committed = False

        try:
            response = make_response(handler(*args, **kwargs))
        except Exception:
            try:
                IdempotencyService.release(key, request_hash, lease_token)
            except ProtectionStoreUnavailable:
                logger.warning(
                    "Idempotency reservation release failed",
                    extra={"request_id": getattr(g, "request_id", "unknown")},
                )
            raise

        if response.status_code == 204:
            response_body = None
        elif not response.is_json:
            try:
                IdempotencyService.release(key, request_hash, lease_token)
            except ProtectionStoreUnavailable:
                pass
            return api_error(500, "internal_error")
        else:
            response_body = response.get_json(silent=True)
        if getattr(g, "idempotency_atomic_committed", False):
            return response
        if response.status_code == 429 or response.status_code >= 500:
            try:
                IdempotencyService.release(key, request_hash, lease_token)
            except ProtectionStoreUnavailable:
                logger.warning(
                    "Idempotency reservation release failed",
                    extra={"request_id": getattr(g, "request_id", "unknown")},
                )
            return response
        try:
            IdempotencyService.complete(
                key,
                request_hash,
                lease_token,
                response.status_code,
                response_body,
            )
        except ProtectionStoreUnavailable:
            logger.warning(
                "Idempotency response storage failed",
                extra={"request_id": getattr(g, "request_id", "unknown")},
            )
            return api_error(503, "idempotency_unavailable")
        return response
    return wrapped
