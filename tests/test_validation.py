from app.models import NormalizedJob
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
