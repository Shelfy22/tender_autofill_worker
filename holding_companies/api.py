from __future__ import annotations

import hmac
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse

from holding_companies.celery_app import celery_app
from holding_companies.job_store import HoldingJobStore
from holding_companies.models import HoldingTenderInput, HoldingTenderResult
from holding_companies.settings import get_holding_runtime_settings


runtime = get_holding_runtime_settings()
redis_client = redis.Redis.from_url(runtime.redis_url)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    runtime.jobs_root.mkdir(parents=True, exist_ok=True)
    yield
    redis_client.close()


app = FastAPI(
    title="Holding Companies Tender Processor",
    version="0.1.0",
    lifespan=lifespan,
)


def authorize(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if runtime.api_key is None:
        return
    if not x_api_key or not hmac.compare_digest(runtime.api_key, x_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.get("/health")
def health() -> dict[str, str]:
    if not redis_client.ping():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis unavailable")
    return {"status": "ok"}


@app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(authorize)])
def create_job(payload: HoldingTenderInput) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    store = HoldingJobStore(runtime.jobs_root)
    try:
        store.create(job_id, payload)
        celery_app.send_task(
            "holding_companies.process_tender",
            args=[job_id],
            task_id=str(uuid.uuid4()),
            queue=runtime.celery_queue,
        )
    except Exception as exc:
        if store.read(job_id) is not None:
            store.fail(job_id, f"QueuePublish:{type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Holding tender task was not queued: {type(exc).__name__}",
        ) from exc
    return {
        "jobId": job_id,
        "status": "queued",
        "statusUrl": f"/jobs/{job_id}",
        "resultUrl": f"/jobs/{job_id}/result",
        "reportUrl": f"/jobs/{job_id}/report",
    }


@app.get("/jobs/{job_id}", dependencies=[Depends(authorize)])
def get_job(job_id: str) -> dict[str, object]:
    try:
        state = HoldingJobStore(runtime.jobs_root).public_state(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return state


@app.get("/jobs/{job_id}/result", response_model=HoldingTenderResult, dependencies=[Depends(authorize)])
def get_result(job_id: str) -> HoldingTenderResult:
    store = HoldingJobStore(runtime.jobs_root)
    try:
        state = store.public_state(job_id)
        result = store.result(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if state.get("status") != "completed" or result is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Result is not ready")
    return result


@app.get("/jobs/{job_id}/report", dependencies=[Depends(authorize)])
def download_report(job_id: str) -> FileResponse:
    store = HoldingJobStore(runtime.jobs_root)
    try:
        state = store.public_state(job_id)
        report_path = store.report_path(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from exc
    if state is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if state.get("status") != "completed" or not report_path.is_file():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Report is not ready")
    return FileResponse(
        report_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=str(state.get("reportFileName") or "holding_tender.xlsx"),
    )
