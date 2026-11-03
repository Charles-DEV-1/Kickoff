from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time
import json
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app import create_app
from app.common.public_rate_limit import PublicIPRateLimiter
from app.modules.ai.providers import ProviderFailure
from app.modules.templates.service import select_template, validated_variant


def _template(task_type, language, slug, keywords, version=1):
    counts = {"short": 5, "standard": 8, "detailed": 18}
    variants = {
        name: [
            {"text": f"Approved test step {index + 1}", "planned_minutes": 15 if index == 0 else 20}
            for index in range(count)
        ]
        for name, count in counts.items()
    }
    return {
        "id": str(uuid4()), "template_key": slug, "task_type": task_type,
        "language": language, "version": version, "title": f"Reviewed {slug}",
        "description": "Approved test fixture copy", "keywords": keywords,
        "variants": variants, "is_active": True, "updated_at": "2026-11-03T00:00:00Z",
    }


class TemplateDataAPI:
    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [])
        self.calls = []

    def select_public_templates(self):
        self.calls.append("select_public_templates")
        return deepcopy(self.rows)

    def get_authenticated_user(self, token):
        return {"id": jwt.decode(token, options={"verify_signature": False})["sub"]}

    def rpc(self, function_name, token, parameters=None):
        claims = jwt.decode(token, options={"verify_signature": False})
        user_id = claims["sub"]
        p = parameters or {}
        if function_name == "ensure_user_profile":
            return {"id": user_id, "display_name": None, "language": "yo", "timezone": "Africa/Lagos",
                    "settings": {}, "created_at": "2026-11-03T00:00:00Z",
                    "updated_at": "2026-11-03T00:00:00Z", "deleted_at": None,
                    "tokens_valid_after": None}
        if function_name == "consume_api_rate_limit":
            return {"user_id": user_id, "allowed": True, "remaining": p["p_limit"] - 1,
                    "retry_after_seconds": p["p_window_seconds"]}
        raise AssertionError(function_name)


class StaticKeyProvider:
    def __init__(self, key):
        self.key = key

    def get_public_key(self, kid, algorithm):
        if kid != "stage5-test" or algorithm != "ES256":
            raise jwt.InvalidTokenError
        return self.key


@pytest.fixture
def template_app():
    private_key = ec.generate_private_key(ec.SECP256R1())
    user_id = str(uuid4())
    session_id = str(uuid4())
    now = datetime.now(timezone.utc)
    token = jwt.encode({
        "sub": user_id, "session_id": session_id, "role": "authenticated",
        "iss": "https://test-project.supabase.co/auth/v1", "aud": "authenticated",
        "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=5)).timestamp()),
    }, private_key, algorithm="ES256", headers={"kid": "stage5-test"})
    rows = [
        _template("essay", "en", "essay-reviewed", ["essay", "argument"]),
        _template("essay", "yo", "essay-yo-reviewed", ["essay"]),
        _template("generic", "en", "generic-reviewed", ["school", "study"]),
    ]
    api = TemplateDataAPI(rows)
    app = create_app({
        "TESTING": True, "PROPAGATE_EXCEPTIONS": False,
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_JWKS_URL": "https://test-project.supabase.co/auth/v1/.well-known/jwks.json",
        "SUPABASE_JWT_ISSUER": "https://test-project.supabase.co/auth/v1",
        "SUPABASE_JWT_AUDIENCE": "authenticated", "SUPABASE_JWT_ALGORITHMS": ("ES256",),
        "SUPABASE_JWT_LEEWAY_SECONDS": 0, "SUPPORTED_LANGUAGES": ("en", "yo"),
        "ALLOWED_ORIGINS": (), "JWT_KEY_PROVIDER": StaticKeyProvider(private_key.public_key()),
        "SUPABASE_DATA_API": api,
        "PUBLIC_IP_RATE_LIMITER": PublicIPRateLimiter(limit=60, window_seconds=60),
    })
    return app, api, token


def test_template_selection_scores_keywords_and_breaks_ties_deterministically(template_app):
    _app, api, _token = template_app
    essay = [row for row in api.rows if row["task_type"] == "essay"]
    winner, notice = select_template(
        essay, task_type="essay", language="yo", text="Argument essay assignment"
    )
    assert winner["template_key"] == "essay-yo-reviewed"
    assert notice is None

    tied = [
        _template("essay", "en", "z-essay", ["essay"]),
        _template("essay", "en", "a-essay", ["essay"]),
    ]
    winner, _notice = select_template(
        tied, task_type="essay", language="en", text="essay"
    )
    assert winner["template_key"] == "a-essay"

    no_translation = [row for row in essay if row["language"] == "en"]
    winner, notice = select_template(
        no_translation, task_type="essay", language="yo", text="essay assignment"
    )
    assert winner["language"] == "en"
    assert notice == "language_fallback"


def test_template_selection_uses_generic_when_no_keyword_matches(template_app):
    _app, api, _token = template_app
    selected, notice = select_template(
        api.rows, task_type="essay", language="yo", text="biology lab work"
    )
    assert selected["template_key"] == "generic-reviewed"
    assert notice == "language_fallback"


def test_manifest_is_public_and_etag_changes_when_template_version_changes(template_app):
    app, api, _token = template_app
    client = app.test_client()
    first = client.get("/v1/templates/manifest")
    assert first.status_code == 200
    assert first.headers["Cache-Control"].startswith("public")
    assert first.headers["ETag"]
    item = next(row for row in first.get_json()["items"] if row["slug"] == "essay-reviewed")
    assert item["version"] == 1

    not_modified = client.get("/v1/templates/manifest", headers={"If-None-Match": first.headers["ETag"]})
    assert not_modified.status_code == 304
    assert not_modified.headers["ETag"] == first.headers["ETag"]

    api.rows[0]["version"] = 2
    app.extensions["kickoff_template_catalog"].rows(refresh=True)
    updated = client.get("/v1/templates/manifest")
    changed = next(row for row in updated.get_json()["items"] if row["slug"] == "essay-reviewed")
    assert changed["version"] == 2
    assert updated.headers["ETag"] != first.headers["ETag"]

    from pathlib import Path
    migration = Path(__file__).parents[1].joinpath(
        "supabase", "migrations", "202611030005_public_templates.sql"
    ).read_text(encoding="utf-8")
    assert "templates_version_bump" in migration
    assert "new.version := old.version + 1" in migration


def test_public_template_list_detail_filters_and_etag(template_app):
    app, api, _token = template_app
    client = app.test_client()
    listed = client.get("/v1/templates?task_type=essay&language=en&limit=1")
    assert listed.status_code == 200
    assert len(listed.get_json()["items"]) == 1
    template_id = listed.get_json()["items"][0]["id"]
    detail = client.get(f"/v1/templates/{template_id}")
    assert detail.status_code == 200
    assert set(detail.get_json()["variants"]) == {"short", "standard", "detailed"}
    assert client.get(f"/v1/templates/{template_id}", headers={"If-None-Match": detail.headers["ETag"]}).status_code == 304
    assert len(api.calls) == 1


def test_public_template_rate_limit_is_per_ip(template_app):
    app, _api, _token = template_app
    app.extensions["kickoff_public_ip_limiter"] = PublicIPRateLimiter(limit=1, window_seconds=60)
    client = app.test_client()
    first = client.get("/v1/templates/manifest", environ_base={"REMOTE_ADDR": "198.51.100.10"})
    blocked = client.get("/v1/templates/manifest", environ_base={"REMOTE_ADDR": "198.51.100.10"})
    other_ip = client.get("/v1/templates/manifest", environ_base={"REMOTE_ADDR": "198.51.100.11"})
    assert first.status_code == 200
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]
    assert other_ip.status_code == 200


@pytest.mark.parametrize("level,count", [(1, 3), (2, 5), (3, 8), (4, 12), (5, 18)])
def test_breakdown_detail_levels_return_validated_count(template_app, level, count):
    app, _api, token = template_app
    response = app.test_client().post(
        "/v1/ai/breakdown",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Argument essay", "task_type": "essay", "detail_level": level},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["steps"]) == count
    assert body["steps"][0]["planned_minutes"] <= 10
    assert body["source"] == "template"
    assert body["template_id"]
    assert body["prompt_version"]
    assert body["usage"] == {"used": 0, "limit": 10}
    assert _api.calls.count("select_public_templates") == 1


@pytest.mark.parametrize("payload", [
    {"title": "ab"}, {"title": "A valid title", "description": "x" * 501},
    {"title": "A valid title", "detail_level": True},
    {"title": "A valid title", "detail_level": 6},
    {"title": "A valid title", "language": "zz"},
    {"title": "A valid title", "task_type": "unknown"},
    {"title": "A valid title", "task_type": []},
    {"title": "A valid title", "unexpected": "field"},
])
def test_breakdown_rejects_malformed_input_without_echoing_user_text(template_app, payload):
    app, _api, token = template_app
    response = app.test_client().post(
        "/v1/ai/breakdown", headers={"Authorization": f"Bearer {token}"}, json=payload
    )
    assert response.status_code == 422
    body = response.get_json()
    assert body["error"]["code"] == "validation_failed"
    assert "A valid title" not in response.get_data(as_text=True)


def test_breakdown_is_protected_and_does_not_create_tasks(template_app):
    app, api, _token = template_app
    response = app.test_client().post("/v1/ai/breakdown", json={"title": "A valid title"})
    assert response.status_code == 401
    assert not any("create_my_task" in str(call) for call in api.calls)


def test_detail_level_variant_rejects_too_few_or_malformed_approved_steps():
    template = _template("generic", "en", "generic", [])
    template["variants"]["short"] = template["variants"]["short"][:2]
    assert validated_variant(template, 1) is None
    template = _template("generic", "en", "generic", [])
    template["variants"]["short"][1]["planned_minutes"] = True
    assert validated_variant(template, 1) is None


def test_breakdown_reports_missing_reviewed_generic_content_instead_of_inventing_steps(template_app):
    app, api, token = template_app
    api.rows = [row for row in api.rows if row["task_type"] != "generic"]
    catalog = app.extensions.get("kickoff_template_catalog")
    if catalog:
        catalog.rows(refresh=True)
    response = app.test_client().post(
        "/v1/ai/breakdown", headers={"Authorization": f"Bearer {token}"},
        json={"title": "A research assignment"},
    )
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "templates_unavailable"


def test_provisional_english_seed_meets_variant_and_duration_limits():
    import json
    import re
    from pathlib import Path

    seed = Path(__file__).parents[1].joinpath(
        "supabase", "migrations", "202611030006_provisional_generic_english_template.sql"
    ).read_text(encoding="utf-8")
    match = re.search(r"provisional_variants jsonb := '(.*?)'::jsonb;", seed, re.DOTALL)
    assert match, "provisional template JSON is missing"
    variants = json.loads(match.group(1))
    assert {name: len(steps) for name, steps in variants.items()} == {
        "short": 5, "standard": 8, "detailed": 18,
    }
    for steps in variants.values():
        for step in steps:
            assert 1 <= len(step["text"]) <= 280
            assert isinstance(step["planned_minutes"], int)
            assert 1 <= step["planned_minutes"] <= 120
        assert steps[0]["planned_minutes"] <= 10


class FakeAIStore:
    def __init__(self, *, provider_budget=10):
        self.cache = {}
        self.used = 0
        self.pending = set()
        self.provider_budget = provider_budget
        self.provider_calls = 0

    def get_cache(self, cache_hash):
        result = self.cache.get(cache_hash)
        return {"hit": result is not None, "result": deepcopy(result)}

    def read_user_usage(self):
        return {"used": self.used}

    def reserve_user_usage(self, limit):
        if self.used + len(self.pending) >= limit:
            return {"allowed": False, "used": self.used}
        reservation = str(uuid4())
        self.pending.add(reservation)
        return {"allowed": True, "reservation_id": reservation}

    def commit_user_usage(self, reservation_id):
        if reservation_id not in self.pending:
            return {"committed": False, "used": self.used}
        self.pending.remove(reservation_id)
        self.used += 1
        return {"committed": True, "used": self.used}

    def reserve_provider_budget(self, _provider_id, _limit, _usage_date):
        if self.provider_calls >= self.provider_budget:
            return {"allowed": False, "used": self.provider_calls}
        self.provider_calls += 1
        return {"allowed": True, "used": self.provider_calls}

    def save_cache(self, cache_hash, result, _ttl):
        self.cache[cache_hash] = deepcopy(result)
        return True


class FakeAIProvider:
    provider_id = "fake"

    def __init__(self, result=None, *, error=None, delay=0):
        self.result = result
        self.error = error
        self.delay = delay
        self.calls = []

    def generate(self, *, input_data, system_prompt, timeout_seconds):
        self.calls.append({
            "input_data": deepcopy(input_data),
            "system_prompt": system_prompt,
            "timeout_seconds": timeout_seconds,
        })
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if isinstance(self.result, list):
            result = self.result.pop(0)
            return result if isinstance(result, BaseException) else deepcopy(result)
        return deepcopy(self.result)


def _valid_ai_result(level=3):
    count = {1: 3, 2: 5, 3: 8, 4: 12, 5: 18}[level]
    return {
        "steps": [
            {"text": f"Generated safe step {index + 1}",
             "planned_minutes": 10 if index == 0 else 20,
             "discard_me": "provider metadata"}
            for index in range(count)
        ],
        "untrusted_metadata": "discard this field",
    }


def _enable_fake_ai(app, provider, store):
    app.config.update({
        "AI_CACHE_HMAC_KEY": "test-only-hmac-secret-with-at-least-32-bytes",
        "AI_TOTAL_BUDGET_SECONDS": 8,
        "AI_PROVIDER_TIMEOUT_SECONDS": 2,
        "AI_DAILY_ALLOWANCE": 10,
        "AI_PROVIDER_DAILY_BUDGET": 10,
    })
    app.extensions["kickoff_ai_provider"] = provider
    app.extensions["kickoff_ai_store"] = store


def _post_breakdown(app, token, **payload):
    return app.test_client().post(
        "/v1/ai/breakdown", headers={"Authorization": f"Bearer {token}"},
        json={"title": "Research essay", "detail_level": 3, **payload},
    )


def test_ai_valid_result_discards_extra_fields_and_cache_hit_does_not_count(template_app):
    app, _api, token = template_app
    provider = FakeAIProvider(_valid_ai_result())
    store = FakeAIStore()
    _enable_fake_ai(app, provider, store)

    first = _post_breakdown(app, token, description="Read the assignment")
    assert first.status_code == 200
    body = first.get_json()
    assert body["source"] == "ai"
    assert body["usage"] == {"used": 1, "limit": 10}
    assert len(body["steps"]) == 8
    assert set(body["steps"][0]) == {"text", "planned_minutes"}
    assert provider.calls[0]["system_prompt"]

    replay = _post_breakdown(app, token, description="Read the assignment")
    assert replay.status_code == 200
    assert replay.get_json()["source"] == "ai"
    assert replay.get_json()["usage"]["used"] == 1
    assert len(provider.calls) == 1
    assert store.used == 1


@pytest.mark.parametrize("result", [
    None,
    {"steps": []},
    {"steps": [{"text": "too few", "planned_minutes": 5}]},
    {"steps": [
        {"text": "<script>alert(1)</script>", "planned_minutes": 5}
        for _ in range(8)
    ]},
])
def test_empty_malformed_or_malicious_provider_output_falls_back_without_usage(template_app, result):
    app, _api, token = template_app
    provider = FakeAIProvider(result)
    store = FakeAIStore()
    _enable_fake_ai(app, provider, store)
    response = _post_breakdown(app, token)
    assert response.status_code == 200
    assert response.get_json()["source"] == "template"
    assert response.get_json()["usage"]["used"] == 0
    assert store.used == 0
    assert len(provider.calls) <= 2


def test_unavailable_provider_falls_back_without_usage_or_sensitive_diagnostics(template_app, caplog):
    app, _api, token = template_app
    provider = FakeAIProvider(error=ProviderFailure(retryable=False))
    store = FakeAIStore()
    store.used = 4
    _enable_fake_ai(app, provider, store)
    response = _post_breakdown(app, token)
    assert response.status_code == 200
    assert response.get_json()["source"] == "template"
    assert response.get_json()["usage"]["used"] == 4
    assert store.used == 4
    assert len(provider.calls) == 1
    assert any(
        getattr(record, "ai_fallback_reason", None) == "provider_request_failed"
        for record in caplog.records
    )
    assert "Research essay" not in caplog.text


def test_slow_provider_is_bounded_and_uses_template_fallback(template_app):
    app, _api, token = template_app
    provider = FakeAIProvider(_valid_ai_result(), delay=0.15)
    store = FakeAIStore()
    _enable_fake_ai(app, provider, store)
    app.config["AI_TOTAL_BUDGET_SECONDS"] = 0.05
    started = time.monotonic()
    response = _post_breakdown(app, token)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.get_json()["source"] == "template"
    assert elapsed < 0.5
    assert store.used == 0


def test_provider_input_redacts_pii_and_user_text_never_appears_in_logs(template_app, caplog):
    app, _api, token = template_app
    provider = FakeAIProvider(_valid_ai_result())
    store = FakeAIStore()
    _enable_fake_ai(app, provider, store)
    email = "secret.student@example.com"
    phone = "+234 803 555 1234"
    response = _post_breakdown(
        app, token, title=f"Research essay for {email}",
        description=f"Call me at {phone} about this assignment.",
    )
    assert response.status_code == 200
    sent = json.dumps(provider.calls[0]["input_data"])
    assert email not in sent
    assert phone not in sent
    captured_logs = caplog.text
    assert email not in captured_logs
    assert phone not in captured_logs


def test_retryable_failure_retries_once_and_provider_budget_exhaustion_falls_back(template_app):
    app, _api, token = template_app
    provider = FakeAIProvider([
        ProviderFailure(retryable=True),
        _valid_ai_result(),
    ])
    store = FakeAIStore()
    _enable_fake_ai(app, provider, store)
    response = _post_breakdown(app, token)
    assert response.status_code == 200
    assert response.get_json()["source"] == "ai"
    assert len(provider.calls) == 2
    assert store.provider_calls == 2

    provider2 = FakeAIProvider(_valid_ai_result())
    store2 = FakeAIStore(provider_budget=0)
    _enable_fake_ai(app, provider2, store2)
    blocked = _post_breakdown(app, token, description="different prompt")
    assert blocked.status_code == 200
    assert blocked.get_json()["source"] == "template"
    assert provider2.calls == []
    assert store2.used == 0
