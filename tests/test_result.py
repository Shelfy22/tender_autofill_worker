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
        fields={"tenderStatus": "Согласовано КУ ЦП"},
        meta={},
        product_check={"coveragePercent": 75},
        decision={"status": "Согласовано КУ ЦП"},
        warnings=[],
        logs=[],
        debug={},
    )
    assert result["reportId"] == 1
    assert result["jobRecordKey"] == "daily:b:1:42"
    assert result["fields"]["legalEntity"] is None
    assert result["fields"]["toCode"] == "TO-1"
    assert result["reportFields"]["ID"] == "42"
    assert result["sourceTender"]["seldonId"] == 42
