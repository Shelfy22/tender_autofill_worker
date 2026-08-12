from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from billiard.exceptions import SoftTimeLimitExceeded
from celery import Task

from app.celery_app import celery_app
from app.config import get_settings
from app.db import get_repository
from app.logging import bind_context, clear_context, memory_rss_mb
from app.observability import RunObserver, heartbeat_loop
from app.pipeline import TenderPipeline


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="tender_autofill.process_tender", max_retries=0)
def process_tender(self: Task, record_key: str, batch_id: str) -> dict[str, object]:
    settings = get_settings()
    repository = get_repository()
    task_id = str(self.request.id)
    bind_context(batch_id=batch_id, record_key=record_key, task_id=task_id)
    claim = repository.claim_for_processing(record_key, task_id)
    if claim is None:
        logger.warning(
            "job_claim_skipped",
            extra={"event": {"stage": "claim", "reason": "job is not dispatching"}},
        )
        clear_context()
        return {"status": "skipped", "record_key": record_key}

    bind_context(
        batch_id=claim.batch_id,
        record_key=claim.record_key,
        seldon_id=claim.seldon_id,
        attempt=claim.attempt,
        task_id=task_id,
    )
    observer = RunObserver(
        repository,
        run_id=task_id,
        record_key=claim.record_key,
        batch_id=claim.batch_id,
        seldon_id=claim.seldon_id,
        attempt=claim.attempt,
        enabled=settings.observability_enabled,
    )
    observer.start()
    settings.temp_root.mkdir(parents=True, exist_ok=True)
    try:
        with heartbeat_loop(observer, settings.observability_heartbeat_seconds):
            with tempfile.TemporaryDirectory(
                prefix=f"tender-{claim.attempt}-", dir=settings.temp_root
            ) as directory:
                result = TenderPipeline(settings, Path(directory), observer=observer).run(claim)
                completed = repository.complete_job(record_key, task_id, result)
                if not completed:
                    raise RuntimeError("Job result не записан: processing claim больше не принадлежит task")
                observer.finish_completed(result)
                logger.info(
                    "job_completed",
                    extra={
                        "event": {
                            "stage": "completed",
                            "result_json_bytes": len(str(result).encode("utf-8")),
                            "memory_rss_mb": memory_rss_mb(),
                        }
                    },
                )
                return {"status": "completed", "record_key": record_key}
    except SoftTimeLimitExceeded as exc:
        message = f"Python Worker soft timeout: {settings.celery_soft_time_limit_seconds}s"
        repository.fail_job(record_key, task_id, message)
        observer.finish_failed(exc)
        logger.exception("job_soft_timeout", extra={"event": {"stage": "failed", "error": message}})
        raise exc
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        repository.fail_job(record_key, task_id, message)
        observer.finish_failed(exc)
        logger.exception(
            "job_failed",
            extra={
                "event": {
                    "stage": "failed",
                    "error": message,
                    "memory_rss_mb": memory_rss_mb(),
                }
            },
        )
        raise
    finally:
        clear_context()
