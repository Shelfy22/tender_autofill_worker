import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import ExtractedFieldsResponse, TenderPosition, TenderPositionsResponse
from app.services.llm import (
    LlmClient,
    LlmResponseTruncatedError,
    extract_json,
    extract_json_result,
)


def test_malformed_llm_json_is_repaired_before_validation() -> None:
    malformed = (
        '{"fields":{"dateCreated":{"value":"2026-08-17" '
        '"confidence":"high","source":"document","evidence":"header"}},'
        '"warnings":[]}'
    )

    extracted = extract_json_result(malformed)
    validated = ExtractedFieldsResponse.model_validate(extracted.value)

    assert extracted.repaired is True
    assert extracted.initial_error is not None
    assert validated.fields["dateCreated"].value == "2026-08-17"
    assert validated.fields["dateCreated"].confidence == "high"


def test_valid_json_is_never_sent_through_repair() -> None:
    extracted = extract_json_result('{"fields":{},"warnings":[]}')
    assert extracted.repaired is False
    assert extracted.source == "raw"


def test_truncated_response_can_be_forbidden_from_repair() -> None:
    with pytest.raises(json.JSONDecodeError):
        extract_json_result('{"decision":"reject"', allow_repair=False)


def test_json_call_rejects_even_valid_json_when_provider_marks_it_truncated() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
        ),
        attempt=1,
    )
    response = SimpleNamespace(
        model="model-a",
        choices=[
            SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content='{"fields":{},"warnings":[]}'),
            )
        ],
        usage=None,
    )
    client.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_: response)
        )
    )

    with pytest.raises(LlmResponseTruncatedError):
        client.json_call(
            system="test",
            prompt="test",
            schema=ExtractedFieldsResponse,
        )


def test_json_call_normalizes_top_level_product_list() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
        ),
        attempt=1,
    )
    response = SimpleNamespace(
        model="model-a",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="[]"),
            )
        ],
        usage=None,
    )
    client.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_: response)
        )
    )

    parsed = client.json_call(
        system="test",
        prompt="test",
        schema=TenderPositionsResponse,
    )

    assert parsed.products == []
    assert parsed.warnings == []


def test_product_extraction_retries_truncated_full_response_in_chunks() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)
    calls: list[str] = []

    def fake_json_call(**values: object) -> TenderPositionsResponse:
        operation = str(values["operation"])
        calls.append(operation)
        if operation == "extract_tender_products":
            raise LlmResponseTruncatedError("finish_reason=length")
        index = len(calls) - 1
        return TenderPositionsResponse(
            products=[
                TenderPosition(
                    product=f"Товар {index}",
                    quantity=1,
                    unit="шт",
                )
            ]
        )

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.extract_products(
        "\n".join(f"Строка {index}: товар" for index in range(1, 9)),
        [],
    )

    assert len(response.products) >= 2
    assert any("повторено частями" in item for item in response.warnings)
    assert calls[0] == "extract_tender_products"
    assert all(
        operation.startswith("extract_tender_products_chunk_")
        for operation in calls[1:]
    )


def test_large_product_text_skips_wasteful_full_llm_call() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)
    calls: list[str] = []

    def fake_json_call(**values: object) -> TenderPositionsResponse:
        operation = str(values["operation"])
        calls.append(operation)
        return TenderPositionsResponse(products=[])

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.extract_products(
        "\n".join(
            f"Строка {index}: A: {index} | B: Товар {index} | D: шт | E: 1"
            for index in range(1, 1_001)
        ),
        [],
    )

    assert response.products == []
    assert len(calls) > 1
    assert "extract_tender_products" not in calls
    assert all(
        operation.startswith("extract_tender_products_chunk_")
        for operation in calls
    )
    assert any("сразу выполнено небольшими частями" in item for item in response.warnings)


def test_markdown_wrapped_json_remains_supported() -> None:
    assert extract_json('result:\n```json\n{"fields": {}, "warnings": []}\n```') == {
        "fields": {},
        "warnings": [],
    }


def test_fields_list_is_normalized_to_the_expected_dictionary() -> None:
    response = ExtractedFieldsResponse.model_validate(
        {
            "fields": [
                {
                    "fieldName": "dateCreated",
                    "value": "2026-08-17",
                    "confidence": "high",
                    "source": "document",
                    "evidence": "header",
                },
                {
                    "fieldName": "initialPrice",
                    "fieldValue": "1500000 RUB",
                    "confidence": "medium",
                },
            ],
            "warnings": [],
        }
    )

    assert set(response.fields) == {"dateCreated", "initialPrice"}
    assert response.fields["dateCreated"].value == "2026-08-17"
    assert response.fields["initialPrice"].value == "1500000 RUB"


def test_duplicate_fields_keep_first_value_deterministically() -> None:
    response = ExtractedFieldsResponse.model_validate(
        {
            "fields": [
                {"fieldName": "dateCreated", "value": "2026-08-17"},
                {"fieldName": "dateCreated", "value": "2099-01-01"},
            ]
        }
    )
    assert response.fields["dateCreated"].value == "2026-08-17"
