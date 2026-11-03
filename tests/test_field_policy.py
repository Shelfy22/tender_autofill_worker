from app.models import ExtractedFieldsResponse, FieldValue, NormalizedJob
from app.services.decision import PAYMENT_DELAY_REASON, calculate_hard_reasons
from app.services.llm import FIELD_NAMES
from app.services.result import build_result_json
from app.services.validation import validate_fields


ARCHIVED_FIELDS = {
    "nationalRegime",
    "deliveryType",
    "deliveryDays",
    "deliveryBatchDays",
    "deliveryDate",
    "paymentDelayDays",
}


def _job() -> NormalizedJob:
    return NormalizedJob(
        job_record_key="daily:policy:1",
        batch_id="policy",
        report_id=1,
        seldon_id="1",
        report_fields={},
        seldon_purchase={},
    )


def test_archived_fields_are_not_requested_or_emitted() -> None:
    assert ARCHIVED_FIELDS.isdisjoint(FIELD_NAMES)

    extracted = ExtractedFieldsResponse(
        fields={
            "nationalRegime": FieldValue(value="ban"),
            "deliveryType": FieldValue(value="by_requests"),
            "deliveryDays": FieldValue(value=30),
            "deliveryBatchDays": FieldValue(value=7),
            "deliveryDate": FieldValue(value="2027-01-01"),
            "paymentDelayDays": FieldValue(value=180),
            "lotDivisible": FieldValue(value="yes"),
        }
    )
    fields, meta, _ = validate_fields(_job(), extracted, "", [])

    assert ARCHIVED_FIELDS.isdisjoint(fields)
    assert ARCHIVED_FIELDS.isdisjoint(meta)
    assert fields["lotDivisible"] in {"yes", "no"}

    result = build_result_json(
        _job(),
        fields={**fields, **{name: "ban" for name in ARCHIVED_FIELDS}},
        meta={**meta, **{name: {"source": "legacy"} for name in ARCHIVED_FIELDS}},
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )
    assert ARCHIVED_FIELDS.isdisjoint(result["fields"])
    assert ARCHIVED_FIELDS.isdisjoint(result["meta"])


def test_disabled_resolvers_are_not_executed(monkeypatch) -> None:
    import app.services.validation as validation

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled resolver was executed")

    for name in (
        "_national_regime_from_product_tables",
        "_national_regime_from_application_tables",
        "_payment_delay_from_text",
        "_delivery_schedule_from_documents",
        "_delivery_request_evidence_from_documents",
    ):
        monkeypatch.setattr(validation, name, forbidden)
    validate_fields(_job(), None, "", [])


def test_disabled_report_headers_are_not_exported() -> None:
    from app.services.field_policy import DISABLED_FIELD_HEADERS

    job = _job().model_copy(update={
        "report_fields": {header: "legacy" for header in DISABLED_FIELD_HEADERS}
    })
    result = build_result_json(
        job, fields={}, meta={}, product_check=None, decision=None,
        warnings=[], logs=[], debug=None,
    )
    assert DISABLED_FIELD_HEADERS.isdisjoint(result["reportFields"])


def test_payment_delay_no_longer_creates_a_rejection_reason() -> None:
    reasons, _ = calculate_hard_reasons(
        _job(),
        {},
        {"total": 1, "coveragePercent": 100},
        "Оплата производится в течение 180 календарных дней после поставки.",
    )

    assert PAYMENT_DELAY_REASON not in [item.reason for item in reasons]
