import time

import pytest

from app.config import Settings
from app.models import ExtractedFieldsResponse
from app.services.llm import LlmClient


def settings(**overrides: object) -> Settings:
    return Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        llm_api_key="test",
        llm_model_attempt_1="model-a",
        llm_model_attempt_2="model-b",
        llm_model_attempt_3="model-c",
        **overrides,
    )


def test_models_are_rotated_by_job_attempt_with_per_call_fallback() -> None:
    configured = settings()
    assert configured.models_for_attempt(1) == ["model-a", "model-b", "model-c"]
    assert configured.models_for_attempt(2) == ["model-b", "model-c", "model-a"]
    assert configured.models_for_attempt(3) == ["model-c", "model-a", "model-b"]


def test_model_fallback_can_be_disabled() -> None:
    assert settings(llm_enable_model_fallback=False).models_for_attempt(2) == ["model-b"]


def test_openrouter_fallback_works_through_internal_proxy_url() -> None:
    client = LlmClient(
        settings(llm_base_url="http://172.22.172.111:5000/api/v1"),
        attempt=1,
    )
    assert client._fallback_body(client.model_chain) == {
        "models": ["model-b", "model-c"]
    }


def test_openrouter_structured_body_contains_routing_and_healing() -> None:
    client = LlmClient(settings(), attempt=1)
    assert client._structured_extra_body(client.model_chain) == {
        "models": ["model-b", "model-c"],
        "provider": {"require_parameters": True},
        "reasoning": {"effort": "none"},
        "plugins": [{"id": "response-healing"}],
    }


def test_openai_compatible_provider_does_not_receive_router_parameters() -> None:
    client = LlmClient(settings(llm_provider="openai_compatible"), attempt=1)
    assert client._fallback_body(client.model_chain) == {}
    assert client._structured_extra_body(client.model_chain) == {}


def test_structured_output_uses_pydantic_json_schema() -> None:
    client = LlmClient(settings(), attempt=1)
    response_format = client._response_format(ExtractedFieldsResponse)
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"fields", "warnings"}
    fields_schema = schema["properties"]["fields"]
    assert fields_schema["additionalProperties"] is False
    assert "dateCreated" in fields_schema["required"]
    assert fields_schema["properties"]["initialPrice"] == {
        "$ref": "#/$defs/FieldValue"
    }


def test_json_object_compatibility_mode_can_be_selected() -> None:
    client = LlmClient(settings(llm_structured_output_mode="json_object"), attempt=1)
    assert client._response_format(ExtractedFieldsResponse) == {"type": "json_object"}


def test_catalog_selection_uses_gpt_oss_then_qwen_by_default() -> None:
    assert settings().models_for_catalog_selection() == [
        "openai/gpt-oss-120b",
        "qwen/qwen3.7-flash",
        "qwen/qwen3.5-flash-02-23",
    ]


def test_catalog_selection_model_fallback_can_be_disabled() -> None:
    assert settings(llm_enable_model_fallback=False).models_for_catalog_selection() == [
        "openai/gpt-oss-120b"
    ]


def test_stage_timeouts_and_full_model_fallback_defaults() -> None:
    configured = settings()

    assert configured.timeout_for("consolidate_tender_analysis") == 60
    assert configured.timeout_for("ocr_pdf") == 360
    assert configured.llm_max_attempts_per_unit == 3
    assert configured.models_for_attempt(1) == ["model-a", "model-b", "model-c"]


def test_llm_wall_timeout_returns_before_slow_call_finishes() -> None:
    client = LlmClient(settings(llm_rate_limit_backoff_seconds=0), attempt=1)
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        client._call_with_wall_timeout("unit-test", 0.01, lambda: time.sleep(1))

    assert time.monotonic() - started < 0.5