from app.models import (
    ExtractedFieldsResponse,
    FieldValue,
    NormalizedJob,
    ParsedDocument,
    SpreadsheetRow,
    SpreadsheetTable,
)
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


def test_payment_delay_uses_largest_document_option_over_smaller_llm_value() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "paymentDelayDays": FieldValue(
                value=7,
                confidence="high",
                source="contract.docx",
                evidence="\u041e\u043f\u043b\u0430\u0442\u0430 \u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0435 7 \u0440\u0430\u0431\u043e\u0447\u0438\u0445 \u0434\u043d\u0435\u0439.",
            )
        }
    )
    text = (
        "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 1. \u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0441\u044f \u043d\u0435 \u043f\u043e\u0437\u0434\u043d\u0435\u0435 7 \u0440\u0430\u0431\u043e\u0447\u0438\u0445 \u0434\u043d\u0435\u0439. "
        "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 2. \u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0441\u044f \u043d\u0435 \u043f\u043e\u0437\u0434\u043d\u0435\u0435 30 \u0440\u0430\u0431\u043e\u0447\u0438\u0445 \u0434\u043d\u0435\u0439."
    )

    fields, meta, _ = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["paymentDelayDays"] == 42
    assert "30" in meta["paymentDelayDays"]["evidence"]


def test_batch_word_without_requests_does_not_confirm_by_requests_delivery() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(
                value="by_requests",
                confidence="high",
                source="contract.docx",
                evidence="\u041f\u043e\u0441\u0442\u0430\u0432\u0449\u0438\u043a \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u044b\u0432\u0430\u0435\u0442 \u0434\u0430\u0442\u0443 \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438 \u043a\u0430\u0436\u0434\u043e\u0439 \u043f\u0430\u0440\u0442\u0438\u0438.",
            )
        }
    )

    fields, _, warnings = validate_fields(job_with_lot(""), extracted, "", [])

    assert "deliveryType" not in fields
    assert any("\u043f\u0430\u0440\u0442\u0438\u0438" in warning for warning in warnings)


def test_unconfirmed_requests_with_direct_deadline_becomes_single_date() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(
                value="by_requests",
                confidence="high",
                source="contract.docx",
                evidence="\u0414\u0430\u0442\u0430 \u043a\u0430\u0436\u0434\u043e\u0439 \u043f\u0430\u0440\u0442\u0438\u0438 \u0441\u043e\u0433\u043b\u0430\u0441\u043e\u0432\u044b\u0432\u0430\u0435\u0442\u0441\u044f \u0441 \u043f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u0435\u043c.",
            ),
            "deliveryDate": FieldValue(
                value="2026-12-15",
                confidence="high",
                source="\u0422\u0417.docx",
                evidence="\u0421\u0440\u043e\u043a \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438: \u043d\u0435 \u043f\u043e\u0437\u0434\u043d\u0435\u0435 15.12.2026.",
            ),
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["deliveryType"] == "single_date"


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


def test_result_stage_uses_stage_end_date_from_long_notice_row() -> None:
    text = (
        "\u042d\u0442\u0430\u043f \u00ab\u041f\u043e\u0434\u0432\u0435\u0434\u0435\u043d\u0438\u0435 \u0438\u0442\u043e\u0433\u043e\u0432 \u0437\u0430\u043a\u0443\u043f\u043a\u0438, \u0432 \u0440\u0430\u043c\u043a\u0430\u0445 \u043a\u043e\u0442\u043e\u0440\u043e\u0433\u043e \u043e\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043e\u0446\u0435\u043d\u043a\u0430 \u0438 \u0441\u043e\u043f\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0437\u0430\u044f\u0432\u043e\u043a\u00bb: "
        "\u0434\u0430\u0442\u0430 \u043d\u0430\u0447\u0430\u043b\u0430 \u044d\u0442\u0430\u043f\u0430 - 26.10.2026 \u0433. \u0434\u0430\u0442\u0430 \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f \u044d\u0442\u0430\u043f\u0430 - 29.10.2026 \u0433."
    )

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["resultDate"] == "2026-10-29"


def test_delivery_type_is_detected_from_document_request_rule() -> None:
    fields, _, _ = validate_fields(
        job_with_lot(""), None, "Поставка товара осуществляется по разнарядкам.", []
    )

    assert fields["deliveryType"] == "by_requests"


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


def test_payment_delay_prefers_sme_variant_without_conversion() -> None:
    text = """
    Для субъектов МСП оплата производится в течение 30 рабочих дней.
    Для остальных поставщиков отсрочка платежа составляет от 30 до 60 календарных дней.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["paymentDelayDays"] == 30
    assert meta["paymentDelayDays"]["confidence"] == "high"
    assert meta["paymentDelayDays"]["source"] == "Документы / срок оплаты для МСП или самозанятого"


def test_payment_delay_prefers_sme_term_over_ninety_day_general_term() -> None:
    text = """
    Если поставщик является субъектом МСП или самозанятым, оплата производится
    в течение 7 рабочих дней после подписания УПД.
    Для остальных поставщиков оплата производится не более 90 календарных дней.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["paymentDelayDays"] == 7
    assert "МСП" in meta["paymentDelayDays"]["evidence"]
    assert meta["paymentDelayDays"]["source"] == "Документы / срок оплаты для МСП или самозанятого"


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


def test_minimum_mandatory_share_is_normalized_to_no() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="minimum_share",
                confidence="high",
                source="Документация.docx, п. 2.7",
                evidence=(
                    "Установлена минимальная обязательная доля закупок товаров "
                    "российского происхождения по ПП РФ № 1875."
                ),
            )
        }
    )

    fields, meta, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["nationalRegime"] == "no"
    assert "минимальная обязательная доля" in meta["nationalRegime"]["evidence"]


def test_minimum_share_evidence_overrides_misclassified_preference() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="preference",
                confidence="high",
                source="Документация.docx, п. 2.7",
                evidence="Установлена минимальная обязательная доля закупок товаров российского происхождения.",
            )
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["nationalRegime"] == "no"


def test_product_protection_table_overrides_generic_national_regime_candidate() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="ban",
                confidence="high",
                source="Документация.docx, п. 4.14",
                evidence=(
                    "а) запрет закупок; б) ограничение закупок; "
                    "в) преимущество в отношении товаров российского происхождения"
                ),
            )
        }
    )
    product_table = SpreadsheetTable(
        fileName="Сведения о стране происхождения.xlsx",
        sheet="Лист1",
        rows=[
            SpreadsheetRow(
                row=8,
                cells={
                    "A": "№ позиции",
                    "E": "Вид защитной меры (запрет/ограничение)",
                },
            ),
            SpreadsheetRow(row=10, cells={"A": "1", "E": "ограничение"}),
            SpreadsheetRow(row=28, cells={"A": "19", "E": "ограничение"}),
        ],
    )
    documents = [
        ParsedDocument(
            documentIndex=1,
            fileName="Сведения о стране происхождения.xlsx",
            spreadsheetTables=[product_table],
        )
    ]

    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, "", documents)

    assert fields["nationalRegime"] == "restriction"
    assert meta["nationalRegime"]["confidence"] == "high"
    assert "Вид защитной меры" in meta["nationalRegime"]["evidence"]
    assert any("товарной таблицы заменил" in warning for warning in warnings)


def test_pp_1875_application_table_uses_only_explicitly_established_measure() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="ban",
                confidence="high",
                source="Документация.docx",
                evidence="ПП РФ № 1875 предусматривает запрет, ограничение и преимущество.",
            )
        }
    )
    application_table = SpreadsheetTable(
        fileName="Документация.docx",
        sheet="Таблица Word 7",
        rows=[
            SpreadsheetRow(
                row=1,
                cells={
                    "A": "Применение национального режима: вид требования",
                    "B": "Установлены Заказчиком ДА / НЕТ",
                },
            ),
            SpreadsheetRow(row=2, cells={"A": "Запрет", "B": "НЕ УСТАНОВЛЕН"}),
            SpreadsheetRow(row=3, cells={"A": "Ограничение", "B": "НЕ УСТАНОВЛЕНО"}),
            SpreadsheetRow(row=4, cells={"A": "Преимущество", "B": "УСТАНОВЛЕНО"}),
        ],
    )
    documents = [
        ParsedDocument(
            documentIndex=1,
            fileName="Документация.docx",
            spreadsheetTables=[application_table],
        )
    ]

    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, "", documents)

    assert fields["nationalRegime"] == "preference"
    assert meta["nationalRegime"]["confidence"] == "high"
    assert "Преимущество" in meta["nationalRegime"]["evidence"]
    assert any("специальной таблицы применения" in warning for warning in warnings)


def test_pp_1875_application_table_treats_yes_as_direct_ban_evidence() -> None:
    application_table = SpreadsheetTable(
        fileName="Документация.docx",
        sheet="Таблица Word 3",
        rows=[
            SpreadsheetRow(
                row=1,
                cells={
                    "A": "Применение национального режима",
                    "B": "Установлены Заказчиком ДА / НЕТ",
                    "C": "Нормативно-правовой акт",
                },
            ),
            SpreadsheetRow(
                row=2,
                cells={
                    "A": "Запрет закупок товаров, происходящих из иностранных государств",
                    "B": "ДА",
                    "C": "Постановление Правительства РФ от 23.12.2024 № 1875",
                },
            ),
        ],
    )
    documents = [
        ParsedDocument(
            documentIndex=1,
            fileName="Документация.docx",
            spreadsheetTables=[application_table],
        )
    ]

    fields, meta, _ = validate_fields(job_with_lot(""), None, "", documents)

    assert fields["nationalRegime"] == "ban"
    assert "Запрет закупок" in meta["nationalRegime"]["evidence"]


def test_product_protection_table_has_priority_over_pp_1875_application_table() -> None:
    product_table = SpreadsheetTable(
        fileName="ТЗ.xlsx",
        sheet="Товары",
        rows=[
            SpreadsheetRow(
                row=1,
                cells={"A": "Позиция", "E": "Вид защитной меры"},
            ),
            SpreadsheetRow(row=2, cells={"A": "1", "E": "ограничение"}),
        ],
    )
    application_table = SpreadsheetTable(
        fileName="Документация.docx",
        sheet="Таблица Word 3",
        rows=[
            SpreadsheetRow(
                row=1,
                cells={
                    "A": "Применение национального режима",
                    "B": "Установлены Заказчиком ДА / НЕТ",
                },
            ),
            SpreadsheetRow(row=2, cells={"A": "Запрет закупок", "B": "ДА"}),
        ],
    )
    documents = [
        ParsedDocument(documentIndex=1, fileName="ТЗ.xlsx", spreadsheetTables=[product_table]),
        ParsedDocument(documentIndex=2, fileName="Документация.docx", spreadsheetTables=[application_table]),
    ]

    fields, meta, _ = validate_fields(job_with_lot(""), None, "", documents)

    assert fields["nationalRegime"] == "restriction"
    assert meta["nationalRegime"]["source"].startswith("ТЗ.xlsx")


def test_pp_1875_application_table_is_read_from_word_text() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="Документация.docx",
        text=(
            "Применение национального режима\n"
            "Установлены Заказчиком ДА / НЕТ\n"
            "Запрет НЕ УСТАНОВЛЕН\n"
            "Ограничение НЕ УСТАНОВЛЕНО\n"
            "Преимущество УСТАНОВЛЕНО\n"
        ),
    )

    fields, meta, _ = validate_fields(job_with_lot(""), None, "", [document])

    assert fields["nationalRegime"] == "preference"
    assert meta["nationalRegime"]["source"] == "Документация.docx"


def test_national_regime_uses_highest_established_measure() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="restriction; preference",
                confidence="high",
                source="Товарная таблица",
                evidence="По разным позициям установлены ограничение и преимущество.",
            )
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert fields["nationalRegime"] == "restriction"


def test_generic_national_regime_enumeration_does_not_fill_card() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(
                value="ban",
                confidence="high",
                source="Документация.docx, п. 4.14",
                evidence=(
                    "а) запрет закупок; б) ограничение закупок; "
                    "в) преимущество в отношении товаров российского происхождения"
                ),
            )
        }
    )

    fields, _, warnings = validate_fields(job_with_lot(""), extracted, "", [])

    assert "nationalRegime" not in fields
    assert any("перечисляет общие меры" in warning for warning in warnings)


def test_treasury_support_does_not_create_goz_or_special_account() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "stateDefenseOrder": FieldValue(
                value="yes",
                confidence="high",
                source="Проект договора.docx",
                evidence="Казначейское сопровождение целевых средств",
            ),
            "specialAccount": FieldValue(
                value="yes",
                confidence="high",
                source="Проект договора.docx",
                evidence="Открыть счёт для операций с целевыми средствами",
            ),
        }
    )
    text = (
        "Закупка по 223-ФЗ. Предусмотрено казначейское сопровождение субсидии и "
        "идентификатор государственного контракта (соглашения). Поставщик открывает "
        "счёт для операций с целевыми средствами. Обеспечение заявки не установлено."
    )

    fields, _, warnings = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["stateDefenseOrder"] == "no"
    assert fields["specialAccount"] == "no"
    assert any("не доказывают ГОЗ" in warning for warning in warnings)
    assert any("не найдено прямого требования спецсчёта" in warning for warning in warnings)


def test_direct_goz_and_bid_security_special_account_are_preserved() -> None:
    text = (
        "Поставка выполняется в рамках государственного оборонного заказа в соответствии "
        "с 275-ФЗ. Участник обязан внести обеспечение заявки на специальный банковский счёт."
    )

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["stateDefenseOrder"] == "yes"
    assert fields["specialAccount"] == "yes"
    assert meta["stateDefenseOrder"]["confidence"] == "high"
    assert meta["specialAccount"]["confidence"] == "high"


def test_batch_delivery_without_stated_term_remains_blank() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(value="by_requests", confidence="high", source="ТЗ.docx", evidence="Поставка партиями по заявкам заказчика"),
        }
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, "", [])

    assert "deliveryBatchDays" not in fields


def test_delivery_schedule_overrides_single_delivery_period() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(
                value="during_period",
                confidence="high",
                source="Contract.docx",
                evidence="Delivery within 30 calendar days.",
            ),
            "deliveryDays": FieldValue(
                value=30,
                confidence="high",
                source="Contract.docx",
                evidence="Delivery within 30 calendar days.",
            ),
            "deliveryDate": FieldValue(
                value="2027-12-31",
                confidence="high",
                source="Contract.docx",
                evidence="Delivery period ends on 31.12.2027.",
            ),
            "deliveryNote": FieldValue(
                value="Delivery according to the delivery schedule.",
                confidence="high",
                source="Contract.docx",
                evidence="Delivery according to the delivery schedule.",
            ),
        }
    )
    text = (
        "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u0442\u043e\u0432\u0430\u0440\u0430 "
        "\u043e\u0441\u0443\u0449\u0435\u0441\u0442\u0432\u043b\u044f\u0435\u0442\u0441\u044f "
        "\u043f\u043e \u0433\u0440\u0430\u0444\u0438\u043a\u0443 \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438 "
        "\u0434\u043e 31.12.2027."
    )

    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["deliveryType"] == "by_requests"
    assert "deliveryBatchDays" not in fields
    assert "deliveryDays" not in fields
    assert fields["deliveryDate"] == "2027-12-31"
    assert "\u0433\u0440\u0430\u0444\u0438\u043a" in meta["deliveryType"]["evidence"].casefold()
    assert any("\u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d" in warning for warning in warnings)


def test_single_batch_and_work_schedule_do_not_imply_batch_delivery() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryType": FieldValue(
                value="during_period",
                confidence="high",
                source="Contract.docx",
                evidence="Delivery within 15 calendar days.",
            )
        }
    )
    text = (
        "\u041f\u043e\u0441\u0442\u0430\u0432\u043a\u0430 \u043e\u0434\u043d\u043e\u0439 \u043f\u0430\u0440\u0442\u0438\u0435\u0439 "
        "\u0432 \u0442\u0435\u0447\u0435\u043d\u0438\u0435 15 \u0434\u043d\u0435\u0439. "
        "\u0413\u0440\u0430\u0444\u0438\u043a \u0440\u0430\u0431\u043e\u0442\u044b \u0441\u043a\u043b\u0430\u0434\u0430: 9:00-18:00."
    )

    fields, _, _ = validate_fields(job_with_lot(""), extracted, text, [])

    assert fields["deliveryType"] == "during_period"


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
