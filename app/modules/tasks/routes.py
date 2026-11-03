from flask import Blueprint, current_app, g, jsonify, request

from app.common.auth import _error, require_auth
from app.common.errors import api_error
from app.common.private_cache import private_json
from app.common.protections import idempotent_write, rate_limited
from app.common.supabase_data import SupabaseDataAPIError
from app.common.validation import RequestValidationError, uuid_field
from app.modules.tasks.schemas import (
    encode_cursor,
    validate_reorder,
    validate_step_create,
    validate_step_action,
    validate_step_patch,
    validate_task_create,
    validate_task_patch,
    validate_task_query,
    validate_version_body,
)
from app.modules.templates.routes import manifest_items


tasks_blueprint = Blueprint("tasks", __name__)


def _rpc(function, parameters):
    data_api = current_app.extensions["kickoff_supabase_data"]
    try:
        result = data_api.rpc(function, g.auth_access_token, parameters)
    except SupabaseDataAPIError as error:
        if error.status == 401:
            return None, _error(401, "token_invalid")
        return None, _error(503, "service_unavailable")
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict) and result is not None:
        return None, _error(503, "service_unavailable")
    return result, None


def _path_uuid(value, field):
    try:
        return uuid_field(value, field=field), None
    except RequestValidationError as error:
        return None, api_error(422, "validation_failed", details=error.fields)


def _invalid(error):
    return api_error(422, "validation_failed", details=error.fields)


def _state_response(result, *, kind):
    state = result.get("state")
    if state == "not_found":
        return api_error(404, "not_found")
    if state == "version_conflict":
        response = api_error(
            409,
            "version_conflict",
            details=[{"field": "version", "code": "stale_version", "current": result.get("current")}],
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    if state == "task_limit":
        return api_error(409, "task_limit_reached")
    if state == "step_limit":
        return api_error(409, "step_limit_reached")
    if state == "invalid_order":
        return api_error(
            422,
            "validation_failed",
            details=[{"field": "step_ids", "code": "must_match_active_steps"}],
        )
    if state == "invalid_template":
        return api_error(
            422,
            "validation_failed",
            details=[{"field": "template_id", "code": "invalid_template"}],
        )
    if state == "invalid_source_template":
        return api_error(
            422,
            "validation_failed",
            details=[{"field": "template_id", "code": "source_template_mismatch"}],
        )
    if state == "invalid_transition":
        return api_error(422, "invalid_transition")
    if state is not None:
        return _error(503, "service_unavailable")
    if kind == "task" and isinstance(result.get("id"), str):
        response = jsonify(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    if kind == "step" and isinstance(result.get("step"), dict):
        response = jsonify(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    if kind == "deleted" and result.get("deleted") is True:
        response = jsonify(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return _error(503, "service_unavailable")


def _step_action(step_id, action):
    normalized_id, error_response = _path_uuid(step_id, "step_id")
    if error_response:
        return error_response
    try:
        version, actual_minutes = validate_step_action(request.json_body, action)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "act_on_my_step",
        {
            "p_step_id": normalized_id,
            "p_action": action,
            "p_expected_version": version,
            "p_actual_minutes": actual_minutes,
            "p_idempotency_key": g.idempotency_context["key"],
            "p_request_hash": g.idempotency_context["request_hash"],
            "p_lease_token": g.idempotency_context["lease_token"],
        },
    )
    if error_response:
        return error_response
    if isinstance(result, dict) and isinstance(result.get("step"), dict) and isinstance(result.get("reward_delta"), dict):
        # The RPC stored this exact response with the task/reward changes in
        # its transaction, so the outer idempotency middleware must not store
        # a second, non-atomic copy.
        g.idempotency_atomic_committed = True
    if isinstance(result, dict) and result.get("state") == "invalid_transition":
        current = result.get("current") or {}
        response = api_error(
            422,
            "invalid_transition",
            details=[{"field": "status", "code": "invalid_transition", "current": current}],
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    return _state_response(result, kind="step")


@tasks_blueprint.post("/v1/steps/<step_id>/start")
@require_auth
@idempotent_write
@rate_limited("steps_start", limit=60, window_seconds=60)
def start_step(step_id):
    return _step_action(step_id, "start")


@tasks_blueprint.post("/v1/steps/<step_id>/complete")
@require_auth
@idempotent_write
@rate_limited("steps_complete", limit=60, window_seconds=60)
def complete_step(step_id):
    return _step_action(step_id, "complete")


@tasks_blueprint.post("/v1/steps/<step_id>/reopen")
@require_auth
@idempotent_write
@rate_limited("steps_reopen", limit=60, window_seconds=60)
def reopen_step(step_id):
    return _step_action(step_id, "reopen")


@tasks_blueprint.post("/v1/tasks")
@require_auth
@idempotent_write
@rate_limited("tasks_create", limit=30, window_seconds=60)
def create_task():
    try:
        payload = validate_task_create(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "create_my_task",
        {"p_task": payload["task"], "p_steps": payload["steps"]},
    )
    if error_response:
        return error_response
    response = _state_response(result, kind="task")
    response.status_code = 201 if response.status_code == 200 else response.status_code
    return response


@tasks_blueprint.get("/v1/tasks")
@require_auth
@rate_limited("tasks_list", limit=120, window_seconds=60)
def list_tasks():
    try:
        query = validate_task_query(request.args)
    except RequestValidationError as error:
        return _invalid(error)
    if query["updated_since"] is not None:
        result, error_response = _rpc(
            "list_my_task_delta",
            {
                "p_updated_since": query["updated_since"],
                "p_snapshot_at": query["snapshot_at"],
                "p_status": query["status"],
                "p_due_date": query["due_date"],
                "p_cursor_updated_at": query["cursor_updated_at"],
                "p_cursor_id": query["cursor_id"],
                "p_limit": query["limit"],
            },
        )
        if error_response:
            return error_response
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            return _error(503, "service_unavailable")
        items = result["items"]
        next_cursor = encode_cursor(items[-1]) if result.get("has_more") and items else None
        payload = {
            "items": items,
            "next_cursor": next_cursor,
            "next_since": result.get("next_since"),
            "snapshot_at": result.get("snapshot_at"),
            "server_time": result.get("server_time"),
            "full_refresh_required": result.get("full_refresh_required") is True,
        }
        try:
            return private_json(payload)
        except ValueError:
            return _error(503, "response_too_large")

    result, error_response = _rpc(
        "list_my_tasks",
        {
            "p_status": query["status"],
            "p_due_date": query["due_date"],
            "p_updated_since": query["updated_since"],
            "p_cursor_updated_at": query["cursor_updated_at"],
            "p_cursor_id": query["cursor_id"],
            "p_limit": query["limit"],
        },
    )
    if error_response:
        return error_response
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return _error(503, "service_unavailable")
    items = result["items"]
    next_cursor = encode_cursor(items[-1]) if result.get("has_more") and items else None
    try:
        return private_json({"items": items, "next_cursor": next_cursor})
    except ValueError:
        return _error(503, "response_too_large")


@tasks_blueprint.get("/v1/bootstrap")
@require_auth
@rate_limited("bootstrap", limit=60, window_seconds=60)
def get_bootstrap():
    result, error_response = _rpc("get_my_bootstrap", {})
    if error_response:
        return error_response
    profile = result.get("profile") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or not isinstance(profile, dict)
        or not isinstance(result.get("reward_state"), dict)
        or not isinstance(result.get("tasks"), list)
        or len(result["tasks"]) > 50
        or profile.get("id") != g.auth_user_id
        or isinstance(result.get("active_task_count"), bool)
        or not isinstance(result.get("active_task_count"), int)
        or not isinstance(result.get("server_time"), str)
    ):
        return _error(503, "service_unavailable")
    templates = manifest_items(profile.get("language") or "en")
    if templates is None:
        return _error(503, "templates_unavailable")
    payload = {
        "profile": result["profile"],
        "reward_state": result["reward_state"],
        "tasks": result["tasks"],
        "active_task_count": result["active_task_count"],
        "template_manifest": {"items": templates},
        "server_time": result["server_time"],
        "api_version": "v1",
    }
    try:
        return private_json(payload)
    except ValueError:
        return _error(503, "response_too_large")


@tasks_blueprint.get("/v1/tasks/<task_id>")
@require_auth
@rate_limited("tasks_get", limit=120, window_seconds=60)
def get_task(task_id):
    normalized_id, error_response = _path_uuid(task_id, "task_id")
    if error_response:
        return error_response
    result, error_response = _rpc("get_my_task", {"p_task_id": normalized_id})
    if error_response:
        return error_response
    if result is None:
        return api_error(404, "not_found")
    if isinstance(result, dict) and result.get("id") == normalized_id:
        try:
            return private_json(result)
        except ValueError:
            return _error(503, "response_too_large")
    return _state_response(result, kind="task")


@tasks_blueprint.patch("/v1/tasks/<task_id>")
@require_auth
@idempotent_write
@rate_limited("tasks_patch", limit=60, window_seconds=60)
def patch_task(task_id):
    normalized_id, error_response = _path_uuid(task_id, "task_id")
    if error_response:
        return error_response
    try:
        version, patch = validate_task_patch(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "update_my_task",
        {"p_task_id": normalized_id, "p_expected_version": version, "p_patch": patch},
    )
    if error_response:
        return error_response
    return _state_response(result, kind="task")


@tasks_blueprint.delete("/v1/tasks/<task_id>")
@require_auth
@idempotent_write
@rate_limited("tasks_delete", limit=30, window_seconds=60)
def delete_task(task_id):
    normalized_id, error_response = _path_uuid(task_id, "task_id")
    if error_response:
        return error_response
    try:
        version = validate_version_body(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "soft_delete_my_task",
        {"p_task_id": normalized_id, "p_expected_version": version},
    )
    if error_response:
        return error_response
    return _state_response(result, kind="deleted")


@tasks_blueprint.post("/v1/tasks/<task_id>/steps")
@require_auth
@idempotent_write
@rate_limited("steps_create", limit=60, window_seconds=60)
def create_step(task_id):
    normalized_id, error_response = _path_uuid(task_id, "task_id")
    if error_response:
        return error_response
    try:
        payload = validate_step_create(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "create_my_step",
        {"p_task_id": normalized_id, "p_step": payload},
    )
    if error_response:
        return error_response
    response = _state_response(result, kind="step")
    response.status_code = 201 if response.status_code == 200 else response.status_code
    return response


@tasks_blueprint.patch("/v1/steps/<step_id>")
@require_auth
@idempotent_write
@rate_limited("steps_patch", limit=60, window_seconds=60)
def patch_step(step_id):
    normalized_id, error_response = _path_uuid(step_id, "step_id")
    if error_response:
        return error_response
    try:
        version, patch = validate_step_patch(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "update_my_step",
        {"p_step_id": normalized_id, "p_expected_version": version, "p_patch": patch},
    )
    if error_response:
        return error_response
    return _state_response(result, kind="step")


@tasks_blueprint.delete("/v1/steps/<step_id>")
@require_auth
@idempotent_write
@rate_limited("steps_delete", limit=60, window_seconds=60)
def delete_step(step_id):
    normalized_id, error_response = _path_uuid(step_id, "step_id")
    if error_response:
        return error_response
    try:
        version = validate_version_body(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "soft_delete_my_step",
        {"p_step_id": normalized_id, "p_expected_version": version},
    )
    if error_response:
        return error_response
    return _state_response(result, kind="deleted")


@tasks_blueprint.put("/v1/tasks/<task_id>/steps/order")
@require_auth
@idempotent_write
@rate_limited("steps_reorder", limit=30, window_seconds=60)
def reorder_steps(task_id):
    normalized_id, error_response = _path_uuid(task_id, "task_id")
    if error_response:
        return error_response
    try:
        version, step_ids = validate_reorder(request.json_body)
    except RequestValidationError as error:
        return _invalid(error)
    result, error_response = _rpc(
        "reorder_my_steps",
        {
            "p_task_id": normalized_id,
            "p_expected_version": version,
            "p_step_ids": step_ids,
        },
    )
    if error_response:
        return error_response
    return _state_response(result, kind="task")
