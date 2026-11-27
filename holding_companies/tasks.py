from __future__ import annotations

import logging

from billiard.exceptions import SoftTimeLimitExceeded
from celery import Task

from app.config import get_settings
from app.services.documents import safe_filename
from holding_companies.celery_app import celery_app
from holding_companies.job_store import HoldingJobStore
from holding_companies.service import HoldingCompanyService
from holding_companies.settings import get_holding_runtime_settings


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="holding_companies.process_tender", max_retries=0)
def process_tender(self: Task, job_id: str) -> dict[str, object]:
    runtime = get_holding_runtime_settings()
    store = HoldingJobStore(runtime.jobs_root)
    state = store.claim(job_id)
    if state is None:
        return {"status": "skipped", "jobId": job_id}

    try:
        payload = store.input(job_id)
        run = HoldingCompanyService(get_settings()).run(payload)
        report_name = safe_filename(f"{payload.tenderId}.xlsx", "holding_tender.xlsx")
        store.report_path(job_id).write_bytes(run.workbook)
        store.complete(
            job_id,
            result=run.result,
            report_file_name=report_name,
            warnings=run.result.warnings,
        )
        return {"status": "completed", "jobId": job_id}
    except SoftTimeLimitExceeded:
        store.fail(job_id, "SoftTimeLimitExceeded")
        raise
    except Exception as exc:
        store.fail(job_id, type(exc).__name__)
        logger.exception("holding_company_job_failed", extra={"event": {"jobId": job_id}})
        raise
