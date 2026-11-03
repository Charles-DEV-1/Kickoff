import logging
from uuid import UUID, uuid4

import pytest
from flask import g, jsonify

from app import create_app
from app.common.protections import idempotent_write, rate_limited
from app.common.validation import (
    MissingAuthContext,
    RequestValidationError,
    authenticated_user_id,
    integer_field,
    parse_json_object,
    user_scope_filter,
)


class PersistentFakeDataAPI:
    """Stateful RPC stand-in shared across requests and service instances."""

    def __init__(self, user_id):
        self.user_id = user_id
        self.rates = {}
        self.idempotency = {}

    def rpc(self, function_name, _token, parameters=None):
        if function_name == "consume_api_rate_limit":
            bucket = parameters["p_bucket"]
            count = self.rates.get(bucket, 0) + 1
            self.rates[bucket] = count
            limit = parameters["p_limit"]
            return {
                "user_id": self.user_id,
                "allowed": count <= limit,
                "remaining": max(0, limit - count),
                "retry_after_seconds": parameters["p_window_seconds"],
            }

        if function_name == "begin_api_idempotency":
            key = parameters["p_key"]
            request_hash = parameters["p_request_hash"]
            row = self.idempotency.get((self.user_id, key))
            if row is None:
                lease = str(uuid4())
                self.idempotency[(self.user_id, key)] = {
                    "hash": request_hash,
                    "state": "in_progress",
                    "lease": lease,
                }
                return {"state": "new", "lease_token": lease}
            if row["hash"] != request_hash:
                return {"state": "key_reused"}
            if row["state"] == "completed":
                return {
                    "state": "replay",
                    "response_status": row["status"],
                    "response_body": row["body"],
                }
            return {"state": "in_progress"}

        if function_name == "complete_api_idempotency":
            row = self.idempotency[(self.user_id, parameters["p_key"])]
            assert row["lease"] == parameters["p_lease_token"]
            row.update({
                "state": "completed",
                "status": parameters["p_response_status"],
                "body": parameters["p_response_body"],
            })
            return {"stored": True}

        if function_name == "release_api_idempotency":
            self.idempotency.pop((self.user_id, parameters["p_key"]), None)
            return {"released": True}

        raise AssertionError(f"Unexpected RPC: {function_name}")


def make_app(data_api=None, *, max_body=1024 * 1024):
    app = create_app({
        "TESTING": True,
        "PROPAGATE_EXCEPTIONS": False,
        "MAX_CONTENT_LENGTH": max_body,
        "ALLOWED_ORIGINS": (),
        "SUPABASE_DATA_API": data_api,
    })
    app.config["ALLOWED_ORIGINS"] = ()
    return app


def bind_test_user(app, user_id):
    @app.before_request
    def set_test_auth_context():
        g.auth_user_id = user_id
        g.auth_access_token = "test-token-that-is-not-logged"


def test_error_responses_and_logs_do_not_expose_exception_or_token(caplog):
    app = make_app()

    @app.get("/v1/raises")
    def raises_sensitive_error():
        raise RuntimeError("Authorization: Bearer secret-token task text")

    caplog.set_level(logging.ERROR, logger="app.common.errors")
    response = app.test_client().get("/v1/raises")

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"
    assert "request_id" in response.get_json()["error"]
    assert b"secret-token" not in response.data
    assert b"task text" not in response.data
    assert "secret-token" not in caplog.text
    assert "task text" not in caplog.text


def test_request_id_is_generated_and_valid_client_id_is_preserved(caplog):
    app = make_app()
    client = app.test_client()
    caplog.set_level(logging.INFO, logger="kickoff.request")

    generated = client.get("/v1/health")
    UUID(generated.headers["X-Request-Id"])

    supplied_id = str(uuid4())
    supplied = client.get("/v1/health", headers={"X-Request-Id": supplied_id})
    assert supplied.headers["X-Request-Id"] == supplied_id
    assert any(
        getattr(record, "request_id", None) == supplied_id
        for record in caplog.records
    )


def test_request_id_is_replaced_when_client_value_is_not_a_uuid():
    app = make_app()
    response = app.test_client().get(
        "/v1/health",
        headers={"X-Request-Id": "Bearer do-not-log-this"},
    )
    generated = response.headers["X-Request-Id"]
    UUID(generated)
    assert generated != "Bearer do-not-log-this"


def test_user_scope_uses_only_verified_context_and_validates_column():
    app = make_app()
    user_id = str(uuid4())
    with app.test_request_context("/"):
        g.auth_user_id = user_id
        assert user_scope_filter() == {"user_id": f"eq.{user_id}"}
        assert user_scope_filter("owner_id") == {"owner_id": f"eq.{user_id}"}
        with pytest.raises(ValueError):
            user_scope_filter("user_id;delete")

    with app.test_request_context("/"):
        with pytest.raises(MissingAuthContext):
            authenticated_user_id()


def test_validation_helpers_enforce_object_and_integer_boundaries():
    app = make_app()
    with app.test_request_context("/", method="POST", json={"count": True}):
        payload = parse_json_object()
        with pytest.raises(RequestValidationError):
            integer_field(payload, "count", minimum=0, maximum=10)

    with app.test_request_context("/", method="POST", json=[]):
        with pytest.raises(RequestValidationError):
            parse_json_object()


def test_api_write_requires_json_and_obeys_body_size_limit():
    app = make_app(max_body=24)
    client = app.test_client()

    unsupported = client.post("/v1/write", data="a=b")
    assert unsupported.status_code == 415
    assert unsupported.get_json()["error"]["code"] == "unsupported_media_type"

    too_large = client.post(
        "/v1/write",
        data='{"payload":"' + ("x" * 40) + '"}',
        content_type="application/json",
    )
    assert too_large.status_code == 413
    assert too_large.get_json()["error"]["code"] == "payload_too_large"


def test_persistent_rate_limit_counts_across_requests():
    user_id = str(uuid4())
    data_api = PersistentFakeDataAPI(user_id)
    app = make_app(data_api)
    bind_test_user(app, user_id)

    @app.post("/v1/limited")
    @rate_limited("test_bucket", limit=2, window_seconds=60)
    def limited():
        return jsonify({"ok": True})

    client = app.test_client()
    headers = {"Content-Type": "application/json"}
    assert client.post("/v1/limited", headers=headers, json={}).status_code == 200
    assert client.post("/v1/limited", headers=headers, json={}).status_code == 200
    limited_response = client.post("/v1/limited", headers=headers, json={})
    assert limited_response.status_code == 429
    assert limited_response.get_json()["error"]["code"] == "rate_limited"
    assert limited_response.headers["Retry-After"] == "60"
    assert data_api.rates["test_bucket"] == 3


def test_idempotency_replays_original_result_and_rejects_changed_body():
    user_id = str(uuid4())
    data_api = PersistentFakeDataAPI(user_id)
    app = make_app(data_api)
    bind_test_user(app, user_id)
    writes = {"count": 0}

    @app.post("/v1/write")
    @idempotent_write
    def write():
        writes["count"] += 1
        return jsonify({"write_number": writes["count"], "value": request_json_value()}), 201

    client = app.test_client()
    headers = {"Idempotency-Key": "test-key-123456", "Content-Type": "application/json"}
    first = client.post("/v1/write", headers=headers, json={"value": "one"})
    replay = client.post("/v1/write", headers=headers, json={"value": "one"})
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.get_json() == first.get_json()
    assert replay.headers["Idempotent-Replay"] == "true"
    assert writes["count"] == 1

    reused = client.post("/v1/write", headers=headers, json={"value": "different"})
    assert reused.status_code == 409
    assert reused.get_json()["error"]["code"] == "idempotency_key_reused"
    assert writes["count"] == 1


def test_idempotency_supports_bodyless_204_write_responses():
    user_id = str(uuid4())
    data_api = PersistentFakeDataAPI(user_id)
    app = make_app(data_api)
    bind_test_user(app, user_id)

    @app.delete("/v1/no-content")
    @idempotent_write
    def no_content():
        return "", 204

    headers = {"Idempotency-Key": "delete-key-123456"}
    client = app.test_client()
    first = client.delete("/v1/no-content", headers=headers)
    replay = client.delete("/v1/no-content", headers=headers)
    assert first.status_code == 204
    assert replay.status_code == 204
    assert replay.data == b""
    assert replay.headers["Idempotent-Replay"] == "true"


def test_idempotent_replay_precedes_rate_limit_and_429_does_not_poison_key():
    user_id = str(uuid4())
    data_api = PersistentFakeDataAPI(user_id)
    app = make_app(data_api)
    bind_test_user(app, user_id)
    writes = {"count": 0}

    @app.post("/v1/limited-write")
    @idempotent_write
    @rate_limited("one_per_window", limit=1, window_seconds=60)
    def limited_write():
        writes["count"] += 1
        return jsonify({"count": writes["count"]})

    client = app.test_client()
    first_headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "same-write-123456",
    }
    first = client.post("/v1/limited-write", headers=first_headers, json={"value": 1})
    replay = client.post("/v1/limited-write", headers=first_headers, json={"value": 1})
    assert first.status_code == replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert data_api.rates["one_per_window"] == 1
    assert writes["count"] == 1

    limited_headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": "rate-limited-123456",
    }
    limited = client.post("/v1/limited-write", headers=limited_headers, json={"value": 2})
    assert limited.status_code == 429
    assert (user_id, limited_headers["Idempotency-Key"]) not in data_api.idempotency


def request_json_value():
    from flask import request
    return request.get_json()["value"]
