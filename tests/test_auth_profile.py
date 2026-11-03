from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import create_app


class StaticKeyProvider:
    def __init__(self, key):
        self.key = key

    def get_public_key(self, kid, algorithm):
        if kid != "test-key" or algorithm != "ES256":
            raise jwt.InvalidTokenError
        return self.key


class FakeDataAPI:
    def __init__(self, profile):
        self.profile = profile
        self.calls = []
        self.auth_user_status = None
        self.auth_user_id = profile["id"]
        self.rate_counts = {}
        self.idempotency = {}

    def get_authenticated_user(self, access_token):
        self.calls.append(("get_authenticated_user", access_token, None))
        if self.auth_user_status is not None:
            from app.common.supabase_data import SupabaseAuthAPIError
            raise SupabaseAuthAPIError(status=self.auth_user_status)
        return {"id": self.auth_user_id}

    def rpc(self, function_name, access_token, parameters=None):
        self.calls.append((function_name, access_token, parameters))
        if function_name == "consume_api_rate_limit":
            bucket = parameters["p_bucket"]
            count = self.rate_counts.get(bucket, 0) + 1
            self.rate_counts[bucket] = count
            limit = parameters["p_limit"]
            return {
                "user_id": self.profile["id"],
                "allowed": count <= limit,
                "remaining": max(0, limit - count),
                "retry_after_seconds": parameters["p_window_seconds"],
            }
        if function_name == "begin_api_idempotency":
            key = parameters["p_key"]
            request_hash = parameters["p_request_hash"]
            existing = self.idempotency.get(key)
            if existing is None:
                lease = str(uuid4())
                self.idempotency[key] = {
                    "hash": request_hash,
                    "state": "in_progress",
                    "lease": lease,
                }
                return {"state": "new", "lease_token": lease}
            if existing["hash"] != request_hash:
                return {"state": "key_reused"}
            if existing["state"] == "completed":
                return {
                    "state": "replay",
                    "response_status": existing["status"],
                    "response_body": existing["body"],
                }
            return {"state": "in_progress"}
        if function_name == "complete_api_idempotency":
            record = self.idempotency[parameters["p_key"]]
            assert record["lease"] == parameters["p_lease_token"]
            record.update({
                "state": "completed",
                "status": parameters["p_response_status"],
                "body": parameters["p_response_body"],
            })
            return {"stored": True}
        if function_name == "release_api_idempotency":
            self.idempotency.pop(parameters["p_key"], None)
            return {"released": True}
        if function_name == "ensure_user_profile":
            return dict(self.profile)
        if function_name == "get_my_account_deletion_status":
            return None
        if function_name == "patch_my_profile":
            patch = parameters["p_patch"]
            for field in ("display_name", "language", "timezone"):
                if field in patch:
                    self.profile[field] = patch[field]
            self.profile["settings"].update(patch.get("settings", {}))
            return dict(self.profile)
        raise AssertionError("Unexpected RPC")


@pytest.fixture
def auth_setup():
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_id = str(uuid4())
    session_id = str(uuid4())
    profile = {
        "id": user_id,
        "display_name": None,
        "language": "en",
        "timezone": "Africa/Lagos",
        "settings": {"calm_mode": True},
        "created_at": "2026-11-02T00:00:00+00:00",
        "updated_at": "2026-11-02T00:00:00+00:00",
        "deleted_at": None,
        "tokens_valid_after": None,
    }
    data_api = FakeDataAPI(profile)
    app = create_app({
        "TESTING": True,
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_JWKS_URL": "https://test-project.supabase.co/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": "https://test-project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_JWT_ALGORITHMS": ("ES256",),
        "SUPABASE_JWT_LEEWAY_SECONDS": 0,
        "SUPPORTED_LANGUAGES": ("en",),
        "ALLOWED_ORIGINS": ("https://frontend.example",),
        "JWT_KEY_PROVIDER": StaticKeyProvider(private_key.public_key()),
        "SUPABASE_DATA_API": data_api,
    })
    return app, private_key, user_id, session_id, data_api


def make_token(private_key, user_id, session_id, **overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "session_id": session_id,
        "role": "authenticated",
        "iss": "https://test-project.supabase.co/auth/v1",
        "aud": "authenticated",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key"},
    )


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def patch_headers(token, key="test-idempotency-key"):
    return {
        **auth_header(token),
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    }


def test_valid_token_creates_auth_context_and_returns_own_profile(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)

    response = app.test_client().get("/v1/me", headers=auth_header(token))

    assert response.status_code == 200
    assert response.get_json()["id"] == user_id
    assert data_api.calls[0][0] == "get_authenticated_user"
    assert data_api.calls[1][0] == "ensure_user_profile"
    assert data_api.calls[1][2] is None


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({}, "token_invalid"),
        ({"Authorization": "Bearer not-a-jwt"}, "token_invalid"),
        ({"Authorization": "Basic abc"}, "token_invalid"),
    ],
)
def test_missing_or_malformed_token_is_rejected(auth_setup, headers, code):
    app, *_ = auth_setup
    response = app.test_client().get("/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == code


def test_expired_token_is_rejected(auth_setup):
    app, private_key, user_id, session_id, _ = auth_setup
    now = datetime.now(timezone.utc)
    token = make_token(
        private_key,
        user_id,
        session_id,
        iat=int((now - timedelta(minutes=10)).timestamp()),
        exp=int((now - timedelta(minutes=5)).timestamp()),
    )
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_expired"


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://wrong-project.supabase.co/auth/v1"},
        {"aud": "wrong-audience"},
        {"role": "anon"},
        {"is_anonymous": True},
    ],
)
def test_wrong_project_audience_and_anonymous_tokens_are_rejected(auth_setup, overrides):
    app, private_key, user_id, session_id, _ = auth_setup
    token = make_token(private_key, user_id, session_id, **overrides)
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_invalid"


def test_bad_signature_is_rejected(auth_setup):
    app, _, user_id, session_id, _ = auth_setup
    wrong_key = ec.generate_private_key(ec.SECP256R1())
    token = make_token(wrong_key, user_id, session_id)
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_invalid"


def test_deleted_account_and_revoked_token_are_rejected(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)
    data_api.profile["deleted_at"] = "2026-11-01T00:00:00+00:00"
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "account_deleted"

    data_api.profile["deleted_at"] = None
    data_api.profile["tokens_valid_after"] = (
        datetime.now(timezone.utc) + timedelta(minutes=1)
    ).isoformat()
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_revoked"


def test_supabase_revoked_session_is_rejected_before_profile_access(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)
    data_api.auth_user_status = 401

    response = app.test_client().get("/v1/me", headers=auth_header(token))

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "token_revoked"
    assert [call[0] for call in data_api.calls] == [
        "get_authenticated_user", "get_my_account_deletion_status"
    ]


def test_deleted_supabase_account_is_rejected_before_profile_access(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)
    data_api.auth_user_status = 404

    response = app.test_client().get("/v1/me", headers=auth_header(token))

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "account_deleted"
    assert [call[0] for call in data_api.calls] == ["get_authenticated_user"]


def test_data_api_profile_mismatch_is_not_returned(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    other_id = str(uuid4())
    data_api.profile["id"] = other_id
    token = make_token(private_key, user_id, session_id)

    response = app.test_client().get("/v1/me", headers=auth_header(token))

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "service_unavailable"


def test_patch_merges_settings_and_preserves_other_profile_values(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)

    response = app.test_client().patch(
        "/v1/me",
        headers=patch_headers(token),
        json={"settings": {"sound_enabled": False}},
    )

    assert response.status_code == 200
    assert response.get_json()["settings"] == {
        "calm_mode": True,
        "sound_enabled": False,
    }
    assert "user_id" not in next(
        call[2]["p_patch"]
        for call in data_api.calls
        if call[0] == "patch_my_profile"
    )

    replay = app.test_client().patch(
        "/v1/me",
        headers=patch_headers(token),
        json={"settings": {"sound_enabled": False}},
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert sum(call[0] == "patch_my_profile" for call in data_api.calls) == 1

    reused = app.test_client().patch(
        "/v1/me",
        headers=patch_headers(token),
        json={"display_name": "different request"},
    )
    assert reused.status_code == 409
    assert reused.get_json()["error"]["code"] == "idempotency_key_reused"


def test_patch_rejects_user_id_and_unknown_settings_key(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)
    client = app.test_client()
    headers = patch_headers(token, "test-key-user-id")

    response = client.patch("/v1/me", headers=headers, json={"user_id": str(uuid4())})
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"

    response = client.patch(
        "/v1/me",
        headers=patch_headers(token, "test-key-unknown"),
        json={"settings": {"admin": True}},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"
    assert sum(call[0] == "patch_my_profile" for call in data_api.calls) == 0


def test_patch_requires_idempotency_key(auth_setup):
    app, private_key, user_id, session_id, _ = auth_setup
    token = make_token(private_key, user_id, session_id)
    response = app.test_client().patch(
        "/v1/me",
        headers={**auth_header(token), "Content-Type": "application/json"},
        json={"display_name": "Updated"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "idempotency_key_required"


def test_patch_validates_timezone_language_and_setting_types(auth_setup):
    app, private_key, user_id, session_id, _ = auth_setup
    token = make_token(private_key, user_id, session_id)
    headers = patch_headers(token, "test-key-timezone")

    response = app.test_client().patch(
        "/v1/me",
        headers=patch_headers(token, "test-key-language"),
        json={"timezone": "Not/A_Zone"},
    )
    assert response.status_code == 422

    response = app.test_client().patch(
        "/v1/me",
        headers=patch_headers(token, "test-key-setting"),
        json={"language": "xx"},
    )
    assert response.status_code == 422

    response = app.test_client().patch(
        "/v1/me",
        headers=headers,
        json={"settings": {"daily_reminder_cap": True}},
    )
    assert response.status_code == 422


def test_rls_backed_profile_rpc_never_accepts_a_target_user_id(auth_setup):
    app, private_key, user_id, session_id, data_api = auth_setup
    token = make_token(private_key, user_id, session_id)
    response = app.test_client().get("/v1/me", headers=auth_header(token))
    assert response.status_code == 200
    function_name, forwarded_token, parameters = data_api.calls[1]
    assert function_name == "ensure_user_profile"
    assert forwarded_token == token
    assert parameters is None
