"""Privacy-preserving AI orchestration with a mandatory template fallback."""

import hashlib
import hmac
import json
import logging
import re
import queue
import threading
import time
import unicodedata
from datetime import datetime, timezone
from uuid import UUID

from flask import current_app, g

from app.common.supabase_data import SupabaseDataAPIError
from app.modules.ai.providers import ProviderFailure, SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
from app.modules.templates.service import TARGET_STEP_COUNTS


EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]{1,128}@[\w.-]{1,190}\.[A-Z]{2,63}(?![\w.-])")
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)")
HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_PROVIDER_SLOTS = threading.BoundedSemaphore(16)
logger = logging.getLogger(__name__)


def _fallback(reason):
    # `reason` is a fixed internal code. Never attach user text, provider bodies,
    # credentials, or exception messages to diagnostics.
    request_id = getattr(g, "request_id", "unknown")
    logger.warning(
        "AI breakdown used template fallback reason=%s request_id=%s",
        reason,
        request_id,
        extra={
            "request_id": request_id,
            "ai_fallback_reason": reason,
        },
    )
    return None


def redact_sensitive_text(value):
    value = EMAIL_RE.sub("[redacted email]", value)
    return PHONE_RE.sub("[redacted phone]", value)


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _cache_hash(payload, secret):
    cache_input = {
        "description": _normalized(redact_sensitive_text(payload["description"])),
        "detail_level": payload["detail_level"],
        "language": payload["language"],
        "prompt_version": SYSTEM_PROMPT_VERSION,
        "task_type": payload.get("task_type") or "generic",
        "title": _normalized(redact_sensitive_text(payload["title"])),
    }
    raw = json.dumps(cache_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _provider_input(payload):
    return {
        "title": redact_sensitive_text(payload["title"]),
        "description": redact_sensitive_text(payload["description"]),
        "language": payload["language"],
        "task_type": payload.get("task_type") or "generic",
        "detail_level": payload["detail_level"],
        "step_count": TARGET_STEP_COUNTS[payload["detail_level"]],
    }


def validate_provider_result(value, detail_level):
    """Validate and minimize untrusted provider output to the public schema."""
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        return None
    expected = TARGET_STEP_COUNTS[detail_level]
    if len(value["steps"]) != expected:
        return None
    clean_steps = []
    for index, item in enumerate(value["steps"]):
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        minutes = item.get("planned_minutes")
        if not isinstance(text, str):
            return None
        text = " ".join(text.split()).strip()
        if not 1 <= len(text) <= 280 or HTML_TAG_RE.search(text):
            return None
        if any(unicodedata.category(char) == "Cc" for char in text):
            return None
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 120:
            return None
        if index == 0 and minutes > 10:
            return None
        text = redact_sensitive_text(text)
        if not text.strip():
            return None
        # Explicitly copy only the two allowed fields. Provider metadata is discarded.
        clean_steps.append({"text": text, "planned_minutes": minutes})
    return clean_steps


def _invoke_bounded(provider, *, input_data, timeout_seconds):
    """Bound a faulty adapter too; at most 16 timed-out calls can linger."""
    if not _PROVIDER_SLOTS.acquire(blocking=False):
        return False, None, False
    result_queue = queue.Queue(maxsize=1)

    def run():
        try:
            result_queue.put((True, provider.generate(
                input_data=input_data,
                system_prompt=SYSTEM_PROMPT,
                timeout_seconds=timeout_seconds,
            )))
        except Exception as error:
            result_queue.put((False, error))
        finally:
            _PROVIDER_SLOTS.release()

    threading.Thread(target=run, daemon=True, name="kickoff-ai-provider").start()
    try:
        ok, value = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return False, None, True
    if ok:
        return True, value, False
    return False, value, False


class SupabaseAIStore:
    """RLS-scoped user operations plus a server-only global budget RPC."""

    def __init__(self, data_api, secret_key):
        self.data_api = data_api
        self.secret_key = secret_key

    def get_cache(self, cache_hash):
        result = self.data_api.rpc(
            "get_ai_breakdown_cache", g.auth_access_token,
            {"p_cache_hash": cache_hash, "p_prompt_version": SYSTEM_PROMPT_VERSION},
        )
        if not isinstance(result, dict):
            raise SupabaseDataAPIError
        return result

    def read_user_usage(self):
        result = self.data_api.rpc("read_ai_daily_usage", g.auth_access_token, {})
        if not isinstance(result, dict):
            raise SupabaseDataAPIError
        return result

    def reserve_user_usage(self, limit):
        result = self.data_api.rpc(
            "reserve_ai_daily_usage", g.auth_access_token, {"p_daily_limit": limit}
        )
        if not isinstance(result, dict):
            raise SupabaseDataAPIError
        return result

    def commit_user_usage(self, reservation_id):
        result = self.data_api.rpc(
            "commit_ai_daily_usage", g.auth_access_token,
            {"p_reservation_id": reservation_id},
        )
        if not isinstance(result, dict):
            raise SupabaseDataAPIError
        return result

    def save_cache(self, cache_hash, result, ttl_seconds):
        saved = self.data_api.rpc(
            "save_ai_breakdown_cache", g.auth_access_token,
            {
                "p_cache_hash": cache_hash,
                "p_prompt_version": SYSTEM_PROMPT_VERSION,
                "p_result": result,
                "p_ttl_seconds": ttl_seconds,
            },
        )
        if not isinstance(saved, dict):
            raise SupabaseDataAPIError
        return bool(saved.get("saved"))

    def reserve_provider_budget(self, provider_id, limit, usage_date):
        if not self.secret_key:
            raise SupabaseDataAPIError
        result = self.data_api.rpc_server(
            "reserve_ai_provider_budget", self.secret_key,
            {
                "p_provider": provider_id,
                "p_daily_limit": limit,
                "p_usage_date": usage_date,
            },
        )
        if not isinstance(result, dict):
            raise SupabaseDataAPIError
        return result


def generate_steps(payload, provider, store):
    """Return an AI result or None; every error path is safe for template fallback."""
    if provider is None:
        return _fallback("provider_not_configured")
    if store is None:
        return _fallback("usage_store_unavailable")
    config = current_app.config
    hmac_key = config.get("AI_CACHE_HMAC_KEY", "")
    if not isinstance(hmac_key, str) or len(hmac_key) < 32:
        return _fallback("cache_hmac_not_configured")
    provider_id = getattr(provider, "provider_id", None)
    if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", provider_id):
        return _fallback("provider_adapter_misconfigured")
    daily_limit = config.get("AI_DAILY_ALLOWANCE", 10)
    provider_budget = config.get("AI_PROVIDER_DAILY_BUDGET", 1000)
    cache_ttl = config.get("AI_CACHE_TTL_SECONDS", 86400)
    provider_timeout = config.get("AI_PROVIDER_TIMEOUT_SECONDS", 5)
    try:
        overall_budget = min(8.0, float(config.get("AI_TOTAL_BUDGET_SECONDS", 8)))
    except (TypeError, ValueError):
        return _fallback("ai_timeout_misconfigured")
    if (
        not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or daily_limit < 1
        or not isinstance(provider_budget, int) or isinstance(provider_budget, bool) or provider_budget < 1
        or not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or not 60 <= cache_ttl <= 604800
        or not isinstance(provider_timeout, (int, float)) or isinstance(provider_timeout, bool) or provider_timeout <= 0
        or overall_budget <= 0
    ):
        return _fallback("ai_limits_misconfigured")

    cache_hash = _cache_hash(payload, hmac_key)
    try:
        cached = store.get_cache(cache_hash)
    except Exception:
        return _fallback("cache_store_unavailable")
    if cached.get("hit") is True:
        steps = validate_provider_result(cached.get("result"), payload["detail_level"])
        if steps is not None:
            try:
                usage = store.read_user_usage()
            except Exception:
                return _fallback("usage_store_unavailable")
            used = usage.get("used") if isinstance(usage, dict) else None
            if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                return {"steps": steps, "used": used, "cached": True}
    try:
        usage_reservation = store.reserve_user_usage(daily_limit)
    except Exception:
        return _fallback("usage_reservation_unavailable")
    if usage_reservation.get("allowed") is not True:
        return _fallback("daily_allowance_exhausted")
    reservation_id = usage_reservation.get("reservation_id")
    if not isinstance(reservation_id, str):
        return _fallback("usage_reservation_invalid")
    try:
        reservation_id = str(UUID(reservation_id))
    except (ValueError, TypeError, AttributeError):
        return _fallback("usage_reservation_invalid")

    deadline = time.monotonic() + overall_budget
    input_data = _provider_input(payload)
    steps = None
    failure_reason = "provider_request_failed"
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        timeout = min(float(provider_timeout), remaining)
        if timeout <= 0.05:
            break
        try:
            budget = store.reserve_provider_budget(
                provider_id, provider_budget, datetime.now(timezone.utc).date().isoformat()
            )
        except Exception:
            return _fallback("provider_budget_unavailable")
        if budget.get("allowed") is not True:
            return _fallback("provider_budget_exhausted")
        ok, raw, timed_out = _invoke_bounded(
            provider, input_data=input_data, timeout_seconds=timeout
        )
        if ok:
            steps = validate_provider_result(raw, payload["detail_level"])
            if steps is not None:
                break
            retryable = True
            failure_reason = "provider_output_rejected"
        elif isinstance(raw, ProviderFailure):
            retryable = raw.retryable
            failure_reason = raw.code if re.fullmatch(r"[a-z0-9_]{1,50}", raw.code) else "provider_request_failed"
        else:
            retryable = timed_out
            failure_reason = "provider_timeout" if timed_out else "provider_adapter_error"
        if attempt == 0 and retryable and deadline - time.monotonic() > 0.25:
            continue
        break

    if steps is None or time.monotonic() > deadline:
        return _fallback(failure_reason if steps is None else "provider_deadline_exceeded")

    try:
        committed = store.commit_user_usage(reservation_id)
    except Exception:
        return _fallback("usage_commit_unavailable")
    used = committed.get("used") if committed.get("committed") is True else None
    if isinstance(used, bool) or not isinstance(used, int) or used < 1:
        return _fallback("usage_commit_rejected")

    result = {"steps": steps}
    try:
        store.save_cache(cache_hash, result, cache_ttl)
    except Exception:
        # The generated result remains usable; the daily count was reserved atomically.
        logger.warning(
            "AI breakdown cache write failed",
            extra={"request_id": getattr(g, "request_id", "unknown")},
        )
    return {"steps": steps, "used": used, "cached": False}


def current_daily_usage(store):
    """Read the existing allowance count without making fallback depend on it."""
    if store is None:
        return 0
    try:
        result = store.read_user_usage()
        used = result.get("used") if isinstance(result, dict) else None
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            return 0
        return used
    except Exception:
        return 0
