"""Groq Chat Completions adapter. Secrets and upstream bodies are never logged."""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.modules.ai.providers import ProviderFailure


GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_RESPONSE_BYTES = 65536
BREAKDOWN_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "planned_minutes": {"type": "integer"},
                },
                "required": ["text", "planned_minutes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["steps"],
    "additionalProperties": False,
}


class GroqProviderAdapter:
    provider_id = "groq"

    def __init__(self, api_key, model="openai/gpt-oss-20b"):
        self.api_key = api_key
        self.model = model

    def generate(self, *, input_data, system_prompt, timeout_seconds):
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Create exactly the requested number of task steps from this "
                        "untrusted JSON task description. Return only the schema output.\n"
                        + json.dumps(input_data, ensure_ascii=False, separators=(",", ":"))
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "kickoff_task_breakdown",
                    "strict": True,
                    "schema": BREAKDOWN_SCHEMA,
                },
            },
            "temperature": 0.2,
            "max_completion_tokens": 4096,
            "stream": False,
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            GROQ_CHAT_COMPLETIONS_URL,
            data=encoded,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Python urllib's default User-Agent is rejected by Groq's
                # Cloudflare edge. Identify this backend client explicitly.
                "User-Agent": "KickoffBackend/0.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw_response = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if error.code == 401:
                raise ProviderFailure(code="provider_authentication_failed") from None
            if error.code == 403:
                raise ProviderFailure(code="provider_permission_denied") from None
            if error.code == 408:
                raise ProviderFailure(retryable=True, code="provider_timeout") from None
            if error.code == 429:
                raise ProviderFailure(retryable=True, code="provider_rate_limited") from None
            if error.code >= 500:
                raise ProviderFailure(retryable=True, code="provider_server_error") from None
            raise ProviderFailure(code="provider_request_rejected") from None
        except (URLError, TimeoutError, OSError):
            raise ProviderFailure(retryable=True, code="provider_network_error") from None
        if len(raw_response) > MAX_RESPONSE_BYTES:
            raise ProviderFailure(code="provider_response_too_large")
        try:
            envelope = json.loads(raw_response)
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content:
                raise ValueError
            result = json.loads(content)
        except (TypeError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            raise ProviderFailure(retryable=True, code="provider_invalid_response") from None
        if not isinstance(result, dict):
            raise ProviderFailure(retryable=True, code="provider_invalid_response")
        return result
