import json

import pytest

from app.models import ExtractedFieldsResponse
from app.services.llm import extract_json, extract_json_result


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
