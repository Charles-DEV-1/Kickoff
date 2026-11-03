import json
from urllib.error import HTTPError

import pytest

from app import create_app
from app.modules.ai.groq_provider import (
    BREAKDOWN_SCHEMA,
    GROQ_CHAT_COMPLETIONS_URL,
    GroqProviderAdapter,
)
from app.modules.ai.providers import ProviderFailure


def test_groq_is_selected_only_when_configured_with_a_key():
    active = create_app({
        "TESTING": True,
        "AI_PROVIDER": "groq",
        "GROQ_API_KEY": "test-only-key",
        "GROQ_MODEL": "openai/gpt-oss-20b",
        "ALLOWED_ORIGINS": (),
    })
    assert isinstance(active.extensions["kickoff_ai_provider"], GroqProviderAdapter)

    no_key = create_app({
        "TESTING": True,
        "AI_PROVIDER": "groq",
        "GROQ_API_KEY": "",
        "ALLOWED_ORIGINS": (),
    })
    assert no_key.extensions["kickoff_ai_provider"] is None


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def test_groq_adapter_uses_fixed_endpoint_strict_json_and_bounded_timeout(monkeypatch):
    content = json.dumps({"steps": [{"text": "Read the prompt", "planned_minutes": 5}]})
    envelope = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["user_agent"] = request.get_header("User-agent")
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(envelope)

    monkeypatch.setattr("app.modules.ai.groq_provider.urlopen", fake_urlopen)
    adapter = GroqProviderAdapter("test-groq-secret", "openai/gpt-oss-20b")
    result = adapter.generate(
        input_data={"title": "Exam prep", "step_count": 1},
        system_prompt="Fixed prompt v1",
        timeout_seconds=3.5,
    )

    assert result == {"steps": [{"text": "Read the prompt", "planned_minutes": 5}]}
    assert captured["url"] == GROQ_CHAT_COMPLETIONS_URL
    assert captured["authorization"] == "Bearer test-groq-secret"
    assert captured["user_agent"] == "KickoffBackend/0.1"
    assert captured["timeout"] == 3.5
    assert captured["body"]["model"] == "openai/gpt-oss-20b"
    assert captured["body"]["response_format"]["json_schema"]["strict"] is True
    assert captured["body"]["response_format"]["json_schema"]["schema"] == BREAKDOWN_SCHEMA
    assert all("user_id" not in message["content"] for message in captured["body"]["messages"])


def test_groq_rate_limit_is_retryable_without_exposing_upstream_body(monkeypatch):
    secret = "do-not-repeat-this-secret"

    def rate_limited(_request, timeout):
        raise HTTPError(
            GROQ_CHAT_COMPLETIONS_URL, 429, "limited", {},
            # The upstream body may contain details and must not be surfaced.
            fp=None,
        )

    monkeypatch.setattr("app.modules.ai.groq_provider.urlopen", rate_limited)
    adapter = GroqProviderAdapter(secret)
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(input_data={}, system_prompt="private task", timeout_seconds=1)
    assert error.value.retryable is True
    assert secret not in str(error.value)
    assert "private task" not in str(error.value)


@pytest.mark.parametrize(("status", "expected_code"), [
    (401, "provider_authentication_failed"),
    (403, "provider_permission_denied"),
])
def test_groq_auth_and_permission_errors_have_safe_codes(monkeypatch, status, expected_code):
    monkeypatch.setattr(
        "app.modules.ai.groq_provider.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError(GROQ_CHAT_COMPLETIONS_URL, status, "restricted", {}, fp=None)
        ),
    )
    with pytest.raises(ProviderFailure) as error:
        GroqProviderAdapter("test-key").generate(
            input_data={}, system_prompt="prompt", timeout_seconds=1
        )
    assert error.value.code == expected_code


def test_groq_adapter_rejects_malformed_response_safely(monkeypatch):
    envelope = json.dumps({"choices": [{"message": {"content": "not-json"}}]}).encode()
    monkeypatch.setattr(
        "app.modules.ai.groq_provider.urlopen", lambda *_args, **_kwargs: FakeResponse(envelope)
    )
    adapter = GroqProviderAdapter("test-key")
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(input_data={}, system_prompt="prompt", timeout_seconds=1)
    assert error.value.retryable is True
    assert "not-json" not in str(error.value)
