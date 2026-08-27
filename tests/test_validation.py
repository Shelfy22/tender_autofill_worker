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


def test_contract_validity_date_is_not_delivery_date() -> None:
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

    fields, meta, warnings = validate_fields(job_with_lot(""), extracted, "", [])

    assert "deliveryDate" not in fields
    assert "deliveryDate" not in meta
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
