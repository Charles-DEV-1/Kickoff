import logging
import uuid

from flask import g, request


logger = logging.getLogger("kickoff.request")


def register_request_id(app):
    @app.before_request
    def assign_request_id():
        supplied = request.headers.get("X-Request-Id", "")
        try:
            request_id = str(uuid.UUID(supplied))
        except (ValueError, AttributeError, TypeError):
            request_id = str(uuid.uuid4())

        g.request_id = request_id
        request.request_id = request_id

    @app.after_request
    def attach_request_id(response):
        request_id = getattr(g, "request_id", str(uuid.uuid4()))
        response.headers["X-Request-Id"] = request_id
        if request.path.startswith("/v1/"):
            logger.info(
                "Request completed",
                extra={
                    "request_id": request_id,
                    "route": request.url_rule.rule if request.url_rule else "unmatched",
                    "method": request.method,
                    "status_code": response.status_code,
                },
            )
        return response
