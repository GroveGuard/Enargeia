from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests

from winkg import gui


class FakeResponse:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        llm_provider="openai-compatible",
        llm_enabled=True,
        llm_base_url="https://llm.example/v1",
        llm_model="test-model",
        llm_api_key="",
        llm_timeout_seconds=10,
    )


def test_llm_connection_test_requires_extraction_json_schema(monkeypatch):
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse('{"entities":[],"relations":[]}')

    monkeypatch.setattr(gui, "get_settings", _settings)
    monkeypatch.setattr(requests, "post", post)

    result = gui._run_llm_test()

    assert result["status"] == "OK"
    assert result["output_format"] == "json_object"
    request = calls[0]
    assert request["json"]["response_format"] == {"type": "json_object"}
    assert {message["role"] for message in request["json"]["messages"]} == {"system", "user"}
    assert request["json"]["max_tokens"] == 256


@pytest.mark.parametrize(
    "content, message",
    [
        (
            "The user wants me to extract a knowledge graph.",
            "liefert aber kein gültiges JSON",
        ),
        (
            '{"answer":"Acme uses SAP"}',
            "erforderlichen Arrays",
        ),
    ],
)
def test_llm_connection_test_rejects_nonconforming_responses(
    monkeypatch, content: str, message: str
):
    monkeypatch.setattr(gui, "get_settings", _settings)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: FakeResponse(content))

    with pytest.raises(ValueError, match=message):
        gui._run_llm_test()
