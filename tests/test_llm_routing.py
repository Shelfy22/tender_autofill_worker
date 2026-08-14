import pytest

from app.config import Settings
from app.services.llm import LlmClient


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "postgres_dsn": "postgresql://user:pass@localhost/db",
        "llm_api_key": "test",
        "llm_model_attempt_1": "model-a",
        "llm_model_attempt_2": "model-b",
        "llm_model_attempt_3": "model-c",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_each_job_attempt_uses_one_openai_model() -> None:
    configured = settings()
    assert configured.models_for_attempt(1) == ["model-a"]
    assert configured.models_for_attempt(2) == ["model-b"]
    assert configured.models_for_attempt(3) == ["model-c"]


def test_default_models_match_direct_openai_attempt_semantics() -> None:
    configured = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        llm_api_key="test",
    )
    assert configured.llm_base_url == "https://api.openai.com/v1"
    assert configured.models_for_attempt(1) == ["gpt-5"]
    assert configured.models_for_attempt(2) == ["gpt-5-mini"]
    assert configured.models_for_attempt(3) == ["gpt-4.1"]
    assert configured.models_for_ocr() == ["gpt-4.1"]


def test_openrouter_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="not OpenRouter"):
        settings(llm_base_url="https://openrouter.ai/api/v1")


def test_openrouter_key_and_provider_prefixed_models_are_rejected() -> None:
    with pytest.raises(ValueError, match="not OpenRouter"):
        settings(llm_api_key="sk-or-v1-test")
    with pytest.raises(ValueError, match="provider prefix"):
        settings(llm_model_attempt_1="openai/gpt-5")


def test_gpt5_chat_parameters_omit_temperature() -> None:
    client = LlmClient(settings(llm_model_attempt_1="gpt-5"), attempt=1)
    assert client._chat_completion_kwargs() == {
        "model": "gpt-5",
        "max_completion_tokens": 16_000,
    }


def test_gpt41_chat_parameters_keep_deterministic_temperature() -> None:
    client = LlmClient(settings(llm_model_attempt_3="gpt-4.1"), attempt=3)
    assert client._chat_completion_kwargs() == {
        "model": "gpt-4.1",
        "max_completion_tokens": 16_000,
        "temperature": 0,
    }


def test_responses_output_text_is_extracted_from_http_payload() -> None:
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Первая страница"},
                    {"type": "output_text", "text": "Вторая страница"},
                ],
            }
        ]
    }
    assert LlmClient._responses_output_text(response) == "Первая страница\nВторая страница"


def test_responses_usage_uses_input_and_output_token_names() -> None:
    assert LlmClient._usage(
        {"usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}}
    ) == (120, 30, 150)
