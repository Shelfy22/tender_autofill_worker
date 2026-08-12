from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import NormalizedJob, TenderResult


def build_result_json(
    job: NormalizedJob,
    *,
    fields: dict[str, Any],
    meta: dict[str, Any],
    product_check: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    warnings: list[str],
    logs: list[dict[str, Any]],
    debug: dict[str, Any] | None,
) -> dict[str, Any]:
    result_fields = {**fields, "legalEntity": None, "toCode": job.to_code}
    result = TenderResult(
        fields=result_fields,
        meta=meta,
        productCheck=product_check,
        decision=decision,
        warnings=list(dict.fromkeys(str(warning) for warning in warnings if str(warning).strip())),
        logs=logs,
        debug=debug,
        reportId=job.report_id,
        seldonId=job.seldon_id,
        etpId=job.etp_id,
        purchaseType=job.purchase_type,
        purchaseNumber=str(job.seldon_purchase.get("notificationNumber") or job.etp_id or "") or None,
        tenderUrl=job.tender_url,
        batchId=job.batch_id,
        batchDate=job.batch_date,
        rowNumber=job.row_number,
        jobRecordKey=job.job_record_key,
        remainingDays=job.remaining_days,
        toCode=job.to_code,
        lawCode=job.law_code,
        sectionName=job.section_name,
        filterName=job.filter_name,
        reportFields=job.report_fields,
        sourceTender=job.seldon_purchase,
        processedAt=datetime.now(timezone.utc),
    )
    return result.model_dump(mode="json")
