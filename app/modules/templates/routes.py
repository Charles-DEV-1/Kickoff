import base64
import hashlib
import json
import re
from flask import Blueprint, current_app, jsonify, make_response, request

from app.common.errors import api_error
from app.common.public_rate_limit import public_rate_limited
from app.common.supabase_data import SupabaseDataAPIError
from app.common.validation import RequestValidationError, uuid_field
from app.modules.templates.service import TemplateCatalog, TemplateCatalogUnavailable


templates_blueprint = Blueprint("templates", __name__)
TASK_TYPES = {
    "essay", "lab_report", "exam_revision", "group_project", "reading",
    "presentation", "assignment", "generic",
}


def _catalog():
    data_api = current_app.extensions["kickoff_supabase_data"]
    catalog = current_app.extensions.get("kickoff_template_catalog")
    if catalog is None or catalog.data_api is not data_api:
        catalog = TemplateCatalog(data_api, ttl_seconds=30)
        current_app.extensions["kickoff_template_catalog"] = catalog
    return catalog


def _unavailable():
    return api_error(503, "templates_unavailable")


def _read_rows():
    try:
        return _catalog().rows()
    except TemplateCatalogUnavailable:
        return None


def _language(value):
    if not isinstance(value, str) or value not in current_app.config.get("SUPPORTED_LANGUAGES", ("en",)):
        raise RequestValidationError([{"field": "language", "code": "unsupported_language"}])
    return value


def _etagged(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    etag = hashlib.sha256(raw).hexdigest()
    if request.if_none_match.star_tag or request.if_none_match.contains_weak(etag):
        response = make_response("", 304)
    else:
        response = make_response(jsonify(payload), 200)
    response.set_etag(etag)
    response.headers["Cache-Control"] = "public, max-age=120, stale-while-revalidate=30"
    return response


def _summary(row):
    return {
        "id": row["id"],
        "slug": row["template_key"],
        "task_type": row["task_type"],
        "language": row["language"],
        "title": row["title"],
        "version": row["version"],
    }


def manifest_items(language=None):
    """Shared small manifest representation for authenticated bootstrap reads."""
    rows = _read_rows()
    if rows is None:
        return None
    return [
        {"id": row["id"], "slug": row["template_key"], "language": row["language"],
         "task_type": row["task_type"], "version": row["version"]}
        for row in rows if language is None or row["language"] == language
    ]


@templates_blueprint.get("/v1/templates/manifest")
@public_rate_limited("templates_public")
def template_manifest():
    try:
        language = request.args.get("language")
        if language is not None:
            language = _language(language)
        if set(request.args.keys()) - {"language"}:
            raise RequestValidationError([{"field": "query", "code": "unknown_field"}])
    except RequestValidationError as error:
        return api_error(422, "validation_failed", details=error.fields)
    items = manifest_items(language)
    if items is None:
        return _unavailable()
    return _etagged({"items": items})


def _decode_cursor(cursor):
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        offset = data["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise RequestValidationError([{"field": "cursor", "code": "invalid_cursor"}]) from None


def _encode_cursor(offset):
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@templates_blueprint.get("/v1/templates")
@public_rate_limited("templates_public")
def list_templates():
    try:
        allowed = {"task_type", "language", "limit", "cursor"}
        if set(request.args.keys()) - allowed:
            raise RequestValidationError([{"field": "query", "code": "unknown_field"}])
        task_type = request.args.get("task_type")
        if task_type is not None and task_type not in TASK_TYPES:
            raise RequestValidationError([{"field": "task_type", "code": "invalid_value"}])
        language = request.args.get("language")
        if language is not None:
            language = _language(language)
        limit_text = request.args.get("limit", "50")
        if not re.fullmatch(r"[0-9]{1,3}", limit_text):
            raise RequestValidationError([{"field": "limit", "code": "invalid_value"}])
        limit = int(limit_text)
        if not 1 <= limit <= 100:
            raise RequestValidationError([{"field": "limit", "code": "out_of_range"}])
        offset = _decode_cursor(request.args.get("cursor"))
    except RequestValidationError as error:
        return api_error(422, "validation_failed", details=error.fields)

    rows = _read_rows()
    if rows is None:
        return _unavailable()
    filtered = [
        row for row in rows
        if (task_type is None or row["task_type"] == task_type)
        and (language is None or row["language"] == language)
    ]
    page = filtered[offset:offset + limit]
    next_cursor = _encode_cursor(offset + limit) if offset + limit < len(filtered) else None
    return _etagged({
        "items": [_summary(row) for row in page],
        "next_cursor": next_cursor,
    })


@templates_blueprint.get("/v1/templates/<template_id>")
@public_rate_limited("templates_public")
def get_template(template_id):
    try:
        normalized_id = uuid_field(template_id, field="template_id")
    except RequestValidationError as error:
        return api_error(422, "validation_failed", details=error.fields)
    rows = _read_rows()
    if rows is None:
        return _unavailable()
    template = next((row for row in rows if row.get("id") == normalized_id), None)
    if template is None:
        return api_error(404, "not_found")
    payload = {
        "id": template["id"],
        "slug": template["template_key"],
        "task_type": template["task_type"],
        "language": template["language"],
        "title": template["title"],
        "description": template.get("description", ""),
        "keywords": template.get("keywords", []),
        "variants": template.get("variants", {}),
        "version": template["version"],
        "updated_at": template.get("updated_at"),
    }
    return _etagged(payload)
