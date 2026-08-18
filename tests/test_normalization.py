from app.models import JobClaim
from app.services.normalization import normalize_job_payload


def test_tender_url_uses_seldon_url_source_for_document_referer() -> None:
    claim = JobClaim(
        record_key="daily:test:1:1091291223",
        batch_id="test",
        attempt=1,
        report_id=3,
        seldon_id="1091291223",
        report_fields={},
        input_json={
            "reportId": 3,
            "seldonId": "1091291223",
            "seldonPurchase": {
                "urlSource": "https://com.roseltorg.ru/procedure/123",
            },
        },
    )

    job = normalize_job_payload(claim)

    assert job.tender_url == "https://com.roseltorg.ru/procedure/123"


def test_explicit_tender_url_has_priority_over_seldon_purchase_links() -> None:
    claim = JobClaim(
        record_key="daily:test:1:1091289267",
        batch_id="test",
        attempt=1,
        report_id=3,
        seldon_id="1091289267",
        report_fields={},
        input_json={
            "reportId": 3,
            "seldonId": "1091289267",
            "tenderUrl": "https://utp.sberbank-ast.ru/VIP/procedure/456",
            "seldonPurchase": {
                "urlSource": "https://stale.example.test/procedure/456",
            },
        },
    )

    job = normalize_job_payload(claim)

    assert job.tender_url == "https://utp.sberbank-ast.ru/VIP/procedure/456"
