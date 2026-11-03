import hmac

from flask import Blueprint, current_app, jsonify, request

from app.common.supabase_data import SupabaseDataAPIError


retention_blueprint = Blueprint("retention", __name__)


def _as_dict(value):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    return value if isinstance(value, dict) else None


def _server_rpc(data_api, name, secret, parameters=None):
    try:
        return data_api.rpc_server(name, secret, parameters)
    except SupabaseDataAPIError:
        raise


def _auth_delete(data_api, secret, deletion_id, user_id, phase):
    succeeded = False
    try:
        data_api.delete_auth_user(
            user_id,
            secret,
            should_soft_delete=(phase == "soft"),
        )
        succeeded = True
    except Exception as error:
        # A missing Auth user already satisfies the hard-delete goal. Treat
        # missing during soft-delete the same way: no Auth identity remains.
        if getattr(error, "status", None) == 404:
            succeeded = True
    _server_rpc(
        data_api,
        "record_account_auth_delete_attempt",
        secret,
        {
            "p_deletion_id": deletion_id,
            "p_phase": phase,
            "p_succeeded": succeeded,
        },
    )
    return succeeded


@retention_blueprint.get("/internal/jobs/purge")
def run_daily_retention_job():
    configured_secret = current_app.config.get("CRON_SECRET", "")
    supplied_header = request.headers.get("Authorization", "")
    expected_header = f"Bearer {configured_secret}" if configured_secret else ""
    if not configured_secret:
        return jsonify({"error": "job_not_configured"}), 503
    if not hmac.compare_digest(supplied_header, expected_header):
        return jsonify({"error": "unauthorized"}), 401

    data_api = current_app.extensions["kickoff_supabase_data"]
    service_secret = current_app.config.get("SUPABASE_SECRET_KEY", "")
    if not service_secret:
        return jsonify({"error": "job_not_configured"}), 503

    try:
        soft_rows = _server_rpc(data_api, "list_pending_auth_soft_deletions", service_secret)
        if not isinstance(soft_rows, list):
            raise SupabaseDataAPIError
        soft_succeeded = 0
        soft_failed = 0
        for row in soft_rows:
            if not isinstance(row, dict):
                continue
            if _auth_delete(data_api, service_secret, row.get("id"), row.get("user_id"), "soft"):
                soft_succeeded += 1
            else:
                soft_failed += 1

        purge_result = _as_dict(_server_rpc(
            data_api, "run_data_retention_purge", service_secret
        ))
        if purge_result is None:
            raise SupabaseDataAPIError

        hard_rows = _server_rpc(data_api, "list_pending_auth_hard_deletions", service_secret)
        if not isinstance(hard_rows, list):
            raise SupabaseDataAPIError
        hard_succeeded = 0
        hard_failed = 0
        for row in hard_rows:
            if not isinstance(row, dict):
                continue
            if _auth_delete(data_api, service_secret, row.get("id"), row.get("user_id"), "hard"):
                hard_succeeded += 1
            else:
                hard_failed += 1
    except SupabaseDataAPIError:
        return jsonify({"error": "job_unavailable"}), 503

    response = jsonify({
        "status": "completed",
        "soft_auth_deletes_succeeded": soft_succeeded,
        "soft_auth_deletes_pending": soft_failed,
        "retention": purge_result,
        "hard_auth_deletes_succeeded": hard_succeeded,
        "hard_auth_deletes_pending": hard_failed,
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200
