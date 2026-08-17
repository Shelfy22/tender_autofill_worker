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
