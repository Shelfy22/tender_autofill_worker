from app.models import DecisionReason, LlmDecision, NormalizedJob
from app.services.decision import (
    CONSIGNMENT_REASON,
    COVERAGE_REASON,
    DELIVERY_DEADLINE_REASON,
    DOCUMENTATION_REASON,
    HIGH_VOLTAGE_REASON,
    MARKET_RESEARCH_REASON,
    ORGANIZER_CANCELLATION_REASON,
    PAYMENT_DEPENDENCY_REASON,
    PAYMENT_DELAY_REASON,
    PRICE_REASON,
    REPAIR_KIT_REASON,
    REMOTE_TERRITORY_REASON,
    SUPPLY_WORK_REASON,
    HardReason,
    apply_final_decision,
    build_decision_prompt,
    calculate_hard_reasons,
)


def _reason_names(reasons: list[HardReason]) -> set[str]:
    return {item.reason for item in reasons}


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
        "coverageApproved": True,
        "supplyValueHardReject": False,
        "supplyValueThresholdApplicable": False,
        "priceEvaluationComplete": False,
        "supplyTotalPriceRub": 0,
        "summary": "Покрытие 100%",
    }
    value.update(overrides)
    return value


def test_conditional_mounting_heading_is_not_supply_with_works() -> None:
    text = (
        "Требования по сопутствующему монтажу "
        "(если монтаж осуществляется поставщиком). Дополнительные требования не указаны."
    )

    reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)

    assert SUPPLY_WORK_REASON not in _reason_names(reasons)


def test_optional_advance_cap_is_not_payment_dependency() -> None:
    text = (
        "Авансирование может быть предусмотрено по соглашению сторон. "
        "Размер аванса не более суммы, полученной от Госзаказчика."
    )

    reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)

    assert PAYMENT_DEPENDENCY_REASON not in _reason_names(reasons)


def test_direct_payment_after_state_customer_is_rejected() -> None:
    text = (
        "Оплата поставленного товара производится только после получения денежных "
        "средств от Госзаказчика."
    )

    reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)

    assert PAYMENT_DEPENDENCY_REASON in _reason_names(reasons)


def test_participant_refusal_from_postqualification_is_not_organizer_cancellation() -> None:
    text = "Участник отказался от проведения постквалификации и дальнейшего участия."

    reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)

    assert ORGANIZER_CANCELLATION_REASON not in _reason_names(reasons)


def test_explicit_procurement_cancellation_is_detected() -> None:
    text = "Организатор отказался от проведения закупки."

    reasons, _ = calculate_hard_reasons(job(), {}, product_check(), text)

    assert ORGANIZER_CANCELLATION_REASON in _reason_names(reasons)


def test_substation_voltage_in_housing_module_name_is_not_product_voltage() -> None:
    check = product_check(
        details=[
            {
                "positionIndex": 1,
                "sourceProduct": "Жилой модуль для ПС 500/330/220 кВ",
                "productQuery": "Модульное здание для подстанции 500 кВ",
                "sourceRequirements": "",
                "sourceEvidence": "",
            }
        ]
    )

    reasons, checks = calculate_hard_reasons(job(), {}, check, "")

    assert HIGH_VOLTAGE_REASON not in _reason_names(reasons)
    assert checks["highVoltageCheck"]["triggered"] is False


def test_initial_price_missing_or_zero_does_not_reject() -> None:
    for value in (None, "", 0, "0 руб."):
        reasons, _ = calculate_hard_reasons(
            job(), {"initialPrice": value}, product_check(), ""
        )
        assert PRICE_REASON not in [reason.reason for reason in reasons]


def test_positive_initial_price_below_one_million_rejects() -> None:
    for report_id in (1, 2):
        reasons, _ = calculate_hard_reasons(
            job(report_id=report_id),
            {"initialPrice": "999 999,00 рублей"},
            product_check(),
            "",
        )
        assert PRICE_REASON in [reason.reason for reason in reasons]


def test_commercial_initial_price_does_not_drive_one_million_rule() -> None:
    reasons, checks = calculate_hard_reasons(
        job(report_id=3),
        {"initialPrice": "100 000,00 рублей"},
        product_check(),
        "",
    )

    assert PRICE_REASON not in [reason.reason for reason in reasons]
    assert checks["priceThresholdCheck"]["mode"] == "calculated_qdrant_price_times_quantity"


def test_calculated_supply_price_reject_requires_complete_evaluation() -> None:
    complete, _ = calculate_hard_reasons(
        job(report_id=3),
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
        job(report_id=3),
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


def test_calculated_supply_price_is_informational_for_223_and_44() -> None:
    calculated_below_threshold = product_check(
        supplyValueHardReject=True,
        supplyValueThresholdApplicable=True,
        priceEvaluationComplete=True,
        supplyTotalPriceRub=13_830.33,
    )

    for report_id in (1, 2):
        reasons, checks = calculate_hard_reasons(
            job(report_id=report_id),
            {"initialPrice": "2 000 000 рублей"},
            calculated_below_threshold,
            "",
        )
        assert PRICE_REASON not in [reason.reason for reason in reasons]
        assert checks["priceThresholdCheck"]["mode"] == "initial_price_only"
        assert checks["priceThresholdCheck"]["calculatedValueInformationalOnly"] is True


def test_expertise_mounting_costs_are_not_supply_with_work() -> None:
    text = (
        "Оплата услуг эксперта, экспертной организации, а также всех расходов, "
        "в том числе связанных с транспортировкой, монтажом (демонтажем) Товара "
        "для экспертизы, осуществляется Поставщиком."
    )

    reasons, _ = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(total=2),
        text,
    )

    assert SUPPLY_WORK_REASON not in [reason.reason for reason in reasons]


def test_direct_supplier_mounting_obligation_is_supply_with_work() -> None:
    text = "Поставщик обязан выполнить монтаж и пусконаладочные работы на объекте заказчика."

    reasons, _ = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(total=2),
        text,
    )

    matching = [reason for reason in reasons if reason.reason == SUPPLY_WORK_REASON]
    assert len(matching) == 1
    assert "Поставщик обязан выполнить монтаж" in matching[0].evidence


def test_acceptance_remedy_storage_is_not_consignment() -> None:
    false_contexts = (
        "Товар, от которого Покупатель отказался, принимается на ответственное хранение. "
        "Поставщик обязан вывезти Товар своими силами.",
        "Возврат или замена товара осуществляется за счет Поставщика. Расходы Покупателя "
        "на ответственное хранение подлежат возмещению Поставщиком.",
        "При поступлении Товара без сопроводительных документов Товар принимается на "
        "ответственное хранение до момента поступления документов.",
        "На время внешней экспертизы качества товар помещается на временное ответственное "
        "хранение до получения экспертного заключения.",
    )

    for text in false_contexts:
        reasons, checks = calculate_hard_reasons(
            job(),
            {"initialPrice": 2_000_000},
            product_check(total=1),
            text,
        )
        assert CONSIGNMENT_REASON not in [item.reason for item in reasons], text
        assert checks["consignmentCheck"]["triggered"] is False


def test_direct_consignment_storage_triggers_reason() -> None:
    text = (
        "Товар передается на консигнацию и размещается на консигнационном складе "
        "Покупателя за счет Поставщика до реализации."
    )

    reasons, checks = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(total=1),
        text,
    )

    matching = [item for item in reasons if item.reason == CONSIGNMENT_REASON]
    assert len(matching) == 1
    assert "консигнационном складе" in matching[0].evidence
    assert checks["consignmentCheck"]["triggered"] is True


def test_llm_cannot_reintroduce_storage_after_missing_documents_as_consignment() -> None:
    evidence = (
        "При поступлении товара без документов товар принимается на ответственное "
        "хранение до момента поступления документов за счет Поставщика."
    )
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=CONSIGNMENT_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=CONSIGNMENT_REASON,
                    evidence=evidence,
                    confidence="high",
                )
            ],
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


def test_zip_in_main_product_completeness_is_not_repair_kit() -> None:
    main_product = "Маркер по металлу электроискровой ЭИМ"
    text = (
        f"{main_product}. Комплектность: источник питания — 1 шт.; "
        "ЗИП: сменные наконечники — 3 шт.; предохранитель 2А — 1 шт."
    )
    reasons, checks = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(
            total=1,
            details=[
                {
                    "positionIndex": 1,
                    "sourceProduct": main_product,
                    "productQuery": main_product,
                }
            ],
        ),
        text,
    )

    assert REPAIR_KIT_REASON not in [reason.reason for reason in reasons]
    assert checks["repairKitCheck"]["triggered"] is False


def test_repair_kit_product_position_triggers_reason() -> None:
    product_name = "Комплект ЗИП для трансформатора ТМГ"
    reasons, checks = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(
            total=1,
            details=[
                {
                    "positionIndex": 1,
                    "sourceProduct": product_name,
                    "productQuery": product_name,
                }
            ],
        ),
        "",
    )

    matching = [reason for reason in reasons if reason.reason == REPAIR_KIT_REASON]
    assert len(matching) == 1
    assert product_name in matching[0].evidence
    assert checks["repairKitCheck"]["triggered"] is True


def test_product_made_from_customer_layout_in_requirements_triggers_reason() -> None:
    requirements = (
        "Световой короб должен изготавливаться в соответствии с макетом (эскизом) "
        "Заказчика, включая фигурную резку основания корпуса из АКП."
    )
    reasons, checks = calculate_hard_reasons(
        job(),
        {"initialPrice": 2_000_000},
        product_check(
            total=1,
            details=[
                {
                    "positionIndex": 1,
                    "sourceProduct": "короб э/ф 1-стор. логотип 765x630x60 мм",
                    "productQuery": "короб э/ф 1-стор. логотип 765x630x60 мм",
                    "sourceRequirements": requirements,
                }
            ],
        ),
        requirements,
    )

    matching = [item for item in reasons if item.reason == REPAIR_KIT_REASON]
    assert len(matching) == 1
    assert "Позиция 1, требования" in matching[0].evidence
    assert "макетом (эскизом)" in matching[0].evidence
    assert checks["repairKitCheck"]["triggered"] is True


def test_llm_zip_reason_is_suppressed_for_main_product_completeness() -> None:
    main_product = "Маркер по металлу электроискровой ЭИМ"
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            total=1,
            details=[
                {
                    "positionIndex": 1,
                    "sourceProduct": main_product,
                    "productQuery": main_product,
                }
            ],
        ),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=REPAIR_KIT_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=REPAIR_KIT_REASON,
                    evidence=(
                        "Маркер поставляется в комплекте; ЗИП включает "
                        "сменные наконечники и предохранитель."
                    ),
                    confidence="high",
                )
            ],
            confidence="high",
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


def test_llm_cannot_reintroduce_delivery_deadline_without_validated_date() -> None:
    evidence = "Договор действует по 30.03.2028."
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=DELIVERY_DEADLINE_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=DELIVERY_DEADLINE_REASON,
                    evidence=evidence,
                    confidence="high",
                )
            ],
            confidence="high",
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


def test_llm_supply_work_reason_is_suppressed_for_expertise_context() -> None:
    evidence = (
        "Расходы, связанные с транспортировкой, монтажом (демонтажем) товара "
        "для экспертизы, осуществляются поставщиком."
    )
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(total=2),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=SUPPLY_WORK_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=SUPPLY_WORK_REASON,
                    evidence=evidence,
                    confidence="high",
                )
            ],
            confidence="high",
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


def test_llm_cannot_reintroduce_deterministic_one_million_reason() -> None:
    llm_decision = LlmDecision(
        decision="reject",
        primaryReason=PRICE_REASON,
        detectedReasons=[
            DecisionReason(
                reason=PRICE_REASON,
                evidence="Расчётная сумма 13 830,33 руб.",
                confidence="high",
            )
        ],
        note="Сумма ниже порога.",
        confidence="high",
    )

    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=llm_decision,
        report_id=1,
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


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
            primaryReason=SUPPLY_WORK_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=SUPPLY_WORK_REASON,
                    evidence="Поставщик обязан выполнить монтаж на объекте заказчика.",
                    confidence="high",
                )
            ],
        ),
    )

    assert fields["tenderStatus"] == "Отказано КУ ЦП"
    assert fields["tenderStatusReason"] == SUPPLY_WORK_REASON
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


def test_coverage_reason_is_primary_when_it_is_the_only_rejection_reason() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            hardReject=True,
            coveragePercent=33.33,
            summary="Покрытие ассортимента 33,33% (2 из 6).",
        ),
        hard_reasons=[HardReason(COVERAGE_REASON, "Покрытие 33,33%", 5)],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )

    assert fields["tenderStatusReason"] == COVERAGE_REASON
    assert "Покрытие ассортимента 33,33%" in fields["tenderStatusNote"]
    assert "Дополнительные подтверждённые причины" not in fields["tenderStatusNote"]
    assert decision["reasonOrigin"] == "deterministic"


def test_non_coverage_hard_reason_has_priority_and_coverage_moves_to_note() -> None:
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
            HardReason(COVERAGE_REASON, "Покрытие 33,33%", 5),
            HardReason(PRICE_REASON, "НМЦК: 900 000 руб.", 20),
        ],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(decision="approve"),
    )

    assert fields["tenderStatusReason"] == PRICE_REASON
    assert f"Основная причина отказа: {PRICE_REASON}." in fields["tenderStatusNote"]
    assert f"{COVERAGE_REASON} — Покрытие 33,33%" in fields["tenderStatusNote"]
    assert "Поставка в удаленные территории — Якутия" in fields["tenderStatusNote"]
    assert [item["reason"] for item in decision["additionalReasons"]] == [
        COVERAGE_REASON,
        "Коммерческие условия. Поставка в удаленные территории",
    ]


def test_llm_reason_overrides_only_coverage_and_all_other_reasons_are_noted() -> None:
    works_reason = SUPPLY_WORK_REASON
    military_reason = "Номенклатура. Военная приемка"
    fields, meta, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(
            hardReject=True,
            coveragePercent=33.33,
            summary="Покрытие ассортимента 33,33% (2 из 6).",
        ),
        hard_reasons=[HardReason(COVERAGE_REASON, "Покрытие 33,33%", 5)],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=works_reason,
            detectedReasons=[
                DecisionReason(
                    reason=works_reason,
                    evidence="Поставщик обязан выполнить монтаж на объекте заказчика",
                    confidence="high",
                ),
                DecisionReason(
                    reason=military_reason,
                    evidence="Приемка продукции проводится военным представительством",
                    confidence="medium",
                ),
            ],
            confidence="high",
        ),
    )

    assert fields["tenderStatusReason"] == works_reason
    assert f"{COVERAGE_REASON} — Покрытие 33,33%" in fields["tenderStatusNote"]
    assert (
        f"{military_reason} — Приемка продукции проводится военным представительством"
        in fields["tenderStatusNote"]
    )
    assert decision["reasonOrigin"] == "llm_alternative_over_coverage"
    assert meta["tenderStatusReason"]["source"] == (
        "LLM: альтернативная причина при обязательном отказе по комплектованию лота"
    )


def test_decision_prompt_requires_full_analysis_after_coverage_rejection() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[HardReason(COVERAGE_REASON, "Покрытие 33,33%", 5)],
        checks={},
        product_check=product_check(hardReject=True, coveragePercent=33.33),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
    )

    assert "обязательно проверь все остальные причины справочника" in prompt
    assert "detectedReasons должен содержать полный список" in prompt
    assert "Не останавливай проверку после анализа товарного ассортимента" in prompt


def test_decision_prompt_distinguishes_supply_work_from_expertise() -> None:
    prompt = build_decision_prompt(
        fields={},
        hard_reasons=[],
        checks={},
        product_check=product_check(total=1),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
    )

    assert "прямой обязанности поставщика" in prompt
    assert "монтаж/демонтаж товара только для экспертизы" in prompt
    assert "Не считать консигнацией ответственное/временное хранение" in prompt
    assert "сама извлечённая товарная позиция" in prompt
    assert "sourceRequirements именно этой товарной позиции" in prompt
    assert "лишь входят в его комплектность" in prompt
    assert "Регион регистрации" in prompt


def test_decision_prompt_excludes_actual_cost_reason_from_llm_options() -> None:
    prompt = build_decision_prompt(
        fields={"initialPrice": 1_300_000},
        hard_reasons=[],
        checks={},
        product_check=product_check(quantityAdjustedTotalRub=2_000_000),
        all_text="Документация тендера",
        maximum_text_chars=10_000,
        report_id=1,
    )

    assert "Коммерческие условия. НМЦК менее фактической стоимости" not in prompt


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


def test_customer_region_alone_does_not_trigger_remote_territory_rule() -> None:
    text = (
        'Организатор: ПАО "Якутскэнерго". '
        "Регион заказчика/организатора: Республика Саха (Якутия)."
    )

    reasons, checks = calculate_hard_reasons(
        job(),
        {},
        product_check(),
        text,
    )

    assert REMOTE_TERRITORY_REASON not in [item.reason for item in reasons]
    assert checks["remoteTerritoryCheck"]["triggered"] is False


def test_structured_delivery_note_triggers_remote_territory_rule() -> None:
    reasons, checks = calculate_hard_reasons(
        job(),
        {"deliveryNote": "Республика Саха (Якутия), г. Якутск, склад заказчика"},
        product_check(),
        "Организатор зарегистрирован в Москве.",
    )

    matching = [item for item in reasons if item.reason == REMOTE_TERRITORY_REASON]
    assert len(matching) == 1
    assert "deliveryNote" in matching[0].evidence
    assert checks["remoteTerritoryCheck"]["triggered"] is True


def test_llm_cannot_reintroduce_remote_reason_from_customer_region() -> None:
    fields, _, decision = apply_final_decision(
        fields={"customerRegion": "Республика Саха (Якутия)"},
        meta={},
        product_check=product_check(total=1),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=REMOTE_TERRITORY_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=REMOTE_TERRITORY_REASON,
                    evidence=(
                        "Регион деятельности заказчика ПАО Якутскэнерго — "
                        "Республика Саха (Якутия)."
                    ),
                    confidence="high",
                )
            ],
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []


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


def _voltage_product_check(
    source_product: str,
    *,
    evidence: str = "",
    requirements: str = "",
) -> dict[str, object]:
    return product_check(
        details=[
            {
                "positionIndex": 1,
                "sourceProduct": source_product,
                "productQuery": source_product,
                "sourceEvidence": evidence,
                "sourceRequirements": requirements,
            }
        ]
    )


def test_apparent_power_kva_does_not_trigger_35_kv_reason() -> None:
    evidence = (
        "Мощность: 1000 кВ∙А. "
        "Напряжение ВН – 10 кВ, напряжение НН – 0,4 кВ."
    )
    reasons, checks = calculate_hard_reasons(
        job(),
        {},
        _voltage_product_check(
            "Трансформатор ТМГ-1000/10/0,4 кВ",
            evidence=evidence,
        ),
        evidence,
    )

    assert HIGH_VOLTAGE_REASON not in [item.reason for item in reasons]
    assert checks["highVoltageCheck"]["triggered"] is False
    assert all(
        item["voltageKv"] < 35
        for item in checks["highVoltageCheck"]["parsedVoltages"]
    )


def test_power_units_are_never_treated_as_voltage() -> None:
    power_values = (
        "35 кВА",
        "35 кВ·А",
        "35 кВ∙А",
        "35 кВ А",
        "35 kVA",
        "35 кВт",
    )
    for value in power_values:
        reasons, _ = calculate_hard_reasons(
            job(),
            {},
            _voltage_product_check(f"Трансформатор, мощность {value}"),
            value,
        )
        assert HIGH_VOLTAGE_REASON not in [item.reason for item in reasons], value


def test_compound_35_10_kv_in_product_position_triggers_reason() -> None:
    reasons, checks = calculate_hard_reasons(
        job(),
        {},
        _voltage_product_check("Трансформатор 35/10 кВ"),
        "",
    )

    reason = next(item for item in reasons if item.reason == HIGH_VOLTAGE_REASON)
    assert "35/10" in reason.evidence
    assert checks["highVoltageCheck"]["triggered"] is True
    assert [
        item["voltageKv"] for item in checks["highVoltageCheck"]["parsedVoltages"]
    ] == [35.0, 10.0]


def test_voltage_in_contextual_position_evidence_triggers_reason() -> None:
    reasons, _ = calculate_hard_reasons(
        job(),
        {},
        _voltage_product_check(
            "Трансформатор",
            evidence="Класс напряжения: 110 кВ.",
        ),
        "",
    )

    assert HIGH_VOLTAGE_REASON in [item.reason for item in reasons]


def test_unrelated_voltage_in_combined_text_does_not_trigger_reason() -> None:
    reasons, checks = calculate_hard_reasons(
        job(),
        {},
        _voltage_product_check("Трансформатор 10/0,4 кВ"),
        "Справочная информация о сети 110 кВ.",
    )

    assert HIGH_VOLTAGE_REASON not in [item.reason for item in reasons]
    assert checks["highVoltageCheck"]["triggered"] is False


def test_llm_cannot_reintroduce_deterministic_35_kv_reason() -> None:
    fields, _, decision = apply_final_decision(
        fields={},
        meta={},
        product_check=product_check(),
        hard_reasons=[],
        counterparty_lookup={"status": "matched"},
        llm_decision=LlmDecision(
            decision="reject",
            primaryReason=HIGH_VOLTAGE_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=HIGH_VOLTAGE_REASON,
                    evidence="Неподтвержденное предположение",
                    confidence="high",
                )
            ],
        ),
    )

    assert fields["tenderStatus"] == "Согласовано КУ ЦП"
    assert "tenderStatusReason" not in fields
    assert decision["llmReasonCandidates"] == []
