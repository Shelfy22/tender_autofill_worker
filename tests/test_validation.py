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
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(
                value="by_requests",
                confidence="high",
                source="ТЗ.docx",
                evidence="Поставка товара осуществляется по заявкам заказчика.",
            )
        }
    )
    fields, _, _ = validate_fields(
        job_with_lot(""),
        extracted,
        "Поставка товара осуществляется по заявкам заказчика.",
        [],
    )

    assert fields["deliveryType"] == "by_requests"
    assert "deliveryNote" not in fields


def test_direct_payment_term_fills_missing_llm_field() -> None:
    text = """
    Срок поставки товара составляет 28 календарных дней.
    Поставка товара осуществляется не позднее 30.04.2026.
    Отсрочка платежа составляет 10 рабочих дней.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["paymentDelayDays"] == 14
    assert meta["paymentDelayDays"]["confidence"] == "high"


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


def test_delivery_type_is_not_invented_from_document_regex() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""), None, "Поставка товара осуществляется по разнарядкам.", []
    )

    assert "deliveryType" not in fields


def test_direct_security_fallback_does_not_fill_warranty_columns() -> None:
    text = """
    Обеспечение заявки: не требуется.
    Размер обеспечения исполнения договора составляет 5% цены договора.
    Обеспечение гарантийных обязательств: 1%.
    Гарантийный срок составляет 24 месяца.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert "applicationSecurity" not in fields
    assert fields["contractSecurity"] == "5% цены договора"
    assert "warrantySecurity" not in fields
    assert "warrantyMonths" not in fields
    assert meta["contractSecurity"]["confidence"] == "high"


def test_contract_validity_date_does_not_fill_delivery_date() -> None:
    text = "Договор вступает в силу со дня подписания его Сторонами и действует до 30.03.2028."
    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert "deliveryDate" not in fields


def test_llm_delivery_deadline_is_preserved_without_regex_override() -> None:
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
    assert not warnings


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


def test_llm_payment_evidence_wins_over_regex_fallback() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "paymentDelayDays": FieldValue(
                value=30,
                confidence="high",
                source="Договор.docx",
                evidence="Оплата производится в течение 30 дней после приемки.",
            )
        }
    )

    fields, meta, _ = validate_fields(
        job_with_lot(""),
        extracted,
        "Оплата услуг перевозчика производится в течение 7 дней.",
        [],
    )

    assert fields["paymentDelayDays"] == 42
    assert meta["paymentDelayDays"]["source"] == "LLM evidence / отсрочка переведена по правилу 1.4"


def test_payment_delay_recognizes_payment_deadline_wording() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""),
        None,
        "Оплата товара производится в срок не более 90 рабочих дней со дня приемки.",
        [],
    )

    assert fields["paymentDelayDays"] == 126


def test_batch_delivery_moves_llm_term_to_batch_delivery_days() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(value="by_requests", confidence="high", source="ТЗ.docx", evidence="Поставка по заявкам"),
            "deliveryDays": FieldValue(value=7, confidence="high", source="ТЗ.docx", evidence="В течение 7 дней после заявки"),
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["deliveryType"] == "by_requests"
    assert fields["deliveryBatchDays"] == 7
    assert "deliveryDays" not in fields


def test_national_regime_is_not_overridden_by_global_document_regex() -> None:
    text = """
    Сведения о национальном режиме в соответствии с постановлением № 1875:
    по одним позициям преимущество, по другим ограничение, по отдельным позициям запрет.
    """

    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="restriction", confidence="high", source="Приложение 2", evidence="Ограничение. Приложение 2"
            )
        }
    )
    fields, meta, _ = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["nationalRegime"] == "restriction"
    assert meta["nationalRegime"]["source"] == "Приложение 2"


def test_missing_national_regime_remains_blank() -> None:
    fields, _, _ = validate_fields(job_with_lot(""), None, "Национальный режим: не найден.", [])

    assert "nationalRegime" not in fields


def test_batch_delivery_without_stated_term_remains_blank() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(value="by_requests", confidence="high", source="ТЗ.docx", evidence="Поставка партиями по заявкам заказчика"),
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert "deliveryBatchDays" not in fields


def test_unknown_lot_divisibility_defaults_to_indivisible() -> None:
    fields, meta, _ = validate_fields(job_with_lot(""), None, "", [])

    assert fields["lotDivisible"] == "no"
    assert meta["lotDivisible"]["source"] == "Правило автозаполнения / делимость не установлена"


def test_result_date_is_extracted_from_russian_document_date() -> None:
    text = "Дата подведения итогов закупки: «23» октября 2026 года (время московское)."

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["resultDate"] == "2026-10-23"
    assert fields["contractDate"] == "2026-11-12"


def test_result_date_is_extracted_from_consideration_and_evaluation_wording() -> None:
    text = (
        "Дата рассмотрения вторых частей заявок, оценки и сопоставления заявок "
        "(подведения итогов): не позднее «13» ноября 2026 г."
    )

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["resultDate"] == "2026-11-13"
