import pytest


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
        seldon_purchase={"endDate": "2026-08-07T09:00:00", "price": "900 000"},
    )
    extracted = ExtractedFieldsResponse(
        fields={
            "submissionDeadlineDate": FieldValue(
                value="2026-08-01",
                confidence="low",
                source="Документ.docx",
                evidence="Срок подачи до 01.08.2026",
            ),
            "initialPrice": FieldValue(
                value="800 000",
                confidence="low",
                source="Документ.docx",
                evidence="Цена 800 000",
            ),
        }
    )

    fields, meta, _ = validate_fields(job, extracted, "", [])

    assert fields["submissionDeadlineDate"] == "2026-08-07"
    # Internal field keeps its established HH:MM representation; the final
    # CSV formatter adds seconds in the combined deadline column.
    assert fields["submissionDeadlineTime"] == "09:00"
    assert meta["submissionDeadlineDate"]["source"] == "Seldon/Daily / дата окончания подачи"
    assert fields["initialPrice"] == "900 000"
    assert meta["initialPrice"]["source"] == "Seldon structured data"


def test_delivery_note_with_only_day_unit_is_removed() -> None:
    extracted = ExtractedFieldsResponse(
        fields={
            "deliveryNote": FieldValue(
                value="\u0440\u0430\u0431\u043e\u0447\u0438\u0435 \u0434\u043d\u0438",
                confidence="low",
                source="contract.docx",
                evidence="Delivery within 30 working days.",
            )
        }
    )

    fields, _, warnings = validate_fields(job_with_lot(""), extracted, "", [])

    assert "deliveryNote" not in fields
    assert any("\u0443\u043a\u0430\u0437\u0430\u043d \u0442\u043e\u043b\u044c\u043a\u043e \u0442\u0438\u043f \u0434\u043d\u0435\u0439" in warning for warning in warnings)


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


def test_contract_and_warranty_security_are_extracted_but_application_is_not() -> None:
    text = """
    \u041e\u0431\u0435\u0441\u043f\u0435\u0447\u0435\u043d\u0438\u0435 \u0437\u0430\u044f\u0432\u043a\u0438: \u043d\u0435 \u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f.
    \u0420\u0430\u0437\u043c\u0435\u0440 \u043e\u0431\u0435\u0441\u043f\u0435\u0447\u0435\u043d\u0438\u044f \u0438\u0441\u043f\u043e\u043b\u043d\u0435\u043d\u0438\u044f \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430 \u0441\u043e\u0441\u0442\u0430\u0432\u043b\u044f\u0435\u0442 5% \u0446\u0435\u043d\u044b \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430.
    \u041e\u0431\u0435\u0441\u043f\u0435\u0447\u0435\u043d\u0438\u0435 \u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0439\u043d\u044b\u0445 \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u0441\u0442\u0432: 1%.
    \u0413\u0430\u0440\u0430\u043d\u0442\u0438\u0439\u043d\u044b\u0439 \u0441\u0440\u043e\u043a \u0441\u043e\u0441\u0442\u0430\u0432\u043b\u044f\u0435\u0442 24 \u043c\u0435\u0441\u044f\u0446\u0430.
    """

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert "applicationSecurity" not in fields
    assert fields["contractSecurity"].startswith("5%")
    assert fields["warrantySecurity"].startswith("1%")
    assert "applicationSecurity" not in meta
    assert meta["contractSecurity"]["confidence"] == "high"


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


def test_pp_1875_national_regime_boilerplate_does_not_create_goz() -> None:
    text = (
        "Требования применяются к товарам по перечню согласно приложению № 1 "
        "Постановления Правительства РФ от 23.12.2024 № 1875, а также закупок "
        "в рамках государственного оборонного заказа для выполнения мероприятий "
        "государственных программ Российской Федерации."
    )

    fields, meta, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["stateDefenseOrder"] == "no"
    assert meta["stateDefenseOrder"]["evidence"] == "Нет прямого упоминания ГОЗ или 275-ФЗ"


def test_real_goz_after_pp_1875_boilerplate_is_not_lost() -> None:
    text = (
        "По ПП № 1875 применяются ограничения, в том числе к закупкам в рамках "
        "государственного оборонного заказа для выполнения мероприятий государственных программ. "
        "Поставка по настоящему договору выполняется в рамках ГОЗ."
    )

    fields, _, _ = validate_fields(job_with_lot(""), None, text, [])

    assert fields["stateDefenseOrder"] == "yes"


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
