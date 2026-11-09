from app.models import NormalizedJob
from app.services.result import build_result_json


def test_reason_codes_accept_both_russian_yo_spellings():
    from app.services.result import _tender_status_reason_code

    for label, expected in (
        ("Номенклатура. Военная приемка", "cp_nom_accept_military"),
        ("Номенклатура. Атомная приемка", "cp_nom_accept_atomic"),
    ):
        assert _tender_status_reason_code(label) == expected
        assert _tender_status_reason_code(label.replace("приемка", "приёмка")) == expected


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
    assert result["fields"]["toCode"] == "Dep_TO-1"
    assert result["reportFields"]["Код ТО"] == "Dep_TO-1"
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
        "federalLaw", "applicationSecurity", "dateCreated", "tenderGroup", "counterpartyCode",
        "counterpartyName", "counterpartyInn", "counterpartyKpp", "counterpartyCkg",
        "counterpartyPotential", "deal", "contract", "counterpartyNote", "op",
        "legalEntity", "tenderWonDate",
        "submissionDeadlineDate", "submissionDeadlineTime", "tenderUrlSource",
        "initialPrice", "finalPrice",
    }

    result = build_result_json(
        job,
        fields={**{key: "obsolete" for key in omitted_fields}, "contractSecurity": "5%"},
        meta={**{key: {"source": "test"} for key in omitted_fields}, "contractSecurity": {"source": "contract"}},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert omitted_fields.isdisjoint(result["fields"])
    assert omitted_fields.isdisjoint(result["meta"])
    assert result["fields"]["contractSecurity"] == "5%"
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

    assert result["fields"]["lotDivisible"] == "yes"
    assert result["reportFields"]["Лот делимый"] == "yes"
    assert result["reportFields"]["lotDivisible"] == "yes"


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

    assert result["reportFields"]["Лот делимый"] == "no"
    assert result["fields"]["lotDivisible"] == "no"


def test_result_uses_target_system_codes_for_goz_lot_and_decision() -> None:
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
            "lotDivisible": "yes",
            "tenderStatus": "Отказано ИИ",
            "tenderStatusReason": "Коммерческие условия. НМЦК менее 1 млн руб.",
            "nationalRegime": "ban; restriction; preference",
        },
        meta={},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )

    assert result["fields"]["stateDefenseOrder"] == "yes"
    assert result["fields"]["lotDivisible"] == "yes"
    assert result["fields"]["tenderStatus"] == "230"
    assert result["fields"]["tenderStatusReason"] == "ku_nmck_ls300"
    assert "nationalRegime" not in result["fields"]

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

    assert no_result["fields"]["stateDefenseOrder"] == "no"
    assert "nationalRegime" not in no_result["fields"]


def test_result_maps_all_new_status_and_reason_dictionary_codes() -> None:
    job = NormalizedJob(
        job_record_key="daily:b:1:42",
        batch_id="b",
        report_id=1,
        seldon_id="42",
    )
    cases = (
        ("Загружен Seldon", None, "110", None),
        ("Согласовано ИИ", None, "220", None),
        (
            "Отказано ИИ",
            "Оргвопросы. Опрос рынка / Мониторинг / Анализ рынка / Анонс / КИМ",
            "230",
            "cp_org_analiz",
        ),
        (
            "Отказано ИИ",
            "Оргвопросы. На момент согласования менее 3 рабочих дней до подачи заявки",
            "230",
            "org_term_ls3",
        ),
        (
            "Отказано ИИ",
            "Оргвопросы. Отказ организатора от проведения тендера",
            "230",
            "cp_org_tender_no",
        ),
        ("Загружен Seldon", "Прочее", "110", "cp_other"),
    )

    for status, reason, expected_status, expected_reason in cases:
        fields = {"tenderStatus": status}
        if reason:
            fields["tenderStatusReason"] = reason
        result = build_result_json(
            job,
            fields=fields,
            meta={},
            product_check=None,
            decision=None,
            warnings=[],
            logs=[],
            debug=None,
        )
        assert result["fields"]["tenderStatus"] == expected_status
        assert result["fields"].get("tenderStatusReason") == expected_reason


def test_result_prefixes_every_department_code_once() -> None:
    base = {
        "job_record_key": "daily:b:1:42",
        "batch_id": "b",
        "report_id": 1,
        "seldon_id": "42",
    }
    for raw, expected in (("1Y", "Dep_1Y"), ("11D", "Dep_11D"), ("Dep_1F", "Dep_1F")):
        job = NormalizedJob(**base, to_code=raw, report_fields={"Код ТО": raw})
        result = build_result_json(
            job,
            fields={},
            meta={},
            product_check=None,
            decision=None,
            warnings=[],
            logs=[],
            debug=None,
        )
        assert result["toCode"] == expected
        assert result["fields"]["toCode"] == expected
        assert result["reportFields"]["Код ТО"] == expected
