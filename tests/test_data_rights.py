from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import create_app
from app.common.supabase_data import SupabaseAuthAPIError, SupabaseAuthAdminError


class StaticKeyProvider:
    def __init__(self, key):
        self.key = key

    def get_public_key(self, kid, algorithm):
        if kid != "rights-key" or algorithm != "ES256":
            raise jwt.InvalidTokenError
        return self.key


class RightsDataAPI:
    def __init__(self, user_id):
        self.user_id = user_id
        self.profile = {
            "id": user_id, "display_name": "Sam", "language": "en",
            "timezone": "Africa/Lagos", "settings": {"calm_mode": True},
            "created_at": "2026-11-01T00:00:00+00:00",
            "updated_at": "2026-11-01T00:00:00+00:00",
            "deleted_at": None, "tokens_valid_after": None,
        }
        self.data_export = {
            "export_version": 1,
            "exported_at": "2026-11-03T00:00:00+00:00",
            "profile": {"id": user_id},
            "consents": [{"consent_type": "ai_processing"}],
            "tasks": [{"id": str(uuid4()), "title": "private task"}],
            "steps": [{"id": str(uuid4()), "text": "private step"}],
            "reward_events": [{"points_delta": 10}],
            "reward_state": {"points_total": 10},
            "ai_breakdowns": [{"prompt_version": "v1", "result": {"steps": []}}],
            "ai_usage": [{"generations": 1}],
        }
        self.deletion = None
        self.rate_counts = {}
        self.idempotency = {}
        self.export_calls = 0
        self.rpc_calls = []
        self.server_calls = []
        self.auth_delete_calls = []
        self.fail_auth_delete = True
        self.purge_runs = 0

    def get_authenticated_user(self, token):
        if self.profile["deleted_at"]:
            raise SupabaseAuthAPIError(status=403)
        return {"id": self.user_id, "email": "sam@example.test"}

    def rpc(self, name, token, parameters=None, **kwargs):
        self.rpc_calls.append((name, parameters, kwargs))
        if name == "ensure_user_profile":
            return deepcopy(self.profile)
        if name == "get_my_account_deletion_status":
            return {"status": self.deletion["status"]} if self.deletion else None
        if name == "consume_api_rate_limit":
            bucket = parameters["p_bucket"]
            count = self.rate_counts.get(bucket, 0) + 1
            self.rate_counts[bucket] = count
            limit = parameters["p_limit"]
            return {
                "user_id": self.user_id,
                "allowed": count <= limit,
                "remaining": max(0, limit - count),
                "retry_after_seconds": parameters["p_window_seconds"],
            }
        if name == "begin_api_idempotency":
            key = parameters["p_key"]
            request_hash = parameters["p_request_hash"]
            existing = self.idempotency.get(key)
            if existing and existing["hash"] != request_hash:
                return {"state": "key_reused"}
            if existing and existing["state"] == "completed":
                return {"state": "replay", "response_status": existing["status"], "response_body": existing["body"]}
            if existing:
                return {"state": "in_progress"}
            lease = str(uuid4())
            self.idempotency[key] = {"hash": request_hash, "lease": lease, "state": "in_progress"}
            return {"state": "new", "lease_token": lease}
        if name == "complete_api_idempotency":
            entry = self.idempotency[parameters["p_key"]]
            assert entry["lease"] == parameters["p_lease_token"]
            entry.update({"state": "completed", "status": parameters["p_response_status"], "body": parameters["p_response_body"]})
            return {"stored": True}
        if name == "release_api_idempotency":
            self.idempotency.pop(parameters["p_key"], None)
            return {"released": True}
        if name == "export_my_data":
            self.export_calls += 1
            return deepcopy(self.data_export)
        if name == "request_my_account_deletion":
            assert parameters == {"p_confirmation": "DELETE"}
            instant = datetime.now(timezone.utc)
            self.profile["deleted_at"] = instant.isoformat()
            self.deletion = {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "requested_at": instant.isoformat(),
                "purge_after": (instant + timedelta(days=30)).isoformat(),
                "status": "pending",
                "soft_deleted": False,
                "app_data_purged": False,
                "hard_deleted": False,
            }
            return {key: self.deletion[key] for key in ("id", "requested_at", "purge_after", "status")}
        raise AssertionError(f"Unexpected user RPC: {name}")

    def delete_auth_user(self, user_id, secret, *, should_soft_delete):
        self.auth_delete_calls.append((user_id, secret, should_soft_delete))
        if self.fail_auth_delete:
            raise SupabaseAuthAdminError(status=503)

    def rpc_server(self, name, secret, parameters=None):
        self.server_calls.append((name, parameters))
        if name == "record_account_auth_delete_attempt":
            assert self.deletion and parameters["p_deletion_id"] == self.deletion["id"]
            if parameters["p_phase"] == "soft":
                self.deletion["soft_deleted"] = parameters["p_succeeded"]
            elif parameters["p_succeeded"]:
                self.deletion["hard_deleted"] = True
                self.deletion["status"] = "purged"
            return {"updated": True}
        if name == "list_pending_auth_soft_deletions":
            if self.deletion and not self.deletion["soft_deleted"]:
                return [{"id": self.deletion["id"], "user_id": self.user_id}]
            return []
        if name == "run_data_retention_purge":
            self.purge_runs += 1
            if self.deletion and self.deletion["purge_after"] <= datetime.now(timezone.utc).isoformat():
                self.deletion["app_data_purged"] = True
            return {"accounts_data_purged": int(bool(self.deletion and self.deletion["app_data_purged"]))}
        if name == "list_pending_auth_hard_deletions":
            if self.deletion and self.deletion["app_data_purged"] and not self.deletion["hard_deleted"]:
                return [{"id": self.deletion["id"], "user_id": self.user_id}]
            return []
        raise AssertionError(f"Unexpected server RPC: {name}")


@pytest.fixture
def setup_rights():
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_id = str(uuid4())
    session_id = str(uuid4())
    data_api = RightsDataAPI(user_id)
    app = create_app({
        "TESTING": True,
        "SUPABASE_URL": "https://rights-project.supabase.co",
        "SUPABASE_PUBLISHABLE_KEY": "test-publishable",
        "SUPABASE_SECRET_KEY": "server-secret-test",
        "CRON_SECRET": "cron-test-secret",
        "SUPABASE_JWT_ISSUER": "https://rights-project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_JWT_ALGORITHMS": ("ES256",),
        "SUPABASE_JWT_LEEWAY_SECONDS": 0,
        "JWT_KEY_PROVIDER": StaticKeyProvider(private_key.public_key()),
        "SUPABASE_DATA_API": data_api,
    })
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "sub": user_id, "session_id": session_id, "role": "authenticated",
        "iss": "https://rights-project.supabase.co/auth/v1",
        "aud": "authenticated", "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }, private_key, algorithm="ES256", headers={"kid": "rights-key"})
    return app, data_api, user_id, {"Authorization": f"Bearer {token}"}


def test_export_is_complete_bounded_private_download_and_never_logged(setup_rights, caplog):
    app, data_api, user_id, headers = setup_rights
    response = app.test_client().get("/v1/me/export", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "export_version", "exported_at", "account", "profile", "consents",
        "tasks", "steps", "reward_events", "reward_state", "ai_breakdowns", "ai_usage",
    }
    assert payload["account"] == {"id": user_id, "email": "sam@example.test"}
    assert payload["tasks"] == data_api.data_export["tasks"]
    assert payload["reward_events"] == data_api.data_export["reward_events"]
    assert payload["ai_breakdowns"] == data_api.data_export["ai_breakdowns"]
    assert response.headers["Content-Disposition"] == 'attachment; filename="kickoff-data-export.json"'
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert data_api.rpc_calls[-1][0] == "export_my_data"
    assert data_api.rpc_calls[-1][2]["max_response_bytes"] == 8 * 1024 * 1024
    captured = caplog.text
    assert "private task" not in captured
    assert "private step" not in captured
    assert "sam@example.test" not in captured


def test_export_limit_is_three_per_day(setup_rights):
    app, data_api, _, headers = setup_rights
    client = app.test_client()
    for _ in range(3):
        assert client.get("/v1/me/export", headers=headers).status_code == 200
    response = client.get("/v1/me/export", headers=headers)
    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "rate_limited"
    assert data_api.export_calls == 3


@pytest.mark.parametrize("payload", [{}, {"confirm": "delete"}, {"confirm": "DELETE", "other": True}])
def test_deletion_requires_exact_confirmation(setup_rights, payload):
    app, data_api, _, headers = setup_rights
    response = app.test_client().delete(
        "/v1/me", headers={**headers, "Idempotency-Key": "delete-confirmation-1"}, json=payload
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"
    assert data_api.deletion is None


def test_delete_locks_account_before_auth_delete_and_failure_is_retryable(setup_rights):
    app, data_api, _, headers = setup_rights
    response = app.test_client().delete(
        "/v1/me", headers={**headers, "Idempotency-Key": "delete-account-001"},
        json={"confirm": "DELETE"},
    )
    assert response.status_code == 202
    assert response.get_json()["auth_deletion_status"] == "pending_retry"
    assert data_api.profile["deleted_at"] is not None
    assert data_api.deletion["status"] == "pending"
    assert data_api.server_calls[-1][0] == "record_account_auth_delete_attempt"
    locked = app.test_client().get("/v1/me", headers=headers)
    assert locked.status_code == 401
    assert locked.get_json()["error"]["code"] == "account_deleted"

    data_api.fail_auth_delete = False
    job = app.test_client().get(
        "/internal/jobs/purge", headers={"Authorization": "Bearer cron-test-secret"}
    )
    assert job.status_code == 200
    assert job.get_json()["soft_auth_deletes_succeeded"] == 1
    assert data_api.deletion["soft_deleted"] is True
    assert data_api.auth_delete_calls[-1] == (data_api.user_id, "server-secret-test", True)


def test_retention_job_is_protected_and_repeat_safe(setup_rights):
    app, data_api, _, headers = setup_rights
    client = app.test_client()
    assert client.get("/internal/jobs/purge").status_code == 401
    assert client.get(
        "/internal/jobs/purge", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401

    data_api.fail_auth_delete = False
    deletion_response = client.delete(
        "/v1/me", headers={**headers, "Idempotency-Key": "delete-account-002"},
        json={"confirm": "DELETE"},
    )
    assert deletion_response.status_code == 202
    data_api.deletion["purge_after"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    first = client.get(
        "/internal/jobs/purge", headers={"Authorization": "Bearer cron-test-secret"}
    )
    second = client.get(
        "/internal/jobs/purge", headers={"Authorization": "Bearer cron-test-secret"}
    )
    assert first.status_code == second.status_code == 200
    assert data_api.purge_runs == 2
    assert [name for name, _ in data_api.server_calls].count("list_pending_auth_soft_deletions") == 2
    assert data_api.deletion["app_data_purged"] is True
    assert data_api.deletion["hard_deleted"] is True
    assert data_api.deletion["status"] == "purged"
    assert len([call for call in data_api.auth_delete_calls if call[2] is False]) == 1
