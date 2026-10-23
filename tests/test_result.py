from app.models import NormalizedJob
from app.services.result import build_result_json


def test_result_json_contract_for_finalizer() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        batch_date="2026-08-07",
        row_number=7,
        report_id=1,
        purchase_type="223-ФЗ",
        seldon_id="42",
        to_code="TO-1",
        law_code="223",
        report_fields={"ID": "42", "Код ТО": "TO-1"},
        seldon_purchase={"seldonId": 42, "notificationNumber": "N-1"},
    )
    result = build_result_json(
        job,
        fields={"tenderStatus": "Согласовано ИИ"},
        meta={},
        product_check={"coveragePercent": 75},
        decision={"status": "Согласовано ИИ"},
        warnings=[],
        logs=[],
        debug={},
    )
    assert result["reportId"] == 1
    assert result["jobRecordKey"] == "daily:b:1:42"
    assert "legalEntity" not in result["fields"]
    assert result["fields"]["toCode"] == "TO-1"
    assert result["reportFields"]["ID"] == "42"
    assert result["sourceTender"]["seldonId"] == 42


def test_result_omits_retired_report_fields_and_headers() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        report_id=1,
        seldon_id="42",
        report_fields={
            "Обеспечение заявки": "1%",
            "Обеспечение контракта": "5%",
            "Федеральный закон": "223-ФЗ",
            "Код контрагента": "1",
            "Название контрагента": "Заказчик",
            "ИНН контрагента": "1234567890",
            "КПП контрагента": "123456789",
            "ЦКГ": "Группа",
            "Потенциал": "Высокий",
            "Сделка": "Сделка",
            "Договор": "Договор",
            "Примечание к контрагенту": "Текст",
            "ОП": "ОП",
            "Юридическое лицо ЭТМ": "ООО ЭТМ",
            "Тендер выигран": "Да",
        },
    )
    omitted_fields = {
        "federalLaw", "applicationSecurity", "contractSecurity", "counterpartyCode",
        "counterpartyName", "counterpartyInn", "counterpartyKpp", "counterpartyCkg",
        "counterpartyPotential", "deal", "contract", "counterpartyNote", "op",
        "legalEntity", "tenderWonDate",
    }

    result = build_result_json(
        job,
        fields={key: "obsolete" for key in omitted_fields},
        meta={key: {"source": "test"} for key in omitted_fields},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert omitted_fields.isdisjoint(result["fields"])
    assert omitted_fields.isdisjoint(result["meta"])
    assert set(job.report_fields).isdisjoint(result["reportFields"])


def test_result_syncs_normalized_lot_divisibility_to_daily_column() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        report_id=1,
        seldon_id="42",
        report_fields={"Лот делимый": ""},
    )

    result = build_result_json(
        job,
        fields={"lotDivisible": "yes"},
        meta={},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert result["fields"]["lotDivisible"] == "Делимый"
    assert result["reportFields"]["Лот делимый"] == "Делимый"
    assert result["reportFields"]["lotDivisible"] == "Делимый"


def test_indivisible_lot_uses_business_label_in_daily_column() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        report_id=1,
        seldon_id="42",
        report_fields={"Лот делимый": ""},
    )

    result = build_result_json(
        job,
        fields={"lotDivisible": "no"},
        meta={},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert result["reportFields"]["Лот делимый"] == "Неделимый"
    assert result["fields"]["lotDivisible"] == "Неделимый"


def test_result_uses_russian_business_labels_for_goz_and_national_regime() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        report_id=1,
        seldon_id="42",
    )

    result = build_result_json(
        job,
        fields={
            "stateDefenseOrder": "yes",
            "nationalRegime": "ban; restriction; preference",
        },
        meta={},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert result["fields"]["stateDefenseOrder"] == "Да"
    assert result["fields"]["nationalRegime"] == "запрет; ограничение; преимущество"

    no_result = build_result_json(
        job,
        fields={"stateDefenseOrder": "no", "nationalRegime": "no"},
        meta={},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert no_result["fields"]["stateDefenseOrder"] == "Нет"
    assert no_result["fields"]["nationalRegime"] == "Нет"
