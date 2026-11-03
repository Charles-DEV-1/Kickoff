from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request

from app.common.auth import require_auth
from app.common.errors import api_error
from app.common.protections import rate_limited
from app.common.validation import RequestValidationError, integer_field, require_only_fields, string_field
from app.modules.templates.routes import _read_rows
from app.modules.templates.service import select_template, validated_variant
from app.modules.ai.service import current_daily_usage, generate_steps
from app.modules.ai.providers import SYSTEM_PROMPT_VERSION


ai_blueprint = Blueprint("ai", __name__)
PROMPT_VERSION = SYSTEM_PROMPT_VERSION
USAGE_LIMIT = 10


def _invalid(field, code="invalid_value"):
    raise RequestValidationError([{"field": field, "code": code}])


def _validate_breakdown(payload):
    require_only_fields(
        payload,
        {"title", "description", "due_at", "detail_level", "language", "task_type"},
    )
    title = string_field(payload, "title", minimum=3, maximum=200).strip()
    if not 3 <= len(title) <= 200:
        _invalid("title")
    description = payload.get("description", "")
    if not isinstance(description, str) or len(description) > 500:
        _invalid("description")
    due_at = payload.get("due_at")
    if due_at is not None:
        if not isinstance(due_at, str):
            _invalid("due_at", "invalid_datetime")
        try:
            parsed = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except ValueError:
            _invalid("due_at", "invalid_datetime")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _invalid("due_at", "timezone_required")

    detail_level = payload.get("detail_level", 3)
    if isinstance(detail_level, bool) or not isinstance(detail_level, int) or not 1 <= detail_level <= 5:
        _invalid("detail_level", "out_of_range")

    language = payload.get("language", (g.auth_profile or {}).get("language") or "en")
    if not isinstance(language, str) or language not in current_app.config.get("SUPPORTED_LANGUAGES", ("en",)):
        _invalid("language", "unsupported_language")
    task_type = payload.get("task_type")
    if task_type is not None and (not isinstance(task_type, str) or task_type not in {
        "essay", "lab_report", "exam_revision", "group_project", "reading",
        "presentation", "assignment", "generic",
    }):
        _invalid("task_type", "invalid_value")
    return {
        "title": title,
        "description": description.strip(),
        "detail_level": detail_level,
        "language": language,
        "task_type": task_type,
    }


@ai_blueprint.post("/v1/ai/breakdown")
@require_auth
@rate_limited("ai_breakdown", limit=5, window_seconds=60)
def breakdown():
    try:
        payload = _validate_breakdown(request.json_body)
    except RequestValidationError as error:
        return api_error(422, "validation_failed", details=error.fields)

    rows = _read_rows()
    if rows is None:
        return api_error(503, "templates_unavailable")
    searchable_text = f"{payload['title']} {payload['description']}"
    template, notice = select_template(
        rows,
        task_type=payload["task_type"],
        language=payload["language"],
        text=searchable_text,
    )
    steps = validated_variant(template, payload["detail_level"]) if template else None
    if steps is None:
        # A malformed or undersized specialized record must never produce
        # placeholder steps. Try only the reviewed generic record next.
        template, generic_notice = select_template(
            rows,
            task_type="generic",
            language=payload["language"],
            text=searchable_text,
        )
        steps = validated_variant(template, payload["detail_level"]) if template else None
        notice = generic_notice
    if template is None or steps is None:
        return api_error(503, "templates_unavailable")

    ai_result = generate_steps(
        payload,
        current_app.extensions.get("kickoff_ai_provider"),
        current_app.extensions.get("kickoff_ai_store"),
    )
    usage_limit = current_app.config.get("AI_DAILY_ALLOWANCE", USAGE_LIMIT)
    if ai_result is not None:
        response_body = {
            "steps": ai_result["steps"],
            "source": "ai",
            "template_id": None,
            "notice": None,
            "prompt_version": PROMPT_VERSION,
            "usage": {"used": ai_result["used"], "limit": usage_limit},
        }
    else:
        daily_used = current_daily_usage(
            current_app.extensions.get("kickoff_ai_store")
        )
        response_body = {
            "steps": steps,
            "source": "template",
            "template_id": template["id"],
            "notice": notice,
            "prompt_version": PROMPT_VERSION,
            "usage": {"used": daily_used, "limit": usage_limit},
        }
    response = jsonify(response_body)
    response.headers["Cache-Control"] = "private, no-store"
    return response, 200
