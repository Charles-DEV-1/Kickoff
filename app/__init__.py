import gzip

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

from app.common.errors import api_error, register_error_handlers
from app.common.jwks import JwksKeyProvider
from app.common.request_id import register_request_id
from app.common.public_rate_limit import PublicIPRateLimiter
from app.common.supabase_data import SupabaseDataAPI
from app.common.validation import RequestValidationError, parse_json_object
from app.config import Settings
from app.modules.profile.routes import profile_blueprint
from app.modules.ai.routes import ai_blueprint
from app.modules.system.routes import system_blueprint
from app.modules.system.retention import retention_blueprint
from app.modules.templates.routes import templates_blueprint
from app.modules.tasks.routes import tasks_blueprint
from app.modules.ai.service import SupabaseAIStore
from app.modules.ai.groq_provider import GroqProviderAdapter


def create_app(settings=None):
    app = Flask(__name__)
    if settings is None:
        app.config.from_object(Settings)
    elif isinstance(settings, dict):
        app.config.from_mapping(settings)
    else:
        app.config.from_object(settings)

    app.config.setdefault("MAX_CONTENT_LENGTH", Settings.MAX_CONTENT_LENGTH)
    app.config.setdefault("ALLOWED_ORIGINS", Settings.ALLOWED_ORIGINS)
    trusted_proxy_hops = app.config.get("PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS", 0)
    if isinstance(trusted_proxy_hops, bool) or not isinstance(trusted_proxy_hops, int) or not 0 <= trusted_proxy_hops <= 5:
        raise ValueError("PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS must be an integer from 0 to 5")
    if trusted_proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_hops)
    app.extensions["kickoff_jwks"] = app.config.get("JWT_KEY_PROVIDER") or JwksKeyProvider(
        app.config.get("SUPABASE_JWKS_URL", ""),
        app.config.get("SUPABASE_JWT_ALGORITHMS", ("ES256",)),
        ttl_seconds=600,
        timeout_seconds=3,
    )
    app.extensions["kickoff_supabase_data"] = app.config.get("SUPABASE_DATA_API") or SupabaseDataAPI(
        app.config.get("SUPABASE_URL", ""),
        app.config.get("SUPABASE_PUBLISHABLE_KEY", ""),
    )
    app.extensions["kickoff_public_ip_limiter"] = app.config.get("PUBLIC_IP_RATE_LIMITER") or PublicIPRateLimiter(
        limit=60, window_seconds=60
    )
    ai_provider = app.config.get("AI_PROVIDER_ADAPTER")
    ai_provider_name = app.config.get("AI_PROVIDER", "disabled")
    if ai_provider is None and ai_provider_name == "groq":
        groq_api_key = app.config.get("GROQ_API_KEY", "")
        if groq_api_key:
            ai_provider = GroqProviderAdapter(
                groq_api_key, app.config.get("GROQ_MODEL", "openai/gpt-oss-20b")
            )
    elif ai_provider is None and ai_provider_name != "disabled":
        raise ValueError("AI_PROVIDER must be 'disabled' or a configured provider adapter")
    app.extensions["kickoff_ai_provider"] = ai_provider
    app.extensions["kickoff_ai_store"] = app.config.get("AI_STORE") or SupabaseAIStore(
        app.extensions["kickoff_supabase_data"], app.config.get("SUPABASE_SECRET_KEY", "")
    )

    register_request_id(app)
    register_error_handlers(app)

    @app.before_request
    def enforce_json_for_api_writes():
        if not request.path.startswith("/v1/"):
            return None
        has_body = request.content_length not in (None, 0)
        requires_json = request.method in {"POST", "PUT", "PATCH"} or (
            request.method == "DELETE" and has_body
        )
        if not requires_json:
            return None
        configured_limit = app.config.get("MAX_CONTENT_LENGTH")
        if (
            configured_limit is not None
            and request.content_length is not None
            and request.content_length > configured_limit
        ):
            return api_error(413, "payload_too_large")
        if not request.is_json:
            return api_error(415, "unsupported_media_type")
        try:
            payload = parse_json_object()
        except RequestValidationError as error:
            if error.fields and error.fields[0]["code"] == "invalid_json":
                return api_error(400, "invalid_json", details=error.fields)
            return api_error(422, "validation_failed", details=error.fields)
        request.json_body = payload

    app.register_blueprint(system_blueprint)
    app.register_blueprint(retention_blueprint)
    app.register_blueprint(profile_blueprint)
    app.register_blueprint(tasks_blueprint)
    app.register_blueprint(templates_blueprint)
    app.register_blueprint(ai_blueprint)

    @app.after_request
    def add_security_and_cors_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")

        origin = request.headers.get("Origin")
        if origin and origin in app.config["ALLOWED_ORIGINS"]:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.vary.add("Origin")
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, Idempotency-Key, If-None-Match"
            )
            response.headers["Access-Control-Expose-Headers"] = (
                "ETag, X-Request-Id, Retry-After, Idempotent-Replay, X-Server-Time, X-Next-Since, X-Snapshot-At"
            )

        # Compress JSON responses when the client supports gzip. Personal
        # endpoints set Vary/Cache-Control themselves; Vary is also required
        # here so caches distinguish compressed and identity representations.
        if (
            response.status_code not in (204, 304)
            and response.headers.get("Content-Encoding") is None
            and response.mimetype == "application/json"
            and request.accept_encodings["gzip"]
            and response.content_length is not None
            and response.content_length >= 1024
        ):
            compressed_body = gzip.compress(response.get_data(), mtime=0)
            response.set_data(compressed_body)
            response.headers["Content-Encoding"] = "gzip"
            response.vary.add("Accept-Encoding")
        return response

    @app.route("/v1/<path:path>", methods=["OPTIONS"])
    def preflight(path):
        origin = request.headers.get("Origin")
        if not origin or origin not in app.config["ALLOWED_ORIGINS"]:
            return jsonify({
                "error": {
                    "code": "origin_not_allowed",
                    "message": "This origin is not allowed.",
                    "request_id": request.request_id,
                }
            }), 403
        return "", 204

    return app
