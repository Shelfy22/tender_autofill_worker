
import pytest

from app.models import CommercialConditionsResponse, DocumentAnalysisResponse, TenderConsolidationResponse
from app.services.commercial_conditions import active_commercial_fields, build_commercial_conditions_context
from app.services.field_policy import DISABLED_TENDER_FIELDS
from app.services.field_prompts import active_commercial_rules, active_consolidation_rules, document_rule
from app.services.llm import LlmClient, _response_schema


def test_commercial_review_retains_warranty_and_result_date_only():
    assert active_commercial_fields() == ("warrantyMonths", "resultDate")
    context = build_commercial_conditions_context(
        "Гарантийный срок: 24 месяца.\n\nДата подведения итогов: 12.12.2026.", []
    )
    assert "targets: warrantyMonths" in context
    assert "targets: resultDate" in context
    assert "targets: delivery" not in context
    assert "targets: paymentDelayDays" not in context
    rules = active_commercial_rules()
    assert "warrantyMonths" in rules and "resultDate" in rules
    assert not any(name in rules for name in DISABLED_TENDER_FIELDS)
    schema = _response_schema(CommercialConditionsResponse, strict=True)
    allowed = schema["$defs"]["DocumentFieldCandidate"]["properties"]["fieldName"]["enum"]
    assert allowed == ["warrantyMonths", "resultDate"]


@pytest.mark.parametrize("name", sorted(DISABLED_TENDER_FIELDS))
def test_one_field_can_be_reenabled_without_enabling_others(monkeypatch, name):
    import app.services.field_policy as policy

    monkeypatch.setattr(policy, "DISABLED_TENDER_FIELDS", DISABLED_TENDER_FIELDS - {name})
    schema = _response_schema(DocumentAnalysisResponse, strict=False)
    allowed = schema["$defs"]["DocumentFieldCandidate"]["properties"]["fieldName"]["enum"]
    assert name in allowed
    assert not (set(allowed) & policy.DISABLED_TENDER_FIELDS)
    if name in ("nationalRegime", "paymentDelayDays"):
        assert name in active_commercial_fields()


def test_document_and_consolidation_instructions_do_not_request_retired_fields():
    assert all(document_rule(name) == "" for name in DISABLED_TENDER_FIELDS)
    assert active_consolidation_rules() == ""
    client = object.__new__(LlmClient)
    captured = {}

    def call(**kwargs):
        captured.update(kwargs)
        return TenderConsolidationResponse()

    client.json_call = call
    client.consolidate_document_analysis([])
    prompt = captured["prompt"]
    assert not any(name in prompt for name in DISABLED_TENDER_FIELDS)
