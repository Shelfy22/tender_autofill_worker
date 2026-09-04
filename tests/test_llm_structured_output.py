import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models import (
    DocumentAnalysisResponse,
    DocumentAnalysisUnit,
    ExtractedFieldsResponse,
    ProductHierarchyResponse,
    ProductCandidateAuditResponse,
    TenderPosition,
    TenderPositionsResponse,
)
from app.services.llm import (
    LlmClient,
    LlmMalformedResponseError,
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




def test_json_call_disables_openrouter_reasoning_by_default() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
        ),
        attempt=1,
    )
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            model="model-a",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"fields":{},"warnings":[]}'),
                )
            ],
            usage=None,
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    client.json_call(system="test", prompt="test", schema=ExtractedFieldsResponse)

    extra_body = captured["extra_body"]
    assert isinstance(extra_body, dict)
    assert extra_body["reasoning"] == {"effort": "none"}
    assert str(captured["messages"][1]["content"]).startswith("/no_think\n")

def test_json_call_rejects_even_valid_json_when_provider_marks_it_truncated() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
            llm_model_attempt_2="model-b",
            llm_model_attempt_3="model-c",
        ),
        attempt=1,
    )
    calls: list[str] = []
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

    def create(**kwargs: object) -> SimpleNamespace:
        calls.append(str(kwargs["model"]))
        return response

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    with pytest.raises(LlmResponseTruncatedError):
        client.json_call(
            system="test",
            prompt="test",
            schema=ExtractedFieldsResponse,
        )

    assert calls == ["model-a", "model-b", "model-c"]

def test_json_call_falls_back_when_model_returns_invalid_json() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
            llm_model_attempt_2="model-b",
            llm_model_attempt_3="model-c",
        ),
        attempt=1,
    )
    calls: list[str] = []

    def create(**kwargs: object) -> SimpleNamespace:
        model = str(kwargs["model"])
        calls.append(model)
        content = (
            "Конечно, вот результат: fields: []"
            if model == "model-a"
            else '{"fields":{},"warnings":[]}'
        )
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=None,
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    parsed = client.json_call(
        system="test",
        prompt="test",
        schema=ExtractedFieldsResponse,
    )

    assert parsed.fields == {}
    assert calls == ["model-a", "model-b"]
    assert client.models_used == ["model-a", "model-b"]


def test_json_call_uses_stage_specific_completion_budget() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
            llm_max_completion_tokens=24000,
            catalog_selection_max_completion_tokens=1234,
        ),
        attempt=1,
    )
    captured: dict[str, object] = {}

    def create(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            model="model-a",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"fields":{},"warnings":[]}'),
                )
            ],
            usage=None,
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    client.json_call(
        system="test",
        prompt="test",
        schema=ExtractedFieldsResponse,
        operation="catalog_product_selection",
    )

    assert captured["max_tokens"] == 1234
    assert captured["timeout"] == 45


def test_json_call_respects_max_attempts_per_unit() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
            llm_model_attempt_2="model-b",
            llm_model_attempt_3="model-c",
            llm_max_attempts_per_unit=2,
        ),
        attempt=1,
    )
    calls: list[str] = []

    def create(**kwargs: object) -> SimpleNamespace:
        model = str(kwargs["model"])
        calls.append(model)
        content = '{"fields":}' if model in {"model-a", "model-b"} else '{"fields":{},"warnings":[]}'
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=None,
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    with pytest.raises(Exception):
        client.json_call(
            system="test",
            prompt="test",
            schema=ExtractedFieldsResponse,
        )

    assert calls == ["model-a", "model-b"]


def test_json_call_falls_back_when_provider_response_has_no_choices() -> None:
    client = LlmClient(
        Settings(
            postgres_dsn="postgresql://user:pass@localhost/db",
            llm_api_key="test",
            llm_model_attempt_1="model-a",
            llm_model_attempt_2="model-b",
            llm_model_attempt_3="model-c",
        ),
        attempt=1,
    )
    calls: list[str] = []

    def create(**kwargs: object) -> SimpleNamespace:
        model = str(kwargs["model"])
        calls.append(model)
        if model == "model-a":
            return SimpleNamespace(model=model, choices=None, usage=None)
        return SimpleNamespace(
            model=model,
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"fields":{},"warnings":[]}'),
                )
            ],
            usage=None,
        )

    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    parsed = client.json_call(
        system="test",
        prompt="test",
        schema=ExtractedFieldsResponse,
    )

    assert parsed.fields == {}
    assert calls == ["model-a", "model-b"]
    assert client.models_used == ["model-a", "model-b"]


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


def test_document_analyzer_prompt_distinguishes_documents_from_works_and_zip() -> None:
    client = object.__new__(LlmClient)
    captured: dict[str, object] = {}

    def fake_json_call(**values: object) -> DocumentAnalysisResponse:
        captured.update(values)
        return DocumentAnalysisResponse(products=[], reasonHits=[], fieldCandidates=[])

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.analyze_document_unit(
        DocumentAnalysisUnit(
            unitId="unit-1",
            sourceType="document",
            fileName="spec.pdf",
            text="предоставить документацию по монтажу, наладке и пуску; ЗИП входит в комплект поставки",
            inputSha256="hash",
        )
    )

    assert response.products == []
    prompt = str(captured["prompt"])
    assert captured["schema"] is DocumentAnalysisResponse
    assert str(captured["operation"]).startswith("analyze_document: spec.pdf")
    assert "Не считай инструкцию/руководство/документацию по монтажу" in prompt
    assert "ЗИП/ремкомплект/запасные части" in prompt
    assert "Не возвращай coverage" in prompt


def test_product_hierarchy_uses_structured_response() -> None:
    client = object.__new__(LlmClient)
    captured: dict[str, object] = {}

    def fake_json_call(**values: object) -> ProductHierarchyResponse:
        captured.update(values)
        return ProductHierarchyResponse(assignments=[])

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.classify_product_hierarchy(
        [
            TenderPosition(product="КТП-1000", quantity=1, unit="шт"),
            TenderPosition(product="Трансформатор", quantity=1, unit="шт"),
        ]
    )

    assert response.assignments == []
    assert captured["schema"] is ProductHierarchyResponse
    assert captured["operation"] == "classify_product_hierarchy"
    assert "parentPositionIndex" in str(captured["prompt"])


def test_product_candidate_audit_uses_structured_response_and_source_cells() -> None:
    client = object.__new__(LlmClient)
    captured: dict[str, object] = {}

    def fake_json_call(**values: object) -> ProductCandidateAuditResponse:
        captured.update(values)
        return ProductCandidateAuditResponse(assignments=[])

    client.json_call = fake_json_call  # type: ignore[method-assign]
    response = client.audit_product_candidates(
        [TenderPosition(product="Аналоги рассматриваются", quantity=162)]
    )

    assert response.assignments == []
    assert captured["schema"] is ProductCandidateAuditResponse
    assert captured["operation"] == "audit_product_candidates"
    assert "duplicateOf" in str(captured["prompt"])
    assert "sourceReference" in str(captured["prompt"])



def test_product_extraction_skips_chunk_retry_when_full_response_is_truncated() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)
    calls: list[str] = []

    def fake_json_call(**values: object) -> TenderPositionsResponse:
        calls.append(str(values["operation"]))
        raise LlmResponseTruncatedError("finish_reason=length")

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.extract_products("small tender text", [])

    assert response.products == []
    assert calls == ["extract_tender_products"]
    assert any("skipped chunk retry" in item for item in response.warnings)


def test_product_extraction_skips_chunk_retry_for_malformed_response() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)
    calls: list[str] = []

    def fake_json_call(**values: object) -> TenderPositionsResponse:
        calls.append(str(values["operation"]))
        raise LlmMalformedResponseError("LLM response did not include choices")

    client.json_call = fake_json_call  # type: ignore[method-assign]

    response = client.extract_products("small tender text", [])

    assert response.products == []
    assert calls == ["extract_tender_products"]
    assert any("skipped chunk retry" in item for item in response.warnings)


def test_large_deterministic_product_list_skips_llm_product_extraction() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)

    def fail_json_call(**_: object) -> TenderPositionsResponse:
        raise AssertionError("LLM should not be called for large deterministic lists")

    client.json_call = fail_json_call  # type: ignore[method-assign]
    response = client.extract_products(
        "long tender text",
        [
            {"product": f"Product {index}", "quantity": 1, "unit": "pcs"}
            for index in range(25)
        ],
    )

    assert response.products == []
    assert any("skipped full-text LLM product extraction" in item for item in response.warnings)


def test_trusted_deterministic_spreadsheet_positions_skip_llm_product_extraction() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)

    def fail_json_call(**_: object) -> TenderPositionsResponse:
        raise AssertionError("LLM should not be called for trusted spreadsheet rows")

    client.json_call = fail_json_call  # type: ignore[method-assign]
    response = client.extract_products(
        "spreadsheet-only tender text",
        [{"product": "Product 1", "quantity": 1, "unit": "pcs"}],
        trust_deterministic=True,
    )

    assert response.products == []
    assert any("source of truth" in item for item in response.warnings)


def test_large_product_text_skips_llm_product_extraction_without_chunks() -> None:
    client = object.__new__(LlmClient)
    client.settings = SimpleNamespace(max_product_text_chars=100_000)

    def fail_json_call(**_: object) -> TenderPositionsResponse:
        raise AssertionError("LLM should not be called for oversized product extraction text")

    client.json_call = fail_json_call  # type: ignore[method-assign]

    response = client.extract_products(
        "\n".join(
            f"Строка {index}: A: {index} | B: Товар {index} | D: шт | E: 1"
            for index in range(1, 5_001)
        ),
        [],
    )

    assert response.products == []
    assert any("too large for safe single-call" in item for item in response.warnings)


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
