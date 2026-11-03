import re
import time
from threading import Lock

from app.common.supabase_data import SupabaseDataAPIError


class TemplateCatalogUnavailable(Exception):
    pass


class TemplateCatalog:
    """Short-lived per-process read cache for the reviewed public catalog."""

    def __init__(self, data_api, *, ttl_seconds=30, clock=time.monotonic):
        self.data_api = data_api
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = Lock()
        self._rows = None
        self._expires_at = 0

    def rows(self, *, refresh=False):
        now = self.clock()
        with self._lock:
            if not refresh and self._rows is not None and now < self._expires_at:
                return self._rows
            try:
                rows = self.data_api.select_public_templates()
            except SupabaseDataAPIError:
                raise TemplateCatalogUnavailable from None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise TemplateCatalogUnavailable
            latest = {}
            for row in rows:
                if row.get("is_active") is not True:
                    continue
                key = (row.get("template_key"), row.get("language"))
                if not all(isinstance(value, str) and value for value in key):
                    continue
                try:
                    version = int(row.get("version"))
                except (TypeError, ValueError):
                    continue
                current = latest.get(key)
                if current is None or (version, str(row.get("id", ""))) > (
                    int(current.get("version", 0)), str(current.get("id", ""))
                ):
                    latest[key] = row
            self._rows = tuple(sorted(
                latest.values(),
                key=lambda row: (
                    row.get("task_type", ""), row.get("language", ""),
                    row.get("template_key", ""), str(row.get("id", "")),
                ),
            ))
            self._expires_at = now + self.ttl_seconds
            return self._rows


def keyword_score(template, text):
    haystack = " ".join(text.split()).casefold()
    keywords = template.get("keywords")
    if not isinstance(keywords, list):
        return 0
    matched = {
        keyword.strip().casefold()
        for keyword in keywords
        if isinstance(keyword, str) and keyword.strip()
        and re.search(r"(?<!\w)" + re.escape(keyword.strip().casefold()) + r"(?!\w)", haystack)
    }
    return len(matched)


def _language_for(rows, language):
    if any(row.get("language") == language for row in rows):
        return language, False
    if any(row.get("language") == "en" for row in rows):
        return "en", language != "en"
    return language, False


def _rank(rows, text):
    return sorted(
        rows,
        key=lambda row: (
            -keyword_score(row, text),
            row.get("template_key", ""),
            str(row.get("id", "")),
        ),
    )


def select_template(rows, *, task_type, language, text):
    """Select a matching localized template or deterministic generic fallback."""
    specialized = [row for row in rows if row.get("task_type") != "generic"]
    if task_type:
        specialized = [row for row in specialized if row.get("task_type") == task_type]
    preferred_language, language_fallback = _language_for(specialized, language)
    candidates = [row for row in specialized if row.get("language") == preferred_language]
    ranked = _rank(candidates, text)
    if ranked and keyword_score(ranked[0], text) >= 1:
        return ranked[0], ("language_fallback" if language_fallback else None)

    generic = [row for row in rows if row.get("task_type") == "generic"]
    generic_language, generic_language_fallback = _language_for(generic, language)
    fallback_rows = [row for row in generic if row.get("language") == generic_language]
    ranked_generic = _rank(fallback_rows, text)
    if not ranked_generic:
        return None, "templates_unavailable"
    notice = "language_fallback" if generic_language_fallback else "template_fallback"
    return ranked_generic[0], notice


TARGET_STEP_COUNTS = {1: 3, 2: 5, 3: 8, 4: 12, 5: 18}
VARIANT_BY_LEVEL = {1: "short", 2: "short", 3: "standard", 4: "detailed", 5: "detailed"}


def validated_variant(template, detail_level):
    """Return an approved variant truncated to that level's minimum count."""
    variants = template.get("variants")
    if not isinstance(variants, dict):
        return None
    variant = variants.get(VARIANT_BY_LEVEL[detail_level])
    target_count = TARGET_STEP_COUNTS[detail_level]
    if not isinstance(variant, list) or len(variant) < target_count or len(variant) > 30:
        return None

    result = []
    for index, item in enumerate(variant[:target_count]):
        if not isinstance(item, dict):
            return None
        text = item.get("text")
        minutes = item.get("planned_minutes")
        if not isinstance(text, str) or not text.strip() or len(text) > 280:
            return None
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 120:
            return None
        result.append({
            "text": text,
            "planned_minutes": min(minutes, 10) if index == 0 else minutes,
        })
    return result
