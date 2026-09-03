from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings, get_settings
from app.models import JobClaim


class Repository:
    _OBSERVABILITY_COUNTERS = {
        "llm_requests",
        "llm_successes",
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
        "documents_requested",
        "documents_parsed",
        "download_bytes",
        "warnings_count",
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool = ConnectionPool(
            conninfo=self.settings.postgres_dsn.get_secret_value(),
            min_size=self.settings.postgres_pool_min_size,
            max_size=self.settings.postgres_pool_max_size,
            kwargs={
                "autocommit": False,
                "row_factory": dict_row,
                "connect_timeout": self.settings.postgres_connect_timeout_seconds,
            },
            open=False,
        )

    def open(self) -> None:
        self._pool.open(wait=True)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self) -> Iterator[Connection[Any]]:
        if self._pool.closed:
            self.open()
        with self._pool.connection() as connection:
            yield connection

    def ping(self) -> bool:
        try:
            with self.connection() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() is not None
        except Exception:
            return False

    def dispatchable_statuses(self, keys: Sequence[str]) -> dict[str, str]:
        if not keys:
            return {}
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT record_key, status
                FROM tender_autofill_jobs
                WHERE record_key = ANY(%s)
                """,
                (list(keys),),
            )
            return {str(row["record_key"]): str(row["status"]) for row in cursor.fetchall()}

    def claim_for_processing(self, record_key: str, task_id: str) -> JobClaim | None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_jobs
                SET
                  status = 'processing',
                  attempt = attempt + 1,
                  started_at = NOW(),
                  finished_at = NULL,
                  error_message = '',
                  worker_execution_id = %s,
                  updated_at = NOW()
                WHERE record_key = %s
                  AND status = 'dispatching'
                RETURNING
                  record_key, batch_id, attempt, input_json, report_fields,
                  report_id, seldon_id, etp_id
                """,
                (task_id, record_key),
            )
            row = cursor.fetchone()
            connection.commit()
        return JobClaim.model_validate(row) if row else None

    def release_dispatch(self, record_key: str, reason: str) -> bool:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_jobs
                SET status = 'queued',
                    worker_execution_id = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    error_message = %s,
                    updated_at = NOW()
                WHERE record_key = %s
                  AND status = 'dispatching'
                RETURNING record_key
                """,
                (reason[:10_000], record_key),
            )
            changed = cursor.fetchone() is not None
            connection.commit()
            return changed

    def complete_job(self, record_key: str, task_id: str, result: dict[str, Any]) -> bool:
        result_json = json.dumps(result, ensure_ascii=False, default=str)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH updated AS (
                  UPDATE tender_autofill_jobs
                  SET
                    status = 'completed',
                    result_json = %s::jsonb,
                    report_fields = COALESCE((%s::jsonb)->'reportFields', report_fields),
                    error_message = '',
                    finished_at = NOW(),
                    updated_at = NOW()
                  WHERE record_key = %s
                    AND status = 'processing'
                    AND worker_execution_id = %s
                  RETURNING batch_id
                ), counts AS (
                  SELECT
                    j.batch_id,
                    COUNT(*) FILTER (WHERE j.status = 'completed')::integer AS completed_count,
                    COUNT(*) FILTER (WHERE j.status = 'failed')::integer AS failed_count
                  FROM tender_autofill_jobs AS j
                  WHERE j.batch_id IN (SELECT batch_id FROM updated)
                  GROUP BY j.batch_id
                )
                UPDATE tender_autofill_batches AS b
                SET completed_count = counts.completed_count,
                    failed_count = counts.failed_count,
                    updated_at = NOW()
                FROM counts
                WHERE b.batch_id = counts.batch_id
                RETURNING b.batch_id
                """,
                (result_json, result_json, record_key, task_id),
            )
            changed = cursor.fetchone() is not None
            connection.commit()
            return changed

    def fail_job(self, record_key: str, task_id: str, error_message: str) -> bool:
        message = error_message.strip()[:10_000] or "Неизвестная ошибка Python Worker"
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH updated AS (
                  UPDATE tender_autofill_jobs
                  SET status = 'failed',
                      result_json = NULL,
                      error_message = %s,
                      finished_at = NOW(),
                      updated_at = NOW()
                  WHERE record_key = %s
                    AND status = 'processing'
                    AND worker_execution_id = %s
                  RETURNING batch_id
                ), counts AS (
                  SELECT
                    j.batch_id,
                    COUNT(*) FILTER (WHERE j.status = 'completed')::integer AS completed_count,
                    COUNT(*) FILTER (WHERE j.status = 'failed')::integer AS failed_count
                  FROM tender_autofill_jobs AS j
                  WHERE j.batch_id IN (SELECT batch_id FROM updated)
                  GROUP BY j.batch_id
                )
                UPDATE tender_autofill_batches AS b
                SET completed_count = counts.completed_count,
                    failed_count = counts.failed_count,
                    updated_at = NOW()
                FROM counts
                WHERE b.batch_id = counts.batch_id
                RETURNING b.batch_id
                """,
                (message, record_key, task_id),
            )
            changed = cursor.fetchone() is not None
            connection.commit()
            return changed

    def start_job_run(
        self,
        *,
        run_id: str,
        record_key: str,
        batch_id: str,
        seldon_id: str | None,
        attempt: int,
        worker_name: str,
    ) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_job_runs
                SET status = 'interrupted',
                    finished_at = COALESCE(finished_at, NOW()),
                    duration_seconds = COALESCE(
                        duration_seconds,
                        EXTRACT(EPOCH FROM (NOW() - started_at))
                    ),
                    error_type = COALESCE(error_type, 'WorkerLost'),
                    error_message = COALESCE(
                        error_message,
                        'Previous attempt was still running when a new attempt started'
                    ),
                    updated_at = NOW()
                WHERE record_key = %s
                  AND status = 'running'
                  AND run_id <> %s
                """,
                (record_key, run_id),
            )
            cursor.execute(
                """
                INSERT INTO tender_autofill_job_runs (
                    run_id, record_key, batch_id, seldon_id, attempt,
                    worker_name, status, started_at, heartbeat_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'running', NOW(), NOW(), NOW())
                ON CONFLICT (run_id) DO UPDATE SET
                    worker_name = EXCLUDED.worker_name,
                    heartbeat_at = NOW(),
                    updated_at = NOW()
                """,
                (run_id, record_key, batch_id, seldon_id, attempt, worker_name),
            )
            connection.commit()

    def heartbeat_job_run(self, run_id: str, memory_rss_mb: float) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_job_runs
                SET heartbeat_at = NOW(),
                    peak_memory_rss_mb = GREATEST(peak_memory_rss_mb, %s),
                    updated_at = NOW()
                WHERE run_id = %s AND status = 'running'
                """,
                (memory_rss_mb, run_id),
            )
            connection.commit()

    def update_job_run_stage(self, run_id: str, stage: str, memory_rss_mb: float) -> None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_job_runs
                SET current_stage = %s,
                    stage_started_at = NOW(),
                    heartbeat_at = NOW(),
                    peak_memory_rss_mb = GREATEST(peak_memory_rss_mb, %s),
                    updated_at = NOW()
                WHERE run_id = %s AND status = 'running'
                """,
                (stage, memory_rss_mb, run_id),
            )
            connection.commit()

    def increment_job_run_counters(self, run_id: str, counters: dict[str, int]) -> None:
        values = {
            key: max(0, int(value))
            for key, value in counters.items()
            if key in self._OBSERVABILITY_COUNTERS and int(value) != 0
        }
        if not values:
            return
        assignments = ", ".join(f"{key} = {key} + %s" for key in values)
        parameters: list[Any] = [*values.values(), run_id]
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE tender_autofill_job_runs
                SET {assignments}, heartbeat_at = NOW(), updated_at = NOW()
                WHERE run_id = %s
                """,
                parameters,
            )
            connection.commit()

    def append_job_event(
        self,
        event: dict[str, Any],
        counters: dict[str, int] | None = None,
    ) -> None:
        details_json = json.dumps(event.get("details") or {}, ensure_ascii=False, default=str)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tender_autofill_job_events (
                    run_id, record_key, batch_id, attempt, event_type, stage,
                    status, service, operation, model, primary_model,
                    provider_request_id, http_method, http_status,
                    duration_seconds, memory_rss_mb, prompt_tokens,
                    completion_tokens, total_tokens, result_count, byte_count,
                    error_type, error_message, details
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s::jsonb
                )
                """,
                (
                    event["run_id"], event["record_key"], event["batch_id"],
                    event["attempt"], event["event_type"], event.get("stage"),
                    event["status"], event.get("service"), event.get("operation"),
                    event.get("model"), event.get("primary_model"),
                    event.get("provider_request_id"), event.get("http_method"),
                    event.get("http_status"), event.get("duration_seconds"),
                    event.get("memory_rss_mb"), event.get("prompt_tokens"),
                    event.get("completion_tokens"), event.get("total_tokens"),
                    event.get("result_count"), event.get("byte_count"),
                    event.get("error_type"), str(event.get("error_message") or "")[:10_000] or None,
                    details_json,
                ),
            )
            metric_increments: list[tuple[str, str, str, int]] = []
            service = str(event.get("service") or "")
            status = str(event.get("status") or "unknown")
            model = str(event.get("model") or "unknown")
            if event.get("event_type") == "external_call" and service == "llm":
                metric_increments.extend(
                    [
                        ("llm_requests", model, status, 1),
                        ("llm_prompt_tokens", model, status, int(event.get("prompt_tokens") or 0)),
                        (
                            "llm_completion_tokens",
                            model,
                            status,
                            int(event.get("completion_tokens") or 0),
                        ),
                    ]
                )
                details = event.get("details") or {}
                performance = details.get("llmPerformance") if isinstance(details, dict) else {}
                if bool(details.get("fallbackUsed")):
                    metric_increments.append(("llm_fallbacks", model, status, 1))
                if isinstance(performance, dict):
                    metric_mapping = {
                        "logicalCalls": "llm_logical_calls",
                        "physicalCalls": "llm_physical_calls",
                        "fallbackCalls": "llm_fallback_calls",
                        "retriedCalls": "llm_retried_calls",
                        "failedCalls": "llm_failed_calls",
                        "truncatedCalls": "llm_truncated_calls",
                        "rateLimitedCalls": "llm_rate_limited_calls",
                        "timeoutCalls": "llm_timeout_calls",
                    }
                    for source_key, metric_name in metric_mapping.items():
                        value = int(performance.get(source_key) or 0)
                        if value:
                            metric_increments.append((metric_name, model, status, value))
                    for source_key, metric_name in {
                        "totalLlmSeconds": "llm_seconds_total",
                        "failedLlmSeconds": "llm_failed_seconds_total",
                        "fallbackLlmSeconds": "llm_fallback_seconds_total",
                    }.items():
                        seconds = float(performance.get(source_key) or 0)
                        if seconds > 0:
                            metric_increments.append((metric_name, model, status, int(round(seconds * 1000))))
            elif event.get("event_type") == "external_call" and service == "qdrant":
                metric_increments.extend(
                    [
                        ("qdrant_queries", "", status, 1),
                        ("qdrant_results", "", status, int(event.get("result_count") or 0)),
                    ]
                )
            elif event.get("event_type") == "external_call" and service == "qdrant_http":
                metric_increments.append(("qdrant_http_requests", "", status, 1))
            elif event.get("event_type") == "external_call" and service == "embedding":
                metric_increments.append(("embedding_queries", model, status, 1))
            elif event.get("event_type") == "external_call" and service == "ollama_http":
                metric_increments.append(("embedding_http_requests", model, status, 1))

            for metric_name, label_model, label_status, increment in metric_increments:
                if increment == 0:
                    continue
                cursor.execute(
                    """
                    INSERT INTO tender_autofill_metric_counters (
                        metric_name, label_model, label_status, value, updated_at
                    ) VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (metric_name, label_model, label_status)
                    DO UPDATE SET
                        value = tender_autofill_metric_counters.value + EXCLUDED.value,
                        updated_at = NOW()
                    """,
                    (metric_name, label_model, label_status, increment),
                )
            run_values = {
                key: max(0, int(value))
                for key, value in (counters or {}).items()
                if key in self._OBSERVABILITY_COUNTERS and int(value) != 0
            }
            if run_values:
                assignments = ", ".join(f"{key} = {key} + %s" for key in run_values)
                cursor.execute(
                    f"""
                    UPDATE tender_autofill_job_runs
                    SET {assignments}, heartbeat_at = NOW(), updated_at = NOW()
                    WHERE run_id = %s
                    """,
                    [*run_values.values(), event["run_id"]],
                )
            connection.commit()

    def finish_job_run(
        self,
        *,
        run_id: str,
        status: str,
        memory_rss_mb: float,
        warnings_count: int = 0,
        error_type: str | None = None,
        error_message: str | None = None,
        result_summary: dict[str, Any] | None = None,
    ) -> None:
        summary_json = json.dumps(result_summary or {}, ensure_ascii=False, default=str)
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tender_autofill_job_runs
                SET status = %s,
                    current_stage = %s,
                    heartbeat_at = NOW(),
                    finished_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at)),
                    peak_memory_rss_mb = GREATEST(peak_memory_rss_mb, %s),
                    warnings_count = GREATEST(warnings_count, %s),
                    error_type = %s,
                    error_message = %s,
                    result_summary = %s::jsonb,
                    updated_at = NOW()
                WHERE run_id = %s
                """,
                (
                    status,
                    "completed" if status == "completed" else "failed",
                    memory_rss_mb,
                    max(0, warnings_count),
                    error_type,
                    str(error_message or "")[:10_000] or None,
                    summary_json,
                    run_id,
                ),
            )
            connection.commit()

    def observability_metrics(self, stale_after_seconds: int = 120) -> dict[str, Any]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'running')::bigint AS running,
                    COUNT(*) FILTER (
                        WHERE status = 'running'
                          AND heartbeat_at < NOW() - (%s * INTERVAL '1 second')
                    )::bigint AS stale,
                    COUNT(*) FILTER (WHERE status = 'completed')::bigint AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed')::bigint AS failed,
                    COUNT(*) FILTER (WHERE status = 'interrupted')::bigint AS interrupted,
                    COALESCE(SUM(llm_requests), 0)::bigint AS llm_requests,
                    COALESCE(SUM(llm_failures), 0)::bigint AS llm_failures,
                    COALESCE(SUM(llm_prompt_tokens), 0)::bigint AS llm_prompt_tokens,
                    COALESCE(SUM(llm_completion_tokens), 0)::bigint AS llm_completion_tokens,
                    COALESCE(SUM(llm_total_tokens), 0)::bigint AS llm_total_tokens,
                    COALESCE(SUM(llm_fallbacks), 0)::bigint AS llm_fallbacks,
                    COALESCE(SUM(embedding_queries), 0)::bigint AS embedding_queries,
                    COALESCE(SUM(embedding_http_requests), 0)::bigint AS embedding_http_requests,
                    COALESCE(SUM(qdrant_queries), 0)::bigint AS qdrant_queries,
                    COALESCE(SUM(qdrant_http_requests), 0)::bigint AS qdrant_http_requests,
                    COALESCE(SUM(qdrant_results), 0)::bigint AS qdrant_results,
                    COALESCE(SUM(download_bytes), 0)::bigint AS download_bytes
                FROM tender_autofill_job_runs
                """,
                (max(1, int(stale_after_seconds)),),
            )
            totals = dict(cursor.fetchone() or {})
            cursor.execute(
                """
                SELECT
                    label_model AS model,
                    label_status AS status,
                    COALESCE(MAX(value) FILTER (WHERE metric_name = 'llm_requests'), 0)::bigint
                        AS requests,
                    COALESCE(MAX(value) FILTER (WHERE metric_name = 'llm_prompt_tokens'), 0)::bigint
                        AS prompt_tokens,
                    COALESCE(MAX(value) FILTER (WHERE metric_name = 'llm_completion_tokens'), 0)::bigint
                        AS completion_tokens
                FROM tender_autofill_metric_counters
                WHERE metric_name IN (
                    'llm_requests', 'llm_prompt_tokens', 'llm_completion_tokens'
                )
                GROUP BY label_model, label_status
                """
            )
            models = [dict(row) for row in cursor.fetchall()]
        return {"totals": totals, "llm_models": models}

    def get_job_runs(self, record_key: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM tender_autofill_job_runs
                WHERE record_key = %s
                ORDER BY attempt DESC, started_at DESC
                LIMIT %s
                """,
                (record_key, min(max(limit, 1), 100)),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_run_with_events(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM tender_autofill_job_runs WHERE run_id = %s", (run_id,))
            run = cursor.fetchone()
            if run is None:
                return None
            cursor.execute(
                """
                SELECT * FROM tender_autofill_job_events
                WHERE run_id = %s
                ORDER BY event_time, event_id
                """,
                (run_id,),
            )
            return {"run": dict(run), "events": [dict(row) for row in cursor.fetchall()]}


_repository: Repository | None = None


def get_repository() -> Repository:
    global _repository
    if _repository is None:
        _repository = Repository()
    return _repository
