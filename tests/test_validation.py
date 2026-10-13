from app.models import ExtractedFieldsResponse, FieldValue, NormalizedJob
from app.services.validation import validate_fields


def job_with_lot(value: str) -> NormalizedJob:
    return NormalizedJob(
        job_record_key="daily:b:1:1",
        batch_id="b",
        report_id=1,
        seldon_id="1",
        report_fields={"Лот делимый": value},
        seldon_purchase={},
    )


def test_daily_lot_divisible_column_is_authoritative() -> None:
    fields, meta, warnings = validate_fields(
        job_with_lot("Да"),
        None,
        "",
        [],
    )

    assert fields["lotDivisible"] == "yes"
    assert meta["lotDivisible"]["source"] == "Daily / колонка «Лот делимый»"
    assert "Лот делимый не заполнен: нет прямого evidence." not in warnings


def test_daily_indivisible_lot_column_is_authoritative() -> None:
    fields, meta, warnings = validate_fields(
        job_with_lot("Нет"),
        None,
        "",
        [],
    )

    assert fields["lotDivisible"] == "no"
    assert meta["lotDivisible"]["source"] == "Daily / колонка «Лот делимый»"
    assert "Лот делимый не заполнен: нет прямого evidence." not in warnings


def test_submission_deadline_is_taken_from_seldon_not_document_llm() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:1",
        batch_id="b",
        report_id=1,
        seldon_id="1",
        report_fields={"Дата окончания приёма заявок": "01.08.2026 12:30:00"},
        seldon_purchase={"endDate": "2026-08-07T09:00:00"},
    )
    extracted = ExtractedFieldsResponse(
        fields={
            "submissionDeadlineDate": FieldValue(
                value="2026-08-01",
                confidence="low",
                source="Документ.docx",
                evidence="Срок подачи до 01.08.2026",
            )
        }
    )

    fields, meta, _ = validate_fields(job, extracted, "", [])

    assert fields["submissionDeadlineDate"] == "2026-08-07"
    # Internal field keeps its established HH:MM representation; the final
    # CSV formatter adds seconds in the combined deadline column.
    assert fields["submissionDeadlineTime"] == "09:00"
    assert meta["submissionDeadlineDate"]["source"] == "Seldon/Daily / дата окончания подачи"


def test_batch_delivery_does_not_invent_generic_delivery_note() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""),
        None,
        "Поставка товара осуществляется по заявкам заказчика.",
        [],
    )

    assert fields["deliveryType"] == "by_requests"
    assert "deliveryNote" not in fields


def test_direct_delivery_and_payment_terms_fill_missing_llm_fields() -> None:
    text = """
    Срок поставки товара составляет 28 календарных дней.
    Поставка товара осуществляется не позднее 30.04.2026.
    Отсрочка платежа составляет 10 рабочих дней.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["deliveryDays"] == 28
    assert fields["deliveryDate"] == "2026-04-30"
    assert fields["paymentDelayDays"] == 14
    assert all(meta[key]["confidence"] == "high" for key in (
        "deliveryDays", "deliveryDate", "paymentDelayDays",
    ))


def test_document_result_date_calculates_contract_date() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "resultDate": FieldValue(
                value="2026-08-07",
                confidence="high",
                source="Извещение.docx",
                evidence="Дата подведения итогов закупки: 07.08.2026.",
            )
        }
    )

    fields, meta, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["resultDate"] == "2026-08-07"
    assert fields["contractDate"] == "2026-08-27"
    assert meta["contractDate"]["source"] == (
        "Расчёт / дата подведения итогов + 20 календарных дней"
    )


def test_delivery_type_uses_all_three_business_values() -> None:
    period, _, _ = validate_fields(
        job_with_lot(""), None, "Срок поставки товара составляет 15 дней.", []
    )
    by_requests, _, _ = validate_fields(
        job_with_lot(""), None, "Поставка товара осуществляется по разнарядкам.", []
    )
    by_date, _, _ = validate_fields(
        job_with_lot(""), None, "Поставка товара осуществляется не позднее 30.04.2026.", []
    )

    assert period["deliveryType"] == "during_period"
    assert by_requests["deliveryType"] == "by_requests"
    assert by_date["deliveryType"] == "single_date"


def test_direct_security_and_warranty_terms_fill_missing_llm_fields() -> None:
    text = """
    Обеспечение заявки: не требуется.
    Размер обеспечения исполнения договора составляет 5% цены договора.
    Обеспечение гарантийных обязательств: 1%.
    Гарантийный срок составляет 24 месяца.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert "applicationSecurity" not in fields
    assert fields["contractSecurity"] == "5% цены договора"
    assert fields["warrantySecurity"] == "1%"
    assert fields["warrantyMonths"] == "24"
    assert all(meta[key]["confidence"] == "high" for key in (
        "contractSecurity", "warrantySecurity", "warrantyMonths",
    ))


def test_contract_validity_date_fills_delivery_date_when_no_direct_deadline() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryDate": FieldValue(
                value="2028-03-30",
                confidence="medium",
                source="Прил.№4 ПД.docx",
                evidence=(
                    "Договор вступает в силу со дня подписания его Сторонами "
                    "и действует по 30.03.2028."
                ),
            )
        }
    )

    text = "Договор вступает в силу со дня подписания его Сторонами и действует до 30.03.2028."
    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["deliveryDate"] == "2028-03-30"
    assert meta["deliveryDate"]["source"] == (
        "Документы / дата окончания действия договора как срок поставки"
    )
    assert any("deliveryDate отброшен" in warning for warning in warnings)


def test_explicit_delivery_deadline_is_preserved() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryDate": FieldValue(
                value="2028-03-30",
                confidence="high",
                source="Техническое задание.docx",
                evidence="Срок поставки товара — до 30.03.2028.",
            )
        }
    )

    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["deliveryDate"] == "2028-03-30"
    assert meta["deliveryDate"]["source"] == "Техническое задание.docx"
    assert not any("deliveryDate отброшен" in warning for warning in warnings)


def test_payment_delay_uses_largest_variant_and_converts_working_days() -> None:
    text = """
    Для субъектов МСП оплата производится в течение 30 рабочих дней.
    Для остальных поставщиков отсрочка платежа составляет от 30 до 60 календарных дней.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["paymentDelayDays"] == 84
    assert meta["paymentDelayDays"]["confidence"] == "high"


def test_payment_delay_converts_payment_term_days() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""),
        None,
        "Оплата производится в течение 7 дней после поставки товара.",
        [],
    )

    assert fields["paymentDelayDays"] == 10


def test_payment_delay_recognizes_settlement_wording() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""),
        None,
        "Расчёты производятся в течение 30 рабочих дней после приемки товара.",
        [],
    )

    assert fields["paymentDelayDays"] == 42


def test_payment_delay_converts_llm_value_when_text_is_fragmented() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "paymentDelayDays": FieldValue(
                value=7,
                confidence="high",
                source="Договор.docx",
                evidence="Оплата производится в течение 7 дней после поставки.",
            )
        }
    )

    fields, meta, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["paymentDelayDays"] == 10
    assert meta["paymentDelayDays"]["source"] == "LLM evidence / отсрочка переведена по правилу 1.4"


def test_batch_delivery_moves_term_to_batch_delivery_days() -> None:
    text = "Поставка товара партиями по заявкам заказчика в течение 7 календарных дней."

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["deliveryType"] == "by_requests"
    assert fields["deliveryBatchDays"] == 7
    assert "deliveryDays" not in fields


def test_national_regime_uses_highest_priority_from_mixed_table() -> None:
    text = """
    Сведения о национальном режиме в соответствии с постановлением № 1875:
    по одним позициям преимущество, по другим ограничение, по отдельным позициям запрет.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["nationalRegime"] == "ban"
    assert meta["nationalRegime"]["source"] == "Документы / приоритет национального режима"


def test_national_regime_restriction_beats_preference() -> None:
    text = "Национальный режим: преимущество для части товаров и ограничение для другой части."

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["nationalRegime"] == "restriction"


def test_result_date_is_extracted_from_russian_document_date() -> None:
    text = "Дата подведения итогов закупки: «23» октября 2026 года (время московское)."

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["resultDate"] == "2026-10-23"
    assert fields["contractDate"] == "2026-11-12"
