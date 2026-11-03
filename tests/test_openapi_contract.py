import json
import re
from pathlib import Path

from app import create_app


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "openapi" / "openapi.yaml"


def _load_spec():
    # openapi.yaml is JSON-compatible YAML (a YAML 1.2 subset), so the
    # contract test can parse it with the Python standard library in CI.
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _ref_exists(document, reference):
    if not reference.startswith("#/"):
        return False
    value = document
    for segment in reference[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or segment not in value:
            return False
        value = value[segment]
    return True


def _walk_refs(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from _walk_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_refs(child)


def _parameter_name(spec, parameter):
    if "$ref" in parameter:
        reference = parameter["$ref"]
        return reference.rsplit("/", 1)[-1]
    return parameter.get("name")


def _actual_routes():
    app = create_app({"TESTING": True, "AI_PROVIDER": "disabled"})
    routes = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", rule.rule)
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            routes.add((path, method.lower()))
    return routes


def _documented_routes(spec):
    return {
        (path, method.lower())
        for path, path_item in spec["paths"].items()
        for method in path_item
        if method.lower() in {"get", "post", "put", "patch", "delete"}
    }


def test_openapi_is_valid_json_yaml_subset_and_references_resolve():
    spec = _load_spec()
    assert spec["openapi"].startswith("3.1.")
    assert spec["info"]["title"] == "Kickoff Online MVP API"
    assert all(_ref_exists(spec, reference) for reference in _walk_refs(spec))


def test_openapi_routes_match_flask_and_leave_deferred_routes_out():
    spec = _load_spec()
    assert _documented_routes(spec) == _actual_routes()
    paths = set(spec["paths"])
    assert not any(
        token in path.lower()
        for path in paths
        for token in ("offline", "sync", "routines", "reminders", "analytics")
    )
    assert "/v1/rewards/state" not in paths
    assert "/v1/ai/usage" not in paths
    assert "/v1/routines" not in paths
    assert "/v1/reminders" not in paths


def test_security_schemes_and_method_access_match_route_boundaries():
    spec = _load_spec()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"]["scheme"] == "bearer"
    assert schemes["cronBearerAuth"]["scheme"] == "bearer"

    public_paths = {
        "/v1/health", "/v1/health/ready", "/v1/templates/manifest", "/v1/templates",
        "/v1/templates/{template_id}",
    }
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method in {"parameters", "summary", "description", "servers"}:
                continue
            if path == "/internal/jobs/purge":
                assert operation["security"] == [{"cronBearerAuth": []}]
            elif path in public_paths:
                assert operation["security"] == []
            else:
                assert operation["security"] == [{"bearerAuth": []}]


def test_idempotency_etags_limits_and_secret_placement_are_documented():
    spec = _load_spec()
    write_methods = {"post", "put", "patch", "delete"}
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method in write_methods and not (path == "/v1/ai/breakdown" and method == "post"):
                names = {_parameter_name(spec, parameter) for parameter in operation.get("parameters", [])}
                assert "IdempotencyKey" in names

    for path, method in (
        ("/v1/me", "get"), ("/v1/bootstrap", "get"), ("/v1/tasks", "get"),
        ("/v1/tasks/{task_id}", "get"), ("/v1/templates/manifest", "get"),
        ("/v1/templates", "get"), ("/v1/templates/{template_id}", "get"),
    ):
        parameters = spec["paths"][path][method].get("parameters", [])
        assert any(_parameter_name(spec, parameter) == "IfNoneMatch" for parameter in parameters)
        assert "304" in spec["paths"][path][method]["responses"]

    task_create = spec["components"]["schemas"]["TaskCreateRequest"]["properties"]
    step_create = spec["components"]["schemas"]["StepCreateRequest"]["properties"]
    assert task_create["title"]["maxLength"] == 200
    assert task_create["notes"]["maxLength"] == 500
    assert task_create["steps"]["maxItems"] == 30
    assert step_create["text"]["maxLength"] == 280
    assert step_create["planned_minutes"]["maximum"] == 240

    raw = SPEC_PATH.read_text(encoding="utf-8").lower()
    for forbidden in ("sb_secret_", "supabase_secret_key", "groq_api_key", "database_url", "bearer eyj"):
        assert forbidden not in raw


def test_operation_ids_are_unique_and_success_responses_have_request_id_headers():
    spec = _load_spec()
    operations = [
        operation
        for path_item in spec["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    ids = [operation["operationId"] for operation in operations]
    assert len(ids) == len(set(ids))
    for operation in operations:
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                headers = response.get("headers", {})
                if "$ref" in response:
                    continue
                assert "X-Request-Id" in headers
