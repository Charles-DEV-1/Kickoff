from urllib.error import URLError
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, jsonify

system_blueprint = Blueprint("system", __name__)


@system_blueprint.get("/v1/health")
def health():
    return jsonify({"status": "ok"}), 200


@system_blueprint.get("/v1/health/ready")
def readiness():
    project_url = current_app.config.get("SUPABASE_URL", "")
    publishable_key = current_app.config.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not project_url or not publishable_key:
        return jsonify({
            "status": "not_ready",
            "dependency": "supabase_auth",
        }), 503

    request = Request(
        f"{project_url}/auth/v1/health",
        headers={"apikey": publishable_key},
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                raise URLError("Supabase Auth health returned an unexpected status")
    except (URLError, TimeoutError, OSError):
        return jsonify({
            "status": "not_ready",
            "dependency": "supabase_auth",
        }), 503

    return jsonify({"status": "ready"}), 200
