"""Regression cases distilled from employee review of ``Сверка_47``.

They intentionally exercise the resolver after LLM extraction.  The complete
source archives remain outside the repository; keeping short evidence here
makes the business decisions reproducible in CI without shipping tender files.
"""

from app.models import ExtractedFieldsResponse, FieldValue, NormalizedJob, ParsedDocument, SpreadsheetRow, SpreadsheetTable
from app.services.validation import validate_fields


def _job(*, result_date: str | None = None) -> NormalizedJob:
    purchase = {"resultDate": result_date} if result_date else {}
    return NormalizedJob(
        job_record_key="daily:sverka47:1",
        batch_id="sverka47",
        report_id=1,
        seldon_id="sverka47",
        report_fields={},
        seldon_purchase=purchase,
    )


def test_sverka47_national_table_uses_only_established_measure() -> None:
    table = SpreadsheetTable(
        fileName="Извещение.docx",
        sheet="Таблица Word 1",
        rows=[
            SpreadsheetRow(row=1, cells={"A": "Применение национального режима", "B": "Установлены Заказчиком ДА / НЕТ"}),
            SpreadsheetRow(row=2, cells={"A": "Запрет", "B": "НЕ УСТАНОВЛЕН"}),
            SpreadsheetRow(row=3, cells={"A": "Ограничение", "B": "УСТАНОВЛЕНО. Приложение № 2"}),
            SpreadsheetRow(row=4, cells={"A": "Преимущество", "B": "НЕ УСТАНОВЛЕНО"}),
        ],
    )
    extracted = ExtractedFieldsResponse(fields={
        "nationalRegime": FieldValue(value="ban", confidence="high", source="Документация", evidence="ПП №1875: запрет, ограничение, преимущество"),
    })

    fields, _, _ = validate_fields(
        _job(), extracted, "", [ParsedDocument(documentIndex=1, fileName="Извещение.docx", spreadsheetTables=[table])]
    )

    assert fields["nationalRegime"] == "restriction"


def test_sverka47_generic_pp1875_list_does_not_create_national_regime() -> None:
    extracted = ExtractedFieldsResponse(fields={
        "nationalRegime": FieldValue(
            value="ban", confidence="high", source="Документация", 
            evidence="а) запрет закупок; б) ограничение закупок; в) преимущество в отношении российских товаров",
        ),
    })

    fields, _, _ = validate_fields(_job(), extracted, "", [])

    assert "nationalRegime" not in fields


def test_sverka47_payment_uses_general_term_not_sme_exception() -> None:
    text = (
        "Оплата производится в срок от 30 до 60 календарных дней после приёмки товара. "
        "Для субъектов МСП оплата производится в течение 7 рабочих дней."
    )

    fields, _, _ = validate_fields(_job(), None, text, [])

    assert fields["paymentDelayDays"] == 60


def test_sverka47_payment_does_not_double_convert_llm_calendar_value() -> None:
    extracted = ExtractedFieldsResponse(fields={
        "paymentDelayDays": FieldValue(
            value=84, confidence="high", source="Договор.docx",
            evidence="Оплата производится в течение 60 календарных дней после приёмки.",
        ),
    })

    fields, _, _ = validate_fields(_job(), extracted, "", [])

    assert fields["paymentDelayDays"] == 60


def test_sverka47_conditional_template_does_not_make_delivery_by_requests() -> None:
    extracted = ExtractedFieldsResponse(fields={
        "deliveryType": FieldValue(
            value="by_requests", confidence="high", source="Проект договора.docx",
            evidence="Если спецификацией предусмотрена поставка по заявкам, сроки устанавливаются в заявке.",
        ),
        "deliveryDays": FieldValue(
            value=20, confidence="high", source="ТЗ.docx",
            evidence="Поставка товара в течение 20 календарных дней с даты заключения договора.",
        ),
    })

    fields, _, _ = validate_fields(_job(result_date="2026-10-14"), extracted, "", [])

    assert fields["deliveryType"] == "during_period"
    assert fields["deliveryDays"] == 20
    assert "deliveryBatchDays" not in fields
    assert fields["deliveryDate"] == "2026-11-23"


def test_sverka47_actual_multi_period_schedule_is_delivery_by_requests() -> None:
    text = (
        "График поставки: январь 2027 — 10 шт.; март 2027 — 15 шт.; "
        "апрель 2027 — 20 шт."
    )

    fields, _, _ = validate_fields(_job(), None, text, [])

    assert fields["deliveryType"] == "by_requests"


def test_sverka47_seldon_result_date_is_authoritative_and_contract_is_derived() -> None:
    extracted = ExtractedFieldsResponse(fields={
        "resultDate": FieldValue(
            value="2026-09-23", confidence="high", source="Извещение.docx",
            evidence="Дата подведения итогов: 23.09.2026",
        ),
    })

    fields, meta, _ = validate_fields(_job(result_date="2026-09-29"), extracted, "", [])

    assert fields["resultDate"] == "2026-09-29"
    assert fields["contractDate"] == "2026-10-19"
    assert meta["resultDate"]["source"].startswith("Seldon/Daily")
