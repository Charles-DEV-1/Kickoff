from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import json
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import create_app
from app.common.validation import RequestValidationError
from app.modules.tasks.schemas import validate_task_create, validate_task_patch


class StaticKeyProvider:
    def __init__(self, key):
        self.key = key

    def get_public_key(self, kid, algorithm):
        if kid != "tasks-test-key" or algorithm != "ES256":
            raise jwt.InvalidTokenError
        return self.key


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TaskDataAPI:
    """Stateful RPC fake; each RPC scopes its records to the token subject."""

    def __init__(self):
        self.tasks = {}
        self.profiles = {}
        self.rate_counts = {}
        self.idempotency = {}
        self.calls = []

    @staticmethod
    def user_id(token):
        return jwt.decode(token, options={"verify_signature": False})["sub"]

    def get_authenticated_user(self, token):
        return {"id": self.user_id(token)}

    def rpc(self, function_name, token, parameters=None):
        caller = self.user_id(token)
        parameters = parameters or {}
        self.calls.append((function_name, caller, deepcopy(parameters)))

        if function_name == "ensure_user_profile":
            return self.profiles.setdefault(caller, {
                "id": caller,
                "display_name": None,
                "language": "en",
                "timezone": "Africa/Lagos",
                "settings": {},
                "created_at": _now(),
                "updated_at": _now(),
                "deleted_at": None,
                "tokens_valid_after": None,
            })
        if function_name == "get_my_bootstrap":
            profile = self.profiles.setdefault(caller, {
                "id": caller, "display_name": None, "language": "en",
                "timezone": "Africa/Lagos", "settings": {},
                "created_at": _now(), "updated_at": _now(),
            })
            owned = [task for task in self.tasks.get(caller, {}).values()
                     if task["deleted_at"] is None and task["status"] == "active"]
            owned.sort(key=lambda task: (task["updated_at"], task["id"]), reverse=True)
            return {
                "profile": deepcopy(profile),
                "reward_state": {"points_total": 0, "plant_stage": 0, "current_streak": 0,
                                 "best_streak": 0, "freezes_held": 0, "streak_status": "none"},
                "tasks": deepcopy(owned[:50]), "active_task_count": len(owned), "server_time": _now(),
            }
        if function_name == "list_my_task_delta":
            now = datetime.now(timezone.utc)
            since = datetime.fromisoformat(parameters["p_updated_since"].replace("Z", "+00:00"))
            server_time = now.isoformat().replace("+00:00", "Z")
            snapshot = parameters.get("p_snapshot_at") or server_time
            snapshot_dt = datetime.fromisoformat(snapshot.replace("Z", "+00:00"))
            if since < now - timedelta(days=30):
                return {"items": [], "has_more": False, "full_refresh_required": True,
                        "server_time": server_time, "next_since": None}
            changed = [task for task in self.tasks.get(caller, {}).values()
                       if since <= datetime.fromisoformat(task["updated_at"].replace("Z", "+00:00")) <= snapshot_dt]
            status = parameters["p_status"]
            due_date = parameters["p_due_date"]
            changed.sort(key=lambda task: (task["updated_at"], task["id"]), reverse=True)
            cursor_time, cursor_id = parameters["p_cursor_updated_at"], parameters["p_cursor_id"]
            if cursor_time and cursor_id:
                changed = [task for task in changed if (task["updated_at"], task["id"]) < (cursor_time, cursor_id)]
            limit = parameters["p_limit"]
            page = deepcopy(changed[:limit])
            for task in page:
                task["matches_filter"] = (task["deleted_at"] is None
                    and (status == "all" or task["status"] == status)
                    and (due_date is None or (task["due_at"] and task["due_at"][:10] == due_date)))
            return {"items": page, "has_more": len(changed) > limit,
                    "full_refresh_required": False, "server_time": server_time,
                    "snapshot_at": snapshot, "next_since": snapshot}
        if function_name == "consume_api_rate_limit":
            bucket = parameters["p_bucket"]
            key = (caller, bucket)
            count = self.rate_counts.get(key, 0) + 1
            self.rate_counts[key] = count
            limit = parameters["p_limit"]
            return {
                "user_id": caller,
                "allowed": count <= limit,
                "remaining": max(0, limit - count),
                "retry_after_seconds": parameters["p_window_seconds"],
            }
        if function_name == "begin_api_idempotency":
            key = (caller, parameters["p_key"])
            row = self.idempotency.get(key)
            request_hash = parameters["p_request_hash"]
            if row is None:
                lease = str(uuid4())
                self.idempotency[key] = {"hash": request_hash, "lease": lease, "state": "in_progress"}
                return {"state": "new", "lease_token": lease}
            if row["hash"] != request_hash:
                return {"state": "key_reused"}
            if row["state"] == "completed":
                return {"state": "replay", "response_status": row["status"], "response_body": row["body"]}
            return {"state": "in_progress"}
        if function_name == "complete_api_idempotency":
            row = self.idempotency[(caller, parameters["p_key"])]
            assert row["lease"] == parameters["p_lease_token"]
            row.update({"state": "completed", "status": parameters["p_response_status"], "body": parameters["p_response_body"]})
            return {"stored": True}
        if function_name == "release_api_idempotency":
            self.idempotency.pop((caller, parameters["p_key"]), None)
            return {"released": True}

        if function_name == "create_my_task":
            owned = self.tasks.setdefault(caller, {})
            active = sum(task["status"] == "active" and task["deleted_at"] is None for task in owned.values())
            if active >= 500:
                return {"state": "task_limit"}
            spec = parameters["p_task"]
            steps = parameters["p_steps"]
            task_id = str(uuid4())
            instant = _now()
            task = {
                "id": task_id, **spec, "status": "active", "version": 1,
                "created_at": instant, "updated_at": instant, "deleted_at": None,
                "steps": [],
            }
            for position, step_spec in enumerate(steps):
                task["steps"].append(self._new_step(task_id, step_spec, position, instant))
            owned[task_id] = task
            return deepcopy(task)

        if function_name == "list_my_tasks":
            tasks = [task for task in self.tasks.get(caller, {}).values() if task["deleted_at"] is None]
            status = parameters["p_status"]
            if status != "all":
                tasks = [task for task in tasks if task["status"] == status]
            due_date = parameters["p_due_date"]
            if due_date:
                tasks = [task for task in tasks if task["due_at"] and task["due_at"][:10] == due_date]
            updated_since = parameters["p_updated_since"]
            if updated_since:
                tasks = [task for task in tasks if task["updated_at"] >= updated_since]
            cursor = parameters["p_cursor_updated_at"], parameters["p_cursor_id"]
            if all(cursor):
                tasks = [task for task in tasks if (task["updated_at"], task["id"]) < cursor]
            tasks.sort(key=lambda task: (task["updated_at"], task["id"]), reverse=True)
            limit = parameters["p_limit"]
            return {"items": deepcopy(tasks[:limit]), "has_more": len(tasks) > limit}

        if function_name == "get_my_task":
            task = self.tasks.get(caller, {}).get(parameters["p_task_id"])
            return deepcopy(task) if task and task["deleted_at"] is None else None

        if function_name == "update_my_task":
            task = self._find_task(caller, parameters["p_task_id"])
            if task is None:
                return {"state": "not_found"}
            if task["version"] != parameters["p_expected_version"]:
                return {"state": "version_conflict", "current": deepcopy(task)}
            task.update(parameters["p_patch"])
            task["version"] += 1
            task["updated_at"] = _now()
            return deepcopy(task)

        if function_name == "soft_delete_my_task":
            task = self._find_task(caller, parameters["p_task_id"])
            if task is None:
                return {"state": "not_found"}
            if task["version"] != parameters["p_expected_version"]:
                return {"state": "version_conflict", "current": deepcopy(task)}
            instant = _now()
            task["deleted_at"] = instant
            task["version"] += 1
            task["updated_at"] = instant
            for step in task["steps"]:
                if step["deleted_at"] is None:
                    step["deleted_at"] = instant
                    step["version"] += 1
                    step["updated_at"] = instant
            return {"deleted": True, "id": task["id"], "version": task["version"], "deleted_at": instant}

        if function_name == "create_my_step":
            task = self._find_task(caller, parameters["p_task_id"])
            if task is None:
                return {"state": "not_found"}
            steps = [step for step in task["steps"] if step["deleted_at"] is None]
            if len(steps) >= 30:
                return {"state": "step_limit"}
            used = {step["position"] for step in steps}
            position = next(pos for pos in range(30) if pos not in used)
            step = self._new_step(task["id"], parameters["p_step"], position, _now())
            task["steps"].append(step)
            task["version"] += 1
            task["updated_at"] = _now()
            return {"step": deepcopy(step), "task_version": task["version"]}

        if function_name in {"update_my_step", "soft_delete_my_step"}:
            step, task = self._find_step(caller, parameters["p_step_id"])
            if step is None:
                return {"state": "not_found"}
            if step["version"] != parameters["p_expected_version"]:
                return {"state": "version_conflict", "current": deepcopy(step)}
            instant = _now()
            if function_name == "update_my_step":
                step.update(parameters["p_patch"])
                step["version"] += 1
                step["updated_at"] = instant
                task["version"] += 1
                task["updated_at"] = instant
                return {"step": deepcopy(step), "task_version": task["version"]}
            step["deleted_at"] = instant
            step["version"] += 1
            step["updated_at"] = instant
            task["version"] += 1
            task["updated_at"] = instant
            return {"deleted": True, "id": step["id"], "version": step["version"], "deleted_at": instant}

        if function_name == "reorder_my_steps":
            task = self._find_task(caller, parameters["p_task_id"])
            if task is None:
                return {"state": "not_found"}
            if task["version"] != parameters["p_expected_version"]:
                return {"state": "version_conflict", "current": deepcopy(task)}
            active = [step for step in task["steps"] if step["deleted_at"] is None]
            ids = parameters["p_step_ids"]
            if len(ids) != len(active) or set(ids) != {step["id"] for step in active}:
                return {"state": "invalid_order"}
            by_id = {step["id"]: step for step in active}
            instant = _now()
            for position, step_id in enumerate(ids):
                by_id[step_id]["position"] = position
                by_id[step_id]["version"] += 1
                by_id[step_id]["updated_at"] = instant
            task["version"] += 1
            task["updated_at"] = instant
            task["steps"].sort(key=lambda step: (step["position"], step["id"]))
            return deepcopy(task)

        raise AssertionError(f"Unexpected RPC {function_name}")

    @staticmethod
    def _new_step(task_id, spec, position, instant):
        return {
            "id": str(uuid4()), "task_id": task_id, **spec,
            "actual_minutes": None, "position": position, "status": "pending",
            "started_at": None, "completed_at": None, "version": 1,
            "created_at": instant, "updated_at": instant, "deleted_at": None,
        }

    def _find_task(self, caller, task_id):
        task = self.tasks.get(caller, {}).get(task_id)
        return task if task and task["deleted_at"] is None else None

    def _find_step(self, caller, step_id):
        for task in self.tasks.get(caller, {}).values():
            for step in task["steps"]:
                if step["id"] == step_id and step["deleted_at"] is None and task["deleted_at"] is None:
                    return step, task
        return None, None

    def select_public_templates(self):
        return [{"id": "template-generic-en", "template_key": "generic", "task_type": "generic",
                 "language": "en", "version": 1, "is_active": True}]


@pytest.fixture
def task_setup():
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_a = str(uuid4())
    session_a = str(uuid4())
    user_b = str(uuid4())
    session_b = str(uuid4())
    api = TaskDataAPI()
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
    def token(user_id, session_id):
        now = datetime.now(timezone.utc)
        return jwt.encode({
            "sub": user_id,
            "session_id": session_id,
            "role": "authenticated",
            "iss": "https://test-project.supabase.co/auth/v1",
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        }, private_key, algorithm="ES256", headers={"kid": "tasks-test-key"})
    return app, api, user_a, token(user_a, session_a), user_b, token(user_b, session_b)


def _headers(token, key=None):
    result = {"Authorization": f"Bearer {token}"}
    if key:
        result["Idempotency-Key"] = key
    return result


def _task_body(title="Study", steps=None):
    return {"title": title, "notes": "", "steps": steps or []}


def _create(client, token, title="Study", steps=None, key="create-task-key-001"):
    return client.post(
        "/v1/tasks",
        headers=_headers(token, key),
        json=_task_body(title, steps),
    )


def test_task_create_is_atomic_and_returns_server_ids(task_setup):
    app, api, user_id, token, *_ = task_setup
    response = _create(app.test_client(), token, steps=[
        {"text": "Read chapter", "planned_minutes": 20},
        {"text": "Write notes", "planned_minutes": 15},
    ])
    assert response.status_code == 201
    task = response.get_json()
    assert task["id"]
    assert len(task["steps"]) == 2
    assert task["steps"][0]["position"] == 0
    assert task["steps"][1]["position"] == 1
    assert set(api.tasks[user_id]) == {task["id"]}
    call = next(call for call in api.calls if call[0] == "create_my_task")
    assert "user_id" not in call[2]
    assert call[2]["p_task"]["title"] == "Study"
    assert len(call[2]["p_steps"]) == 2
    assert sum(item[0] == "create_my_task" for item in api.calls) == 1


@pytest.mark.parametrize("length", [1, 200])
def test_task_title_boundaries_are_accepted(length):
    result = validate_task_create(_task_body("x" * length))
    assert len(result["task"]["title"]) == length


@pytest.mark.parametrize("length", [0, 201])
def test_task_title_out_of_bounds_is_rejected(length):
    with pytest.raises(RequestValidationError):
        validate_task_create(_task_body("x" * length))


def test_task_notes_and_step_limits(task_setup):
    app, *_ = task_setup
    client = app.test_client()
    token = task_setup[3]
    valid_steps = [{"text": "x", "planned_minutes": 1} for _ in range(30)]
    response = client.post(
        "/v1/tasks", headers=_headers(token, "steps-thirty-ok"),
        json={"title": "x", "notes": "n" * 500, "steps": valid_steps},
    )
    assert response.status_code == 201
    too_many = client.post(
        "/v1/tasks", headers=_headers(token, "steps-thirty-one"),
        json={"title": "x", "steps": valid_steps + [{"text": "x", "planned_minutes": 1}]},
    )
    assert too_many.status_code == 422
    too_many_notes = client.post(
        "/v1/tasks", headers=_headers(token, "notes-too-long"),
        json={"title": "x", "notes": "n" * 501},
    )
    assert too_many_notes.status_code == 422

    invalid_steps = client.post(
        "/v1/tasks", headers=_headers(token, "invalid-step-atomic"),
        json={"title": "x", "steps": [
            {"text": "valid", "planned_minutes": 1},
            {"text": "invalid", "planned_minutes": 0},
        ]},
    )
    assert invalid_steps.status_code == 422
    assert sum(call[0] == "create_my_task" for call in task_setup[1].calls) == 1


@pytest.mark.parametrize("minutes", [1, 240])
def test_step_planned_minutes_boundaries(task_setup, minutes):
    app, *_ = task_setup
    response = _create(
        app.test_client(), task_setup[3],
        steps=[{"text": "x" * 280, "planned_minutes": minutes}],
    )
    assert response.status_code == 201


@pytest.mark.parametrize("minutes", [0, 241, True])
def test_step_planned_minutes_invalid(task_setup, minutes):
    app, *_ = task_setup
    response = _create(
        app.test_client(), task_setup[3],
        steps=[{"text": "x", "planned_minutes": minutes}],
    )
    assert response.status_code == 422


def test_active_task_limit_returns_conflict(task_setup):
    app, api, user_id, token, *_ = task_setup
    for index in range(500):
        task_id = str(uuid4())
        api.tasks.setdefault(user_id, {})[task_id] = {
            "id": task_id, "status": "active", "deleted_at": None,
            "version": 1, "updated_at": _now(), "steps": [],
        }
    response = _create(app.test_client(), token)
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "task_limit_reached"
    assert len(api.tasks[user_id]) == 500


def test_list_filters_and_cursor_pagination(task_setup):
    app, _, _, token, *_ = task_setup
    client = app.test_client()
    for index in range(3):
        response = _create(client, token, title=f"Task {index}", key=f"list-task-key-{index}")
        assert response.status_code == 201
    first = client.get("/v1/tasks?status=active&limit=2", headers=_headers(token))
    assert first.status_code == 200
    page = first.get_json()
    assert len(page["items"]) == 2
    assert page["next_cursor"]
    second = client.get(
        f"/v1/tasks?status=active&limit=2&cursor={page['next_cursor']}",
        headers=_headers(token),
    )
    assert second.status_code == 200
    assert len(second.get_json()["items"]) == 1
    assert second.get_json()["next_cursor"] is None


def test_cross_user_guessed_task_id_is_not_found(task_setup):
    app, api, user_a, token_a, user_b, _token_b = task_setup
    task_b = str(uuid4())
    api.tasks.setdefault(user_b, {})[task_b] = {
        "id": task_b, "title": "Private", "status": "active",
        "version": 1, "deleted_at": None, "updated_at": _now(), "steps": [],
    }
    response = app.test_client().get(f"/v1/tasks/{task_b}", headers=_headers(token_a))
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    assert task_b not in api.tasks.get(user_a, {})

    patch = app.test_client().patch(
        f"/v1/tasks/{task_b}",
        headers=_headers(token_a, "foreign-task-patch-key"),
        json={"version": 1, "title": "Try to change"},
    )
    assert patch.status_code == 404
    delete = app.test_client().delete(
        f"/v1/tasks/{task_b}",
        headers=_headers(token_a, "foreign-task-delete-key"),
        json={"version": 1},
    )
    assert delete.status_code == 404
    assert api.tasks[user_b][task_b]["title"] == "Private"

    step_b = api._new_step(
        task_b, {"text": "Other user's step", "planned_minutes": 5}, 0, _now()
    )
    api.tasks[user_b][task_b]["steps"].append(step_b)
    step_patch = app.test_client().patch(
        f"/v1/steps/{step_b['id']}",
        headers=_headers(token_a, "foreign-step-patch-key"),
        json={"version": 1, "text": "Try to change"},
    )
    assert step_patch.status_code == 404
    assert step_b["text"] == "Other user's step"


def test_task_patch_version_conflict_returns_only_own_current_resource(task_setup):
    app, api, user_id, token, *_ = task_setup
    client = app.test_client()
    created = _create(client, token)
    task = created.get_json()
    response = client.patch(
        f"/v1/tasks/{task['id']}",
        headers=_headers(token, "patch-task-version-1"),
        json={"version": 9, "title": "Changed"},
    )
    assert response.status_code == 409
    detail = response.get_json()["error"]["details"][0]
    assert detail["code"] == "stale_version"
    assert detail["current"]["id"] == task["id"]
    assert detail["current"]["title"] == "Study"
    assert api.tasks[user_id][task["id"]]["version"] == 1


def test_task_delete_soft_deletes_task_and_steps(task_setup):
    app, api, user_id, token, *_ = task_setup
    client = app.test_client()
    created = _create(client, token, steps=[{"text": "Read", "planned_minutes": 10}])
    task = created.get_json()
    step_id = task["steps"][0]["id"]
    response = client.delete(
        f"/v1/tasks/{task['id']}",
        headers=_headers(token, "delete-task-with-steps"),
        json={"version": task["version"]},
    )
    assert response.status_code == 200
    stored = api.tasks[user_id][task["id"]]
    assert stored["deleted_at"] is not None
    assert next(step for step in stored["steps"] if step["id"] == step_id)["deleted_at"] is not None
    assert client.get(f"/v1/tasks/{task['id']}", headers=_headers(token)).status_code == 404


def test_task_delete_rejects_stale_version(task_setup):
    app, api, user_id, token, *_ = task_setup
    client = app.test_client()
    task = _create(client, token).get_json()
    response = client.delete(
        f"/v1/tasks/{task['id']}",
        headers=_headers(token, "stale-task-delete-001"),
        json={"version": task["version"] + 1},
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["details"][0]["current"]["version"] == task["version"]
    assert api.tasks[user_id][task["id"]]["deleted_at"] is None


def test_step_add_edit_delete_and_task_versioning(task_setup):
    app, api, user_id, token, *_ = task_setup
    client = app.test_client()
    task = _create(client, token).get_json()
    created = client.post(
        f"/v1/tasks/{task['id']}/steps",
        headers=_headers(token, "add-step-test-001"),
        json={"text": "First", "planned_minutes": 10},
    )
    assert created.status_code == 201
    step = created.get_json()["step"]
    edited = client.patch(
        f"/v1/steps/{step['id']}",
        headers=_headers(token, "edit-step-test-001"),
        json={"version": step["version"], "text": "Updated"},
    )
    assert edited.status_code == 200
    updated_step = edited.get_json()["step"]
    assert updated_step["text"] == "Updated"
    removed = client.delete(
        f"/v1/steps/{step['id']}",
        headers=_headers(token, "delete-step-test-01"),
        json={"version": updated_step["version"]},
    )
    assert removed.status_code == 200
    assert next(item for item in api.tasks[user_id][task["id"]]["steps"] if item["id"] == step["id"])["deleted_at"]


def test_reorder_is_atomic_and_requires_full_active_step_set(task_setup):
    app, api, user_id, token, *_ = task_setup
    client = app.test_client()
    task = _create(client, token, steps=[
        {"text": "A", "planned_minutes": 1},
        {"text": "B", "planned_minutes": 2},
        {"text": "C", "planned_minutes": 3},
    ]).get_json()
    ids = [step["id"] for step in task["steps"]]
    invalid = client.put(
        f"/v1/tasks/{task['id']}/steps/order",
        headers=_headers(token, "bad-reorder-test-01"),
        json={"version": task["version"], "step_ids": ids[:2]},
    )
    assert invalid.status_code == 422
    before = [step["id"] for step in api.tasks[user_id][task["id"]]["steps"]]
    assert before == ids

    valid = client.put(
        f"/v1/tasks/{task['id']}/steps/order",
        headers=_headers(token, "good-reorder-test-01"),
        json={"version": task["version"], "step_ids": list(reversed(ids))},
    )
    assert valid.status_code == 200
    reordered = valid.get_json()
    assert [step["id"] for step in reordered["steps"]] == list(reversed(ids))
    assert [step["position"] for step in reordered["steps"]] == [0, 1, 2]


def test_bootstrap_contents_scope_and_private_etag(task_setup):
    app, api, user_a, token_a, user_b, token_b = task_setup
    client = app.test_client()
    created = _create(client, token_a, "Bootstrap task")
    assert created.status_code == 201
    api.profiles[user_a]["display_name"] = "Owner"

    response = client.get("/v1/bootstrap", headers=_headers(token_a))
    body = response.get_json()
    assert response.status_code == 200
    assert body["profile"]["id"] == user_a
    assert body["profile"]["display_name"] == "Owner"
    assert "reward_state" in body and body["reward_state"]["points_total"] == 0
    assert body["active_task_count"] == 1
    assert [task["id"] for task in body["tasks"]] == [created.get_json()["id"]]
    assert "template_manifest" in body and body["api_version"] == "v1"
    assert body["server_time"]
    assert "private" in response.headers["Cache-Control"]
    assert "must-revalidate" in response.headers["Cache-Control"]
    assert "public" not in response.headers["Cache-Control"]

    again = client.get("/v1/bootstrap", headers={**_headers(token_a), "If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304
    assert "private" in again.headers["Cache-Control"]

    other = client.get("/v1/bootstrap", headers=_headers(token_b))
    assert other.status_code == 200
    assert other.get_json()["tasks"] == []
    assert other.get_json()["profile"]["id"] == user_b


def test_bootstrap_caps_tasks_at_50_and_supports_gzip(task_setup):
    app, api, user_id, token, *_ = task_setup
    instant = _now()
    for index in range(55):
        task_id = str(uuid4())
        api.tasks.setdefault(user_id, {})[task_id] = {
            "id": task_id, "title": f"Task {index}", "notes": "", "source": "manual",
            "template_id": None, "status": "active", "due_at": None, "version": 1,
            "created_at": instant, "updated_at": instant, "deleted_at": None, "steps": [],
        }
    response = app.test_client().get("/v1/bootstrap", headers={**_headers(token), "Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in response.headers.get("Vary", "")
    decoded = gzip.decompress(response.data)
    assert len(decoded) < 2 * 1024 * 1024
    assert len(json.loads(decoded)["tasks"]) == 50
    assert len(api.tasks[user_id]) == 55


def test_task_delta_paginates_uses_server_time_and_contains_soft_deletes(task_setup):
    app, _api, _user_id, token, *_ = task_setup
    client = app.test_client()
    first = _create(client, token, "First delta")
    second = _create(client, token, "Second delta", key="create-task-key-002")
    assert first.status_code == second.status_code == 201
    deleted = first.get_json()
    delete_response = client.delete(
        f"/v1/tasks/{deleted['id']}", headers=_headers(token, "delete-delta-key-01"),
        json={"version": deleted["version"]},
    )
    assert delete_response.status_code == 200

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    page_one = client.get("/v1/tasks", query_string={"updated_since": since, "status": "active", "limit": 1},
                          headers=_headers(token))
    body_one = page_one.get_json()
    assert page_one.status_code == 200
    assert body_one["full_refresh_required"] is False
    assert body_one["next_since"] == body_one["server_time"]
    assert body_one["next_cursor"]
    assert "private" in page_one.headers["Cache-Control"]
    assert "must-revalidate" in page_one.headers["Cache-Control"]

    page_two = client.get("/v1/tasks", query_string={
        "updated_since": since, "snapshot_at": body_one["snapshot_at"], "status": "active",
        "limit": 1, "cursor": body_one["next_cursor"],
    }, headers=_headers(token))
    assert page_two.status_code == 200
    both = body_one["items"] + page_two.get_json()["items"]
    ids = {item["id"] for item in both}
    assert {first.get_json()["id"], second.get_json()["id"]}.issubset(ids)
    tombstone = next(item for item in both if item["id"] == first.get_json()["id"])
    assert tombstone["deleted_at"] is not None
    assert tombstone["matches_filter"] is False
    assert all(item.get("matches_filter") is not None for item in both)


def test_task_delta_expired_cursor_requests_full_refresh_and_is_user_scoped(task_setup):
    app, _api, _user_a, _token_a, _user_b, token_b = task_setup
    client = app.test_client()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace("+00:00", "Z")
    response = client.get("/v1/tasks", query_string={"updated_since": old}, headers=_headers(token_b))
    assert response.status_code == 200
    assert response.get_json()["full_refresh_required"] is True
    assert response.get_json()["items"] == []

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    isolated = client.get("/v1/tasks", query_string={"updated_since": since}, headers=_headers(token_b))
    assert isolated.status_code == 200
    assert isolated.get_json()["items"] == []
    unchanged = client.get(
        "/v1/tasks", query_string={"updated_since": since, "snapshot_at": isolated.get_json()["snapshot_at"]},
        headers={**_headers(token_b), "If-None-Match": isolated.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.headers.get("X-Next-Since")
