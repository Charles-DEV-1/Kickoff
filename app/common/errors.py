import logging

from flask import g, jsonify, request
from werkzeug.exceptions import BadRequest, HTTPException, RequestEntityTooLarge


logger = logging.getLogger(__name__)

ERROR_MESSAGES = {
    "account_deleted": "This account is no longer available.",
    "auth_unavailable": "Sign-in verification is temporarily unavailable.",
    "bad_request": "The request could not be completed.",
    "idempotency_unavailable": "This request could not be safely recorded. Please retry.",
    "idempotency_key_required": "Provide an Idempotency-Key for this write request.",
    "idempotency_key_reused": "This Idempotency-Key was already used for a different request.",
    "internal_error": "Something went wrong. Please try again.",
    "invalid_json": "The request body must contain valid JSON.",
    "invalid_transition": "This step cannot move to that state.",
    "method_not_allowed": "This method is not allowed for this resource.",
    "not_found": "The requested resource was not found.",
    "payload_too_large": "The request body is too large.",
    "rate_limit_unavailable": "Request limits could not be checked. Please retry.",
    "rate_limited": "Too many requests. Please wait before trying again.",
    "request_failed": "The request could not be completed.",
    "request_in_progress": "A request with this Idempotency-Key is still processing.",
    "service_unavailable": "This service is temporarily unavailable.",
    "token_expired": "Your sign-in has expired. Please refresh and try again.",
    "token_invalid": "A valid sign-in is required.",
    "token_revoked": "Your sign-in is no longer valid. Please sign in again.",
    "templates_unavailable": "Reviewed templates are temporarily unavailable.",
    "task_limit_reached": "You have reached the active task limit.",
    "step_limit_reached": "This task has reached its step limit.",
    "unsupported_media_type": "Send this request as JSON.",
    "validation_failed": "One or more request fields are invalid.",
    "version_conflict": "This resource changed. Review the current version and retry.",
}


def api_error(status, code, *, message=None, details=None, headers=None):
    error = {
        "code": code,
        "message": message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["request_failed"]),
        "request_id": getattr(g, "request_id", None),
    }
    if details is not None:
        error["details"] = details
    response = jsonify({"error": error})
    response.status_code = status
    for name, value in (headers or {}).items():
        response.headers[name] = value
    return response


def register_error_handlers(app):
    @app.errorhandler(RequestEntityTooLarge)
    def handle_payload_too_large(_error):
        return api_error(413, "payload_too_large")

    @app.errorhandler(BadRequest)
    def handle_bad_request(_error):
        return api_error(400, "invalid_json")

    @app.errorhandler(HTTPException)
    def handle_http_error(error):
        status = error.code or 500
        code = {
            400: "bad_request",
            404: "not_found",
            405: "method_not_allowed",
            413: "payload_too_large",
            415: "unsupported_media_type",
        }.get(status, "request_failed")
        return api_error(status, code)

    @app.errorhandler(Exception)
    def handle_unexpected_error(_error):
        logger.error(
            "Unhandled request error",
            extra={
                "request_id": getattr(g, "request_id", "unknown"),
                "route": request.url_rule.rule if request.url_rule else "unmatched",
                "method": request.method,
            },
        )
        return api_error(500, "internal_error")
