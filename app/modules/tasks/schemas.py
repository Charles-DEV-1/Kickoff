import base64
import binascii
import json
import re
from datetime import date, datetime, timezone

from app.common.validation import RequestValidationError, integer_field, require_only_fields, string_field, uuid_field


def _invalid(field, code="invalid_value"):
    raise RequestValidationError([{"field": field, "code": code}])


def _timestamp(value, field):
    if not isinstance(value, str):
        _invalid(field, "invalid_datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _invalid(field, "invalid_datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid(field, "timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _task_step(payload, index):
    if not isinstance(payload, dict):
        _invalid(f"steps.{index}", "object_required")
    require_only_fields(payload, {"text", "planned_minutes"})
    text = string_field(payload, "text", minimum=1, maximum=280)
    if not text.strip():
        _invalid(f"steps.{index}.text")
    minutes = integer_field(payload, "planned_minutes", minimum=1, maximum=240)
    return {"text": text, "planned_minutes": minutes}


def validate_task_create(payload):
    require_only_fields(
        payload,
        {"title", "notes", "source", "template_id", "due_at", "steps"},
    )
    title = string_field(payload, "title", minimum=1, maximum=200)
    if not title.strip():
        _invalid("title")
    notes = payload.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 500:
        _invalid("notes")
    source = payload.get("source", "manual")
    if not isinstance(source, str) or source not in {"manual", "template", "ai"}:
        _invalid("source")
    template_id = payload.get("template_id")
    if template_id is not None:
        template_id = uuid_field(template_id, field="template_id")
    if (source == "template") != (template_id is not None):
        _invalid("template_id", "source_template_mismatch")
    due_at = payload.get("due_at")
    if due_at is not None:
        due_at = _timestamp(due_at, "due_at")
    steps = payload.get("steps", [])
    if not isinstance(steps, list):
        _invalid("steps", "array_required")
    if len(steps) > 30:
        _invalid("steps", "too_many")
    return {
        "task": {
            "title": title,
            "notes": notes,
            "source": source,
            "template_id": template_id,
            "due_at": due_at,
        },
        "steps": [_task_step(step, index) for index, step in enumerate(steps)],
    }


def validate_task_patch(payload):
    require_only_fields(
        payload,
        {"version", "title", "notes", "source", "template_id", "due_at"},
    )
    version = integer_field(payload, "version", minimum=1, maximum=2147483647)
    patch = {key: value for key, value in payload.items() if key != "version"}
    if not patch:
        _invalid("body", "empty_patch")
    if "title" in patch:
        title = string_field(patch, "title", minimum=1, maximum=200)
        if not title.strip():
            _invalid("title")
        patch["title"] = title
    if "notes" in patch and (not isinstance(patch["notes"], str) or len(patch["notes"]) > 500):
        _invalid("notes")
    if "source" in patch and (
        not isinstance(patch["source"], str)
        or patch["source"] not in {"manual", "template", "ai"}
    ):
        _invalid("source")
    if "template_id" in patch and patch["template_id"] is not None:
        patch["template_id"] = uuid_field(patch["template_id"], field="template_id")
    if "due_at" in patch and patch["due_at"] is not None:
        patch["due_at"] = _timestamp(patch["due_at"], "due_at")
    return version, patch


def validate_version_body(payload):
    require_only_fields(payload, {"version"})
    return integer_field(payload, "version", minimum=1, maximum=2147483647)


def validate_step_action(payload, action):
    allowed = {"version"}
    if action == "complete":
        allowed.add("actual_minutes")
    require_only_fields(payload, allowed)
    version = integer_field(payload, "version", minimum=1, maximum=2147483647)
    actual_minutes = None
    if "actual_minutes" in payload:
        actual_minutes = integer_field(payload, "actual_minutes", minimum=0, maximum=600)
    return version, actual_minutes


def validate_step_patch(payload):
    require_only_fields(payload, {"version", "text", "planned_minutes"})
    version = integer_field(payload, "version", minimum=1, maximum=2147483647)
    patch = {key: value for key, value in payload.items() if key != "version"}
    if not patch:
        _invalid("body", "empty_patch")
    if "text" in patch:
        text = string_field(patch, "text", minimum=1, maximum=280)
        if not text.strip():
            _invalid("text")
        patch["text"] = text
    if "planned_minutes" in patch:
        patch["planned_minutes"] = integer_field(
            patch, "planned_minutes", minimum=1, maximum=240
        )
    return version, patch


def validate_step_create(payload):
    require_only_fields(payload, {"text", "planned_minutes"})
    return _task_step(payload, 0)


def validate_reorder(payload):
    require_only_fields(payload, {"version", "step_ids"})
    version = integer_field(payload, "version", minimum=1, maximum=2147483647)
    step_ids = payload.get("step_ids")
    if not isinstance(step_ids, list) or len(step_ids) > 30:
        _invalid("step_ids", "invalid_array")
    normalized = [uuid_field(value, field="step_ids") for value in step_ids]
    if len(normalized) != len(set(normalized)):
        _invalid("step_ids", "duplicate_id")
    return version, normalized


def _parse_due_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _invalid("due_date", "invalid_date")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        _invalid("due_date", "invalid_date")


def _parse_cursor(value):
    if not value:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded)
        timestamp = _timestamp(data["updated_at"], "cursor")
        task_id = uuid_field(data["id"], field="cursor")
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        _invalid("cursor", "invalid_cursor")
    return timestamp, task_id


def encode_cursor(task):
    raw = json.dumps(
        {"updated_at": task["updated_at"], "id": task["id"]},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def validate_task_query(args):
    if set(args.keys()) - {"status", "due_date", "updated_since", "snapshot_at", "cursor", "limit"}:
        _invalid("query", "unknown_field")
    status = args.get("status", "active")
    if status not in {"active", "completed", "archived", "all"}:
        _invalid("status")
    due_date = args.get("due_date")
    if due_date is not None:
        due_date = _parse_due_date(due_date)
    updated_since = args.get("updated_since")
    if updated_since is not None:
        updated_since = _timestamp(updated_since, "updated_since")
    snapshot_at = args.get("snapshot_at")
    if snapshot_at is not None:
        if updated_since is None:
            _invalid("snapshot_at", "requires_updated_since")
        snapshot_at = _timestamp(snapshot_at, "snapshot_at")
    limit_text = args.get("limit", "20")
    if not isinstance(limit_text, str) or not limit_text.isdigit():
        _invalid("limit")
    limit = int(limit_text)
    if not 1 <= limit <= 50:
        _invalid("limit")
    cursor_updated_at, cursor_id = _parse_cursor(args.get("cursor"))
    return {
        "status": status,
        "due_date": due_date.isoformat() if due_date else None,
        "updated_since": updated_since,
        "snapshot_at": snapshot_at,
        "cursor_updated_at": cursor_updated_at,
        "cursor_id": cursor_id,
        "limit": limit,
    }
