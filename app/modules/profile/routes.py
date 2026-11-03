from flask import Blueprint, current_app, g, jsonify, make_response, request

from app.common.auth import _error, require_auth
from app.common.errors import api_error
from app.common.private_cache import private_json
from app.common.protections import idempotent_write, rate_limited
from app.common.supabase_data import SupabaseDataAPIError
from app.modules.profile.schemas import (
    ProfileValidationError,
    validate_profile_patch,
)


profile_blueprint = Blueprint("profile", __name__)


@profile_blueprint.get("/v1/me")
@require_auth
def get_me():
    try:
        return private_json(g.auth_profile)
    except ValueError:
        return _error(503, "response_too_large")


@profile_blueprint.patch("/v1/me")
@require_auth
@idempotent_write
@rate_limited("profile_patch", limit=30, window_seconds=60)
def patch_me():
    payload = getattr(request, "json_body", None)
    try:
        patch = validate_profile_patch(
            payload,
            current_app.config["SUPPORTED_LANGUAGES"],
        )
    except ProfileValidationError as error:
        return api_error(422, "validation_failed", details=error.fields)

    data_api = current_app.extensions["kickoff_supabase_data"]
    try:
        profile = data_api.rpc(
            "patch_my_profile",
            g.auth_access_token,
            {"p_patch": patch},
        )
    except SupabaseDataAPIError as error:
        if error.status == 401:
            return _error(401, "token_invalid")
        return _error(503, "service_unavailable")

    if isinstance(profile, list) and len(profile) == 1:
        profile = profile[0]
    if not isinstance(profile, dict) or profile.get("id") != g.auth_user_id:
        return _error(503, "service_unavailable")

    public_profile = {
        key: profile.get(key)
        for key in (
            "id", "display_name", "language", "timezone",
            "settings", "created_at", "updated_at",
        )
    }
    response = jsonify(public_profile)
    response.headers["Cache-Control"] = "private, no-store"
    return response, 200


@profile_blueprint.get("/v1/me/export")
@require_auth
@rate_limited("profile_export", limit=3, window_seconds=86400)
def export_me():
    data_api = current_app.extensions["kickoff_supabase_data"]
    try:
        payload = data_api.rpc(
            "export_my_data",
            g.auth_access_token,
            max_response_bytes=8 * 1024 * 1024,
        )
    except SupabaseDataAPIError as error:
        if error.status == 401:
            return _error(401, "token_invalid")
        if error.status == 413:
            return _error(413, "payload_too_large")
        return _error(503, "service_unavailable")

    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict) or not isinstance(payload.get("profile"), dict):
        return _error(503, "service_unavailable")
    # The Auth email is not duplicated in the app profile table. Include it in
    # the export envelope from the already verified Auth user response.
    payload["account"] = {"id": g.auth_user_id, "email": g.auth_email}
    response = make_response(jsonify(payload), 200)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="kickoff-data-export.json"'
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@profile_blueprint.delete("/v1/me")
@require_auth
@idempotent_write
@rate_limited("profile_delete", limit=5, window_seconds=86400)
def delete_me():
    payload = getattr(request, "json_body", None)
    if not isinstance(payload, dict) or set(payload) != {"confirm"} or payload.get("confirm") != "DELETE":
        return api_error(
            422,
            "validation_failed",
            details=[{"field": "confirm", "code": "must_equal_DELETE"}],
        )

    data_api = current_app.extensions["kickoff_supabase_data"]
    try:
        deletion = data_api.rpc(
            "request_my_account_deletion",
            g.auth_access_token,
            {"p_confirmation": "DELETE"},
        )
    except SupabaseDataAPIError as error:
        if error.status == 401:
            return _error(401, "token_invalid")
        return _error(503, "service_unavailable")

    if isinstance(deletion, list) and len(deletion) == 1:
        deletion = deletion[0]
    if not isinstance(deletion, dict):
        return _error(503, "service_unavailable")
    if deletion.get("state") == "not_found":
        return _error(404, "not_found")
    deletion_id = deletion.get("id")
    purge_after = deletion.get("purge_after")
    if not isinstance(deletion_id, str) or not isinstance(purge_after, str):
        return _error(503, "service_unavailable")

    # Local lockout and the 30-day tombstone are already committed by the RPC.
    # Auth soft deletion is best effort here; the daily protected job retries.
    auth_deleted = _auth_delete_attempt(data_api, deletion_id, g.auth_user_id, soft=True)
    response = jsonify({
        "status": "pending",
        "requested_at": deletion.get("requested_at"),
        "purge_after": purge_after,
        "auth_deletion_status": "soft_deleted" if auth_deleted else "pending_retry",
    })
    response.status_code = 202
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _auth_delete_attempt(data_api, deletion_id, user_id, *, soft):
    secret = current_app.config.get("SUPABASE_SECRET_KEY", "")
    succeeded = False
    try:
        data_api.delete_auth_user(user_id, secret, should_soft_delete=soft)
        succeeded = True
    except Exception as error:
        # An Auth 404 means the requested state is already achieved. Never log
        # provider response text, credentials, user data, or request bodies.
        if getattr(error, "status", None) == 404:
            succeeded = True
    try:
        data_api.rpc_server(
            "record_account_auth_delete_attempt",
            secret,
            {
                "p_deletion_id": deletion_id,
                "p_phase": "soft" if soft else "hard",
                "p_succeeded": succeeded,
            },
        )
    except Exception:
        # The worker can safely repeat the Auth operation and record it later.
        pass
    return succeeded
