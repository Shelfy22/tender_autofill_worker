from app.models import LlmDecision, NormalizedJob
from app.services.decision import (
    PRICE_REASON,
    apply_final_decision,
    calculate_hard_reasons,
)


def job(remaining_days: float | None = 5) -> NormalizedJob:
    return NormalizedJob(
        job_record_key="daily:b:1:1",
        batch_id="b",
        report_id=1,
        seldon_id="1",
        remaining_days=remaining_days,
        report_fields={},
        seldon_purchase={},
    )


def product_check(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "hardReject": False,
        "coveragePercent": 100.0,
        "supplyValueHardReject": False,
        "supplyValueThresholdApplicable": False,
        "priceEvaluationComplete": False,
        "supplyTotalPriceRub": 0,
        "summary": "Покрытие 100%",
    }
    value.update(overrides)
    return value


def test_initial_price_missing_or_zero_does_not_reject() -> None:
    for value in (None, "", 0, "0 руб."):
        reasons, _ = calculate_hard_reasons(
            job(), {"initialPrice": value}, product_check(), ""
        )
        assert PRICE_REASON not in [reason.reason for reason in reasons]


def test_positive_initial_price_below_one_million_rejects() -> None:
    reasons, _ = calculate_hard_reasons(
        job(), {"initialPrice": "999 999,00 рублей"}, product_check(), ""
    )
    assert PRICE_REASON in [reason.reason for reason in reasons]


def test_calculated_supply_price_reject_requires_complete_evaluation() -> None:
    complete, _ = calculate_hard_reasons(
        job(),
        {"initialPrice": 0},
        product_check(
            supplyValueHardReject=True,
            supplyValueThresholdApplicable=True,
            priceEvaluationComplete=True,
            supplyTotalPriceRub=999_999,
        ),
        "",
    )
    incomplete, _ = calculate_hard_reasons(
        job(),
        {"initialPrice": 0},
        product_check(
            supplyValueHardReject=False,
            supplyValueThresholdApplicable=False,
            priceEvaluationComplete=False,
            supplyTotalPriceRub=10,
        ),
        "",
    )
    assert PRICE_REASON in [reason.reason for reason in complete]
    assert PRICE_REASON not in [reason.reason for reason in incomplete]


def test_remaining_days_rule_is_strict() -> None:
    at_three, _ = calculate_hard_reasons(job(3), {}, product_check(), "")
    below_three, _ = calculate_hard_reasons(job(2.99), {}, product_check(), "")
    assert not any("менее 3" in reason.reason for reason in at_three)
    assert any("менее 3" in reason.reason for reason in below_three)


def test_negative_remaining_days_is_preserved_by_rule() -> None:
    reasons, _ = calculate_hard_reasons(job(-1), {}, product_check(), "")
    assert any("менее 3" in reason.reason for reason in reasons)


def test_final_status_priority_counterparty_then_hard_then_llm() -> None:
    hard, _ = calculate_hard_reasons(
        job(), {"initialPrice": 100}, product_check(), ""
    )
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(),
        hard_reasons=hard,
        counterparty_lookup={"status": "not_found", "reason": "нет контрагента"},
        llm_decision=LlmDecision(decision="approve"),
    )
    assert fields["tenderStatus"] == "Проработка контрагента"
    assert fields["tenderStatusReason"] == "Прочее"
    assert decision["counterpartyRequiresWork"] is True

    fields, _, _ = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(),
        hard_reasons=hard,
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )
    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == PRICE_REASON


def test_llm_approve_without_hard_reasons() -> None:
    fields, _, _ = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve", note="Критерии не найдены"),
    )
    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
