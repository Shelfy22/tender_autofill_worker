from __future__ import annotations

import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from app import __version__
from app.celery_app import celery_app
from app.config import get_settings
from app.db import get_repository
from app.logging import configure_logging
from app.models import (
    AcceptedJob,
    BatchDispatchRequest,
    BatchDispatchResponse,
    HealthResponse,
    RejectedJob,
)


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
repository = get_repository()
redis_client = redis.Redis.from_url(settings.redis_url.get_secret_value())

DISPATCHED = Counter("tender_autofill_api_dispatched_total", "Jobs published to the tender queue")
REJECTED = Counter("tender_autofill_api_rejected_total", "Jobs rejected before queue publish", ["reason"])
RUNS = Gauge("tender_autofill_worker_runs", "Durable Python worker runs", ["status"])
WORKER_TOTALS = Gauge(
    "tender_autofill_worker_total",
    "Exact cumulative worker counters stored in PostgreSQL",
    ["metric"],
)
LLM_BY_MODEL = Gauge(
    "tender_autofill_llm_requests_by_model_total",
    "LLM calls stored in the execution audit",
    ["model", "status"],
)
LLM_TOKENS_BY_MODEL = Gauge(
    "tender_autofill_llm_tokens_by_model_total",
    "LLM tokens stored in the execution audit",
    ["model", "status", "type"],
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    repository.open()
    yield
    repository.close()
    redis_client.close()


app = FastAPI(
    title="Tender Autofill Worker API",
    version=__version__,
    lifespan=lifespan,
)


def authorize(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if settings.api_key is None:
        return
    expected = settings.api_key.get_secret_value()
    if not x_api_key or not hmac.compare_digest(expected, x_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.post(
    "/jobs/batch",
    response_model=BatchDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(authorize)],
)
def dispatch_batch(request: BatchDispatchRequest) -> BatchDispatchResponse:
    if len(request.jobs) > settings.api_max_batch_jobs:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Maximum jobs per request: {settings.api_max_batch_jobs}",
        )
    keys = [job.job_record_key for job in request.jobs]
    statuses = repository.dispatchable_statuses(keys)
    accepted: list[AcceptedJob] = []
    rejected: list[RejectedJob] = []
    for job in request.jobs:
        current = statuses.get(job.job_record_key)
        if current != "dispatching":
            reason = "not_found" if current is None else f"status_{current}"
            rejected.append(RejectedJob(jobRecordKey=job.job_record_key, reason=reason))
            REJECTED.labels(reason=reason).inc()
            continue
        task_id = str(uuid.uuid4())
        try:
            celery_app.send_task(
                "tender_autofill.process_tender",
                args=[job.job_record_key, job.batch_id],
                task_id=task_id,
                queue=settings.celery_queue,
            )
        except Exception as exc:
            reason = f"broker_publish_failed:{type(exc).__name__}"
            repository.release_dispatch(
                job.job_record_key,
                f"Python API не опубликовал задачу в Redis: {type(exc).__name__}: {exc}",
            )
            rejected.append(RejectedJob(jobRecordKey=job.job_record_key, reason=reason))
            REJECTED.labels(reason="broker_publish_failed").inc()
            continue
        accepted.append(AcceptedJob(jobRecordKey=job.job_record_key, taskId=task_id))
        DISPATCHED.inc()
    response_status = (
        "accepted" if accepted and not rejected else "partially_accepted" if accepted else "rejected"
    )
    return BatchDispatchResponse(
        status=response_status,
        accepted=len(accepted),
        rejected=len(rejected),
        jobs=accepted,
        rejectedJobs=rejected,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    postgres_ok = repository.ping()
    try:
        redis_ok = bool(redis_client.ping())
    except Exception:
        redis_ok = False
    return HealthResponse(
        status="ok" if postgres_ok and redis_ok else "degraded",
        postgres=postgres_ok,
        redis=redis_ok,
        version=__version__,
    )


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    if settings.observability_enabled:
        try:
            values = repository.observability_metrics(
                stale_after_seconds=max(120, settings.observability_heartbeat_seconds * 4)
            )
            totals = values["totals"]
            for run_status in ("running", "stale", "completed", "failed", "interrupted"):
                RUNS.labels(status=run_status).set(int(totals.get(run_status) or 0))
            for key in (
                "llm_requests",
                "llm_failures",
                "llm_prompt_tokens",
                "llm_completion_tokens",
                "llm_total_tokens",
                "llm_fallbacks",
                "embedding_queries",
                "embedding_http_requests",
                "qdrant_queries",
                "qdrant_http_requests",
                "qdrant_results",
                "download_bytes",
            ):
                WORKER_TOTALS.labels(metric=key).set(int(totals.get(key) or 0))
            for row in values["llm_models"]:
                model = str(row["model"])
                call_status = str(row["status"])
                LLM_BY_MODEL.labels(model=model, status=call_status).set(int(row["requests"]))
                LLM_TOKENS_BY_MODEL.labels(model=model, status=call_status, type="prompt").set(
                    int(row["prompt_tokens"])
                )
                LLM_TOKENS_BY_MODEL.labels(
                    model=model, status=call_status, type="completion"
                ).set(
                    int(row["completion_tokens"])
                )
        except Exception as exc:
            logger.warning(
                "observability_metrics_unavailable",
                extra={"event": {"stage": "metrics", "error": f"{type(exc).__name__}: {exc}"}},
            )
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get(
    "/observability/jobs/{record_key}/runs",
    dependencies=[Depends(authorize)],
)
def job_runs(record_key: str, limit: int = 20) -> dict[str, object]:
    return {"recordKey": record_key, "runs": repository.get_job_runs(record_key, limit)}


@app.get(
    "/observability/runs/{run_id}",
    dependencies=[Depends(authorize)],
)
def run_timeline(run_id: str) -> dict[str, object]:
    result = repository.get_run_with_events(run_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return result
