from app.config import Settings
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


def test_openrouter_extra_body_contains_only_fallback_models() -> None:
    client = LlmClient(settings(), attempt=1)
    assert client._fallback_body(client.model_chain) == {
        "models": ["model-b", "model-c"]
    }


def test_non_openrouter_endpoint_does_not_receive_router_specific_parameter() -> None:
    client = LlmClient(settings(llm_base_url="https://api.openai.com/v1"), attempt=1)
    assert client._fallback_body(client.model_chain) == {}
