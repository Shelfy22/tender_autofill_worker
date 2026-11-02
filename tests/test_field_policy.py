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
        fields={**fields, "deliveryType": "by_requests", "paymentDelayDays": 180},
        meta=meta,
        product_check=None,
        decision=None,
        warnings=[],
        logs=[],
        debug=None,
    )
    assert ARCHIVED_FIELDS.isdisjoint(result["fields"])


def test_payment_delay_no_longer_creates_a_rejection_reason() -> None:
    reasons, _ = calculate_hard_reasons(
        _job(),
        {},
        {"total": 1, "coveragePercent": 100},
        "Оплата производится в течение 180 календарных дней после поставки.",
    )

    assert PAYMENT_DELAY_REASON not in [item.reason for item in reasons]
