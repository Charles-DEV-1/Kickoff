from copy import deepcopy
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
        if kid != "stage4-test" or algorithm != "ES256":
            raise jwt.InvalidTokenError
        return self.key


class RewardAPI:
    """Small RPC model for route, idempotency, and state-machine tests."""

    def __init__(self):
        self.steps = {}
        self.rewards = {}
        self.events = {}
        self.idempotency = {}
        self.fail_action = False

    @staticmethod
    def user(token):
        return jwt.decode(token, options={"verify_signature": False})["sub"]

    def get_authenticated_user(self, token):
        return {"id": self.user(token)}

    def rpc(self, name, token, parameters=None):
        caller = self.user(token)
        p = parameters or {}
        if name == "ensure_user_profile":
            return {"id": caller, "timezone": "Africa/Lagos", "deleted_at": None,
                    "tokens_valid_after": None}
        if name == "consume_api_rate_limit":
            return {"user_id": caller, "allowed": True, "remaining": p["p_limit"],
                    "retry_after_seconds": 60}
        if name == "begin_api_idempotency":
            key = (caller, p["p_key"])
            old = self.idempotency.get(key)
            if old is None:
                lease = str(uuid4())
                self.idempotency[key] = {"hash": p["p_request_hash"], "lease": lease, "state": "in_progress"}
                return {"state": "new", "lease_token": lease}
            if old["hash"] != p["p_request_hash"]:
                return {"state": "key_reused"}
            if old["state"] == "completed":
                return {"state": "replay", "response_status": old["status"], "response_body": old["body"]}
            return {"state": "in_progress"}
        if name == "complete_api_idempotency":
            row = self.idempotency[(caller, p["p_key"])]
            row.update(state="completed", status=p["p_response_status"], body=p["p_response_body"])
            return {"stored": True}
        if name == "release_api_idempotency":
            self.idempotency.pop((caller, p["p_key"]), None)
            return {"released": True}
        if name == "act_on_my_step":
            step = self.steps.get((caller, p["p_step_id"]))
            if step is None:
                return {"state": "not_found"}
            before_step = deepcopy(step)
            before_reward = deepcopy(self.rewards.get(caller))
            before_events = deepcopy(self.events.get(caller))
            if step["version"] != p["p_expected_version"]:
                return {"state": "version_conflict", "current": deepcopy(step)}
            old = deepcopy(step)
            action = p["p_action"]
            if action == "start" and step["status"] == "pending":
                step.update(status="active", started_at=_now(), completed_at=None, actual_minutes=None)
            elif action == "complete" and step["status"] in {"pending", "active"}:
                started = step["started_at"] or _now()
                step.update(status="completed", started_at=started, completed_at=_now(),
                            actual_minutes=p["p_actual_minutes"] or 0)
            elif action == "reopen" and step["status"] == "completed":
                step.update(status="active", completed_at=None, actual_minutes=None)
            elif action == "reopen" and step["status"] == "active":
                step.update(status="pending", started_at=None, completed_at=None, actual_minutes=None)
            else:
                return {"state": "invalid_transition", "current": old}
            step["version"] += 1
            step["updated_at"] = _now()
            events = self.events.setdefault(caller, set())
            points = 0
            if action == "start" or (action == "complete" and old["status"] == "pending"):
                key = "step_started:" + step["id"]
                if key not in events:
                    events.add(key)
                    points += 1
            if action == "complete":
                key = "step_completed:" + step["id"]
                if key not in events:
                    events.add(key)
                    points += 10
            state = self.rewards.setdefault(caller, {"points_total": 0, "plant_stage": 0,
                "current_streak": 0, "best_streak": 0, "freezes_held": 0,
                "streak_status": "none", "last_active_local_date": None})
            state["points_total"] += points
            related = [row for (owner, _), row in self.steps.items()
                       if owner == caller and row["task_id"] == step["task_id"]]
            task_status = "completed" if related and all(row["status"] == "completed" for row in related) else "active"
            result = {"step": deepcopy(step), "task": {"id": step["task_id"], "status": task_status},
                      "reward_delta": {**deepcopy(state), "points_awarded": points},
                      "reward_state": deepcopy(state)}
            if self.fail_action:
                self.steps[(caller, step["id"])] = before_step
                if before_reward is None:
                    self.rewards.pop(caller, None)
                else:
                    self.rewards[caller] = before_reward
                if before_events is None:
                    self.events.pop(caller, None)
                else:
                    self.events[caller] = before_events
                raise RuntimeError("simulated transaction failure")
            idem_key = (caller, p["p_idempotency_key"])
            self.idempotency[idem_key].update(
                state="completed", status=200, body=deepcopy(result)
            )
            return result
        raise AssertionError(name)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def action_setup():
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_id = str(uuid4())
    session_id = str(uuid4())
    api = RewardAPI()
    app = create_app({
        "TESTING": True,
        "PROPAGATE_EXCEPTIONS": False,
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_JWKS_URL": "https://test-project.supabase.co/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": "https://test-project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "SUPABASE_JWT_ALGORITHMS": ("ES256",),
        "SUPABASE_JWT_LEEWAY_SECONDS": 0,
        "SUPPORTED_LANGUAGES": ("en",),
        "ALLOWED_ORIGINS": (),
        "JWT_KEY_PROVIDER": StaticKeyProvider(private_key.public_key()),
        "SUPABASE_DATA_API": api,
    })
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "sub": user_id, "session_id": session_id, "role": "authenticated",
        "iss": "https://test-project.supabase.co/auth/v1", "aud": "authenticated",
        "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp()),
    }, private_key, algorithm="ES256", headers={"kid": "stage4-test"})
    step_id, task_id = str(uuid4()), str(uuid4())
    api.steps[(user_id, step_id)] = {
        "id": step_id, "task_id": task_id, "status": "pending", "version": 1,
        "started_at": None, "completed_at": None, "actual_minutes": None,
    }
    return app, api, user_id, token, step_id


def _headers(token, key):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


@pytest.mark.parametrize("action,initial,expected", [
    ("start", "pending", 200), ("start", "active", 422),
    ("complete", "pending", 200), ("complete", "active", 200),
    ("complete", "completed", 422), ("reopen", "completed", 200),
    ("reopen", "active", 200), ("reopen", "pending", 422),
])
def test_step_action_transition_matrix(action_setup, action, initial, expected):
    app, api, user_id, token, step_id = action_setup
    api.steps[(user_id, step_id)]["status"] = initial
    if initial in {"active", "completed"}:
        api.steps[(user_id, step_id)]["started_at"] = _now()
    if initial == "completed":
        api.steps[(user_id, step_id)]["completed_at"] = _now()
    response = app.test_client().post(
        f"/v1/steps/{step_id}/{action}", json={"version": 1},
        headers=_headers(token, f"transition-{action}-{initial}"),
    )
    assert response.status_code == expected
    if expected == 422:
        assert response.get_json()["error"]["code"] == "invalid_transition"
    else:
        assert response.get_json()["reward_delta"]["points_awarded"] >= 0


def test_completion_accepts_never_started_and_returns_reward_delta(action_setup):
    app, _api, _user_id, token, step_id = action_setup
    response = app.test_client().post(
        f"/v1/steps/{step_id}/complete", json={"version": 1, "actual_minutes": 0},
        headers=_headers(token, "complete-never-started-001"),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["step"]["status"] == "completed"
    assert body["task"]["status"] == "completed"
    assert body["reward_delta"]["points_awarded"] == 11
    assert "points_total" in body["reward_delta"]


def test_same_key_replays_original_action_and_distinct_reopen_does_not_reaward(action_setup):
    app, api, user_id, token, step_id = action_setup
    client = app.test_client()
    first = client.post(f"/v1/steps/{step_id}/start", json={"version": 1},
                        headers=_headers(token, "start-step-repeat-001"))
    replay = client.post(f"/v1/steps/{step_id}/start", json={"version": 1},
                         headers=_headers(token, "start-step-repeat-001"))
    assert first.status_code == replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert len(api.events[user_id]) == 1

    completed = client.post(f"/v1/steps/{step_id}/complete", json={"version": 2},
                            headers=_headers(token, "complete-step-repeat-01"))
    assert completed.status_code == 200
    reopened = client.post(f"/v1/steps/{step_id}/reopen", json={"version": 3},
                           headers=_headers(token, "reopen-step-repeat-001"))
    assert reopened.status_code == 200
    assert reopened.get_json()["task"]["status"] == "active"
    assert reopened.get_json()["reward_delta"]["points_awarded"] == 0
    assert api.rewards[user_id]["points_total"] == 11


@pytest.mark.parametrize("body", [
    {"version": 0}, {"version": 1, "actual_minutes": -1},
    {"version": 1, "actual_minutes": 601}, {"version": 1, "started_at": "2026-01-01T00:00:00Z"},
])
def test_action_payload_validation(action_setup, body):
    app, _api, _user_id, token, step_id = action_setup
    response = app.test_client().post(
        f"/v1/steps/{step_id}/complete", json=body,
        headers=_headers(token, "validate-step-action-001"),
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"


def test_action_failure_returns_safe_5xx_and_does_not_expose_exception(action_setup):
    app, api, user_id, token, step_id = action_setup
    api.fail_action = True
    response = app.test_client().post(
        f"/v1/steps/{step_id}/start", json={"version": 1},
        headers=_headers(token, "transaction-rollback-001"),
    )
    assert response.status_code == 500
    assert "simulated transaction failure" not in response.get_data(as_text=True)
    assert api.steps[(user_id, step_id)]["status"] == "pending"
    assert user_id not in api.rewards
    assert user_id not in api.events


def test_step_action_uses_optimistic_version_and_returns_current_version(action_setup):
    app, api, user_id, token, step_id = action_setup
    api.steps[(user_id, step_id)]["version"] = 2
    response = app.test_client().post(
        f"/v1/steps/{step_id}/start", json={"version": 1},
        headers=_headers(token, "stale-action-version-01"),
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "version_conflict"
    assert api.steps[(user_id, step_id)]["status"] == "pending"


def test_step_action_cannot_access_another_users_step(action_setup):
    app, api, _user_id, token, _step_id = action_setup
    foreign_step_id = str(uuid4())
    foreign_user_id = str(uuid4())
    api.steps[(foreign_user_id, foreign_step_id)] = {
        "id": foreign_step_id, "task_id": str(uuid4()), "status": "pending", "version": 1,
        "started_at": None, "completed_at": None, "actual_minutes": None,
    }
    response = app.test_client().post(
        f"/v1/steps/{foreign_step_id}/start", json={"version": 1},
        headers=_headers(token, "foreign-step-action-001"),
    )
    assert response.status_code == 404
    assert api.steps[(foreign_user_id, foreign_step_id)]["status"] == "pending"


def test_rebuild_function_is_user_scoped_and_event_log_is_append_only_in_migration():
    from pathlib import Path
    sql = Path(__file__).parents[1].joinpath(
        "supabase", "migrations", "202611020004_step_actions_rewards.sql"
    ).read_text(encoding="utf-8")
    base_sql = Path(__file__).parents[1].joinpath(
        "supabase", "migrations", "202611020002_online_mvp_schema.sql"
    ).read_text(encoding="utf-8")
    assert "security invoker" in sql.lower()
    assert "auth.uid()" in sql
    assert "rebuild_my_reward_state" in sql
    assert "dedupe_key" in sql
    assert "returning" in sql.lower()
    assert "reward_events_append_only" in base_sql.lower()


def test_timezone_day_boundary_and_streak_freeze_use_and_expiry():
    from datetime import date
    from app.modules.rewards.rules import advance_streak, local_date_for

    instant = datetime.fromisoformat("2026-01-01T23:30:00+00:00")
    assert local_date_for(instant, "Africa/Lagos") == date(2026, 1, 2)
    base = {
        "points_total": 0, "plant_stage": 0, "current_streak": 5,
        "best_streak": 5, "freezes_held": 1, "streak_status": "active",
        "last_active_local_date": date(2026, 1, 1),
    }
    survived, events = advance_streak(base, date(2026, 1, 3))
    assert survived["streak_status"] == "active"
    assert survived["current_streak"] == 6
    assert survived["freezes_held"] == 0
    assert events[0] == {"type": "freeze_used", "local_date": date(2026, 1, 2)}

    ended, events = advance_streak(base, date(2026, 1, 4))
    assert ended["current_streak"] == 1
    assert ended["best_streak"] == 5
    assert any(event["type"] == "streak_ended" for event in events)


def test_freeze_earning_is_capped_and_can_be_earned_on_seven_day_streak():
    from datetime import date
    from app.modules.rewards.rules import advance_streak

    state = {
        "points_total": 0, "plant_stage": 0, "current_streak": 6,
        "best_streak": 6, "freezes_held": 2, "streak_status": "active",
        "last_active_local_date": date(2026, 2, 6),
    }
    earned, events = advance_streak(state, date(2026, 2, 7))
    assert earned["current_streak"] == 7
    assert earned["freezes_held"] == 3
    assert events == [{"type": "freeze_earned", "local_date": date(2026, 2, 7)}]
    capped, capped_events = advance_streak(earned, date(2026, 2, 8))
    assert capped["freezes_held"] == 3
    assert not any(event["type"] == "freeze_earned" for event in capped_events)


def test_plant_stage_configuration_is_threshold_driven():
    from app.modules.rewards.rules import plant_stage_for

    thresholds = [0, 25, 75, 150]
    assert plant_stage_for(0, thresholds) == 0
    assert plant_stage_for(74, thresholds) == 1
    assert plant_stage_for(75, thresholds) == 2


def test_reward_state_reconstruction_uses_latest_append_only_event_snapshot():
    from app.modules.rewards.rules import rebuild_state_from_events

    state = {
        "points_total": 11, "plant_stage": 0, "current_streak": 3,
        "best_streak": 3, "freezes_held": 1, "streak_status": "active",
        "last_active_local_date": "2026-03-03",
    }
    event = {
        "event_order": 2,
        "event_data": {
            "points_after": 11, "plant_stage_after": 0,
            "current_streak_after": 3, "best_streak_after": 3,
            "freezes_after": 1, "streak_status_after": "active",
            "last_active_local_date_after": "2026-03-03",
        },
    }
    assert rebuild_state_from_events([{"event_order": 1, "event_data": {}}, event]) == state
    assert rebuild_state_from_events([])["points_total"] == 0
