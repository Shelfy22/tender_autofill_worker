from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.db import Repository
from app.logging import memory_rss_mb


logger = logging.getLogger(__name__)


class RunObserver:
    """Best-effort durable execution audit.

    Observability failures are logged but never change tender business results.
    Prompts, document text, credentials and binary data must not be passed here.
    """

    def __init__(
        self,
        repository: Repository,
        *,
        run_id: str,
        record_key: str,
        batch_id: str,
        seldon_id: str | None,
        attempt: int,
        enabled: bool = True,
    ) -> None:
        self.repository = repository
        self.run_id = run_id
        self.record_key = record_key
        self.batch_id = batch_id
        self.seldon_id = seldon_id
        self.attempt = attempt
        self.enabled = enabled
        self.worker_name = socket.gethostname()

    def _safe(self, operation: str, function: Any) -> None:
        if not self.enabled:
            return
        try:
            function()
        except Exception as exc:
            self.enabled = False
            logger.warning(
                "observability_disabled",
                extra={
                    "event": {
                        "stage": "observability",
                        "operation": operation,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                },
            )

    def start(self) -> None:
        self._safe(
            "start_run",
            lambda: self.repository.start_job_run(
                run_id=self.run_id,
                record_key=self.record_key,
                batch_id=self.batch_id,
                seldon_id=self.seldon_id,
                attempt=self.attempt,
                worker_name=self.worker_name,
            ),
        )
        self.event(event_type="job", status="started", stage="claim")

    def heartbeat(self) -> None:
        self._safe(
            "heartbeat",
            lambda: self.repository.heartbeat_job_run(self.run_id, memory_rss_mb()),
        )

    def event(
        self,
        *,
        event_type: str,
        status: str,
        stage: str | None = None,
        service: str | None = None,
        operation: str | None = None,
        model: str | None = None,
        primary_model: str | None = None,
        provider_request_id: str | None = None,
        http_method: str | None = None,
        http_status: int | None = None,
        duration_seconds: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        result_count: int | None = None,
        byte_count: int | None = None,
        error: BaseException | None = None,
        details: dict[str, Any] | None = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "record_key": self.record_key,
            "batch_id": self.batch_id,
            "attempt": self.attempt,
            "event_type": event_type,
            "stage": stage,
            "status": status,
            "service": service,
            "operation": operation,
            "model": model,
            "primary_model": primary_model,
            "provider_request_id": provider_request_id,
            "http_method": http_method,
            "http_status": http_status,
            "duration_seconds": duration_seconds,
            "memory_rss_mb": memory_rss_mb(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "result_count": result_count,
            "byte_count": byte_count,
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
            "details": details or {},
        }

        def persist() -> None:
            self.repository.append_job_event(payload, counters=counters)

        self._safe("append_event", persist)

    def stage_started(self, name: str) -> None:
        self._safe(
            "update_stage",
            lambda: self.repository.update_job_run_stage(self.run_id, name, memory_rss_mb()),
        )
        self.event(event_type="stage", status="started", stage=name)

    def stage_finished(
        self,
        name: str,
        *,
        duration_seconds: float,
        error: BaseException | None = None,
    ) -> None:
        self.event(
            event_type="stage",
            status="failed" if error else "completed",
            stage=name,
            duration_seconds=duration_seconds,
            error=error,
        )

    def counters(self, **values: int) -> None:
        self._safe(
            "increment_counters",
            lambda: self.repository.increment_job_run_counters(self.run_id, values),
        )

    def finish_completed(self, result: dict[str, Any]) -> None:
        decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
        product_check = (
            result.get("productCheck") if isinstance(result.get("productCheck"), dict) else {}
        )
        summary = {
            "decision": decision.get("decision") or decision.get("status"),
            "tenderStatus": (result.get("fields") or {}).get("tenderStatus"),
            "coverage": product_check.get("coverage"),
            "products": product_check.get("total"),
            "supplied": product_check.get("supplied"),
        }
        warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
        self.event(event_type="job", status="completed", stage="completed")
        self._safe(
            "finish_run",
            lambda: self.repository.finish_job_run(
                run_id=self.run_id,
                status="completed",
                memory_rss_mb=memory_rss_mb(),
                warnings_count=len(warnings),
                result_summary=summary,
            ),
        )

    def finish_failed(self, error: BaseException) -> None:
        self.event(event_type="job", status="failed", stage="failed", error=error)
        self._safe(
            "finish_run",
            lambda: self.repository.finish_job_run(
                run_id=self.run_id,
                status="failed",
                memory_rss_mb=memory_rss_mb(),
                error_type=type(error).__name__,
                error_message=str(error),
            ),
        )


@contextmanager
def heartbeat_loop(observer: RunObserver, interval_seconds: int) -> Iterator[None]:
    stopped = threading.Event()

    def loop() -> None:
        while not stopped.wait(interval_seconds):
            observer.heartbeat()

    thread = threading.Thread(target=loop, name="tender-run-heartbeat", daemon=True)
    observer.heartbeat()
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=min(interval_seconds, 5))
