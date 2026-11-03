"""Regression cases distilled from employee review of ``Сверка_47``.

They intentionally exercise the resolver after LLM extraction.  The complete
source archives remain outside the repository; keeping short evidence here
makes the business decisions reproducible in CI without shipping tender files.
"""


import pytest


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
