from app.models import DecisionReason, LlmDecision, NormalizedJob
from app.services.decision import (
    ASSORTMENT_REASON,
    DOCUMENTATION_REASON,
    MARKET_RESEARCH_REASON,
    PAYMENT_DELAY_REASON,
    PRICE_REASON,
    REMOTE_TERRITORY_REASON,
    HardReason,
    apply_final_decision,
    build_decision_prompt,
    calculate_hard_reasons,
)


def job(remaining_days: float | None = 5, report_id: int = 1) -> NormalizedJob:
    return NormalizedJob(
        job_record_key="daily:b:1:1",
        batch_id="b",
        report_id=report_id,
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


def test_hard_rejection_has_priority_over_counterparty_work() -> None:
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
    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == PRICE_REASON
    assert "Дополнительная информация по контрагенту: нет контрагента" in fields["tenderStatusNote"]
    assert decision["counterpartyRequiresWork"] is True
    assert decision["counterpartyAdvisoryOnly"] is True

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


def test_counterparty_work_is_used_when_tender_would_otherwise_be_approved() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=[],
        counterparty_lookup={"status": "not_found", "reason": "ИНН/КПП не найдены в IPro"},
        llm_decision=LlmDecision(decision="approve"),
    )

    assert fields["tenderStatus"] == "Проработка контрагента"
    assert fields["tenderStatusReason"] == "Прочее"
    assert decision["counterpartyRequiresWork"] is True
    assert decision["counterpartyAdvisoryOnly"] is False


def test_llm_rejection_has_priority_over_counterparty_work() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=[],
        counterparty_lookup={"status": "not_found", "reason": "контрагент отсутствует"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=REMOTE_TERRITORY_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=REMOTE_TERRITORY_REASON,
                    evidence="Место поставки: Республика Саха (Якутия)",
                    confidence="high",
                )
            ],
        ),
    )

    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == REMOTE_TERRITORY_REASON
    assert "Дополнительная информация по контрагенту: контрагент отсутствует" in fields["tenderStatusNote"]
    assert decision["counterpartyAdvisoryOnly"] is True


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


def test_seldon_404_is_deterministic_missing_documentation_rejection() -> None:
    reasons, checks = calculate_hard_reasons(
        job(),
        {},
        product_check(total=1),
        "",
        document_context={
            "apiCode": 404,
            "apiDescription": "По запрошенному идентификатору закупки отсутствует документация",
            "documentsFound": 0,
            "documentationMissing": True,
        },
    )

    assert DOCUMENTATION_REASON in [reason.reason for reason in reasons]
    assert checks["documentationCheck"]["automaticApprovalAllowed"] is False


def test_unavailable_documents_are_rejected_with_diagnostic_note() -> None:
    documentation_note = (
        "Seldon выдал ссылки на 2 документ(ов), но пригодный текст документации "
        "не получен. Пустые файлы: specification.xlsx."
    )
    reasons, _ = calculate_hard_reasons(
        job(report_id=3),
        {},
        product_check(total=1),
        "",
        document_context={
            "apiCode": 200,
            "documentsFound": 2,
            "documentationMissing": True,
            "documentationUnavailable": True,
            "documentationNote": documentation_note,
        },
    )

    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=reasons,
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
        report_id=3,
    )

    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == DOCUMENTATION_REASON
    assert documentation_note in fields["tenderStatusNote"]
    assert decision["reason"] == DOCUMENTATION_REASON


def test_missing_products_or_coverage_cannot_be_approved() -> None:
    empty_check = product_check(total=0, coveragePercent=None)
    reasons, _ = calculate_hard_reasons(job(), {}, empty_check, "")
    assert DOCUMENTATION_REASON in [reason.reason for reason in reasons]

    fields, _, _ = apply_final_decision(
        fields={},
        meta={},
        product_check=empty_check,
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )
    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == DOCUMENTATION_REASON


def test_assortment_is_primary_when_it_is_the_only_rejection_reason() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            hardReject=True,
            coveragePercent=33.33,
            summary="Покрытие ассортимента 33,33% (2 из 6).",
        ),
        hard_reasons=[HardReason(ASSORTMENT_REASON, "Покрытие 33,33%", 5)],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )

    assert fields["tenderStatusReason"] == ASSORTMENT_REASON
    assert "Покрытие ассортимента 33,33%" in fields["tenderStatusNote"]
    assert "Дополнительные подтверждённые причины" not in fields["tenderStatusNote"]
    assert decision["reasonOrigin"] == "deterministic"


def test_non_assortment_hard_reason_has_priority_and_assortment_moves_to_note() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            hardReject=True,
            coveragePercent=33.33,
            summary="Покрытие ассортимента 33,33% (2 из 6).",
        ),
        # Deliberately unsorted: final decision must preserve numeric priorities.
        hard_reasons=[
            HardReason("Коммерческие условия. Поставка в удаленные территории", "Якутия", 40),
            HardReason(ASSORTMENT_REASON, "Покрытие 33,33%", 5),
            HardReason(PRICE_REASON, "НМЦК: 900 000 руб.", 20),
        ],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )

    assert fields["tenderStatusReason"] == PRICE_REASON
    assert f"Основная причина отказа: {PRICE_REASON}." in fields["tenderStatusNote"]
    assert f"{ASSORTMENT_REASON} — Покрытие 33,33%" in fields["tenderStatusNote"]
    assert "Поставка в удаленные территории — Якутия" in fields["tenderStatusNote"]
    assert [item["reason"] for item in decision["additionalReasons"]] == [
        ASSORTMENT_REASON,
        "Коммерческие условия. Поставка в удаленные территории",
    ]


def test_llm_reason_overrides_only_assortment_and_all_other_reasons_are_noted() -> None:
    remote_reason = "Коммерческие условия. Поставка в удаленные территории"
    works_reason = "Номенклатура. Поставка с работами"
    fields, meta, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            hardReject=True,
            coveragePercent=33.33,
            summary="Покрытие ассортимента 33,33% (2 из 6).",
        ),
        hard_reasons=[HardReason(ASSORTMENT_REASON, "Покрытие 33,33%", 5)],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=remote_reason,
            detectedReasons=[
                DecisionReason(
                    reason=remote_reason,
                    evidence="Место поставки: Республика Саха",
                    confidence="high",
                ),
                DecisionReason(
                    reason=works_reason,
                    evidence="Монтаж выполняет поставщик",
                    confidence="medium",
                ),
            ],
            confidence="high",
        ),
    )

    assert fields["tenderStatusReason"] == remote_reason
    assert f"{ASSORTMENT_REASON} — Покрытие 33,33%" in fields["tenderStatusNote"]
    assert f"{works_reason} — Монтаж выполняет поставщик" in fields["tenderStatusNote"]
    assert decision["reasonOrigin"] == "llm_alternative_over_assortment"
    assert meta["tenderStatusReason"]["source"] == (
        "LLM: альтернативная причина при обязательном отказе по ассортименту"
    )


def test_decision_prompt_requires_full_analysis_after_assortment_rejection() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[HardReason(ASSORTMENT_REASON, "Покрытие 33,33%", 5)],
        checks={},
        product_check=product_check(hardReject=True, coveragePercent=33.33),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
    )

    assert "обязательно проверь все остальные причины справочника" in prompt
    assert "detectedReasons должен содержать полный список" in prompt
    assert "Не останавливай проверку после анализа товарного ассортимента" in prompt


def test_all_remote_territories_trigger_deterministic_rejection() -> None:
    territory_texts = [
        "Место поставки: Калининградская область.",
        "Место поставки: Республика Дагестан.",
        "Место поставки: Республика Саха (Якутия).",
    ]

    for text in territory_texts:
        reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)
        assert REMOTE_TERRITORY_REASON in [item.reason for item in reasons]


def test_similar_region_name_does_not_trigger_remote_territory_rule() -> None:
    reasons, _ = calculate_hard_reasons(
        job(),
        {},
        product_check(),
        "Место поставки: Сахалинская область.",
    )

    assert REMOTE_TERRITORY_REASON not in [item.reason for item in reasons]


def test_decision_prompt_lists_all_remote_territories() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[],
        checks={},
        product_check=product_check(),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
    )

    assert "Калининград/Калининградская область" in prompt
    assert "Республика Дагестан" in prompt
    assert "Республика Саха (Якутия)" in prompt


def test_payment_delay_rejects_at_ninety_days_inclusive() -> None:
    payment_terms = [
        "Оплата будет произведена в течение 90 дней после поставки.",
        "Оплата производится в течение 90 рабочих дней.",
        "Оплата производится в течение 90 календарных дней.",
        "Предусмотрена отсрочка платежа 90 дней.",
    ]

    for text in payment_terms:
        reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)
        assert PAYMENT_DELAY_REASON in [item.reason for item in reasons]


def test_payment_delay_below_ninety_days_does_not_reject() -> None:
    reasons, _ = calculate_hard_reasons(
        job(),
        {},
        product_check(),
        "Оплата будет произведена в течение 89 дней после поставки.",
    )

    assert PAYMENT_DELAY_REASON not in [item.reason for item in reasons]


def test_decision_prompt_uses_ninety_day_payment_threshold() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[],
        checks={},
        product_check=product_check(),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
    )

    assert "при 90 днях и более (`>= 90`)" in prompt
    assert "к рабочим, календарным и дням без уточнения типа" in prompt


def test_market_research_rejects_only_commercial_tenders() -> None:
    text = "Предмет процедуры: маркетинговое исследование рынка электротехнической продукции."
    documents_present = {
        "apiCode": 200,
        "apiDescription": "OK",
        "documentsFound": 2,
        "documentationMissing": False,
    }

    for report_id in (1, 2):
        reasons, checks = calculate_hard_reasons(
            job(report_id=report_id),
            {},
            product_check(total=1),
            text,
            document_context=documents_present,
        )
        assert MARKET_RESEARCH_REASON not in [reason.reason for reason in reasons]
        assert checks["marketResearchCheck"]["rejectionApplicable"] is False

    reasons, checks = calculate_hard_reasons(
        job(report_id=3),
        {},
        product_check(total=1),
        text,
        document_context=documents_present,
    )
    assert MARKET_RESEARCH_REASON in [reason.reason for reason in reasons]
    assert checks["marketResearchCheck"]["triggered"] is True


def test_223_without_documents_rejects_by_documentation_not_market_research() -> None:
    reasons, _ = calculate_hard_reasons(
        job(report_id=1),
        {},
        product_check(total=1),
        "Маркетинговое исследование рынка.",
        document_context={
            "apiCode": 404,
            "apiDescription": "документация отсутствует",
            "documentsFound": 0,
            "documentationMissing": True,
        },
    )
    reason_names = [reason.reason for reason in reasons]

    assert DOCUMENTATION_REASON in reason_names
    assert MARKET_RESEARCH_REASON not in reason_names


def test_llm_cannot_restore_market_research_reason_for_223_or_44_fz() -> None:
    llm_decision = LlmDecision(
        decision="reject",
        primaryReason=MARKET_RESEARCH_REASON,
        detectedReasons=[
            DecisionReason(
                reason=MARKET_RESEARCH_REASON,
                evidence="Маркетинговое исследование",
                confidence="high",
            )
        ],
    )

    for report_id in (1, 2):
        fields, _, decision = apply_final_decision(
            fields={},
            meta={},
            product_check=product_check(total=1),
            hard_reasons=[],
            counterparty_lookup={"status": "matched"},
            llm_decision=llm_decision,
            report_id=report_id,
        )
        assert fields["tenderStatus"] == "Согласовано КУ ЦП"
        assert "tenderStatusReason" not in fields
        assert decision["llmReasonCandidates"] == []
        assert decision["marketResearchReasonSuppressed"] is True


def test_prompt_excludes_market_research_from_allowed_reasons_for_223() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[],
        checks={},
        product_check=product_check(total=1),
        all_text="Маркетинговое исследование",
        maximum_text_chars=10_000,
        report_id=1,
    )
    allowed_reasons = prompt.split("Правила:", 1)[0]

    assert MARKET_RESEARCH_REASON not in allowed_reasons
    assert "применяется только для коммерческих закупок (reportId=3)" in prompt
