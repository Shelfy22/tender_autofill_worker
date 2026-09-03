from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.config import Settings
from app.observability import RunObserver
from app.services.llm import LlmClient


class FakeRepository:
    def __init__(self) -> None:
        self.started: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.counters: list[dict[str, int]] = []
        self.stages: list[str] = []
        self.finished: dict[str, Any] | None = None

    def start_job_run(self, **values: Any) -> None:
        self.started = values

    def append_job_event(
        self,
        event: dict[str, Any],
        counters: dict[str, int] | None = None,
    ) -> None:
        self.events.append(event)
        if counters:
            self.counters.append(counters)

    def increment_job_run_counters(self, run_id: str, counters: dict[str, int]) -> None:
        assert run_id == "run-1"
        self.counters.append(counters)

    def update_job_run_stage(self, run_id: str, stage: str, memory_rss_mb: float) -> None:
        assert run_id == "run-1"
        assert memory_rss_mb >= 0
        self.stages.append(stage)

    def heartbeat_job_run(self, run_id: str, memory_rss_mb: float) -> None:
        assert run_id == "run-1"

    def finish_job_run(self, **values: Any) -> None:
        self.finished = values


class FakeObserver:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, **values: Any) -> None:
        self.events.append(values)


def make_run_observer(repository: FakeRepository) -> RunObserver:
    return RunObserver(
        repository,  # type: ignore[arg-type]
        run_id="run-1",
        record_key="record-1",
        batch_id="batch-1",
        seldon_id="123",
        attempt=2,
    )


def test_run_observer_persists_stage_timeline_and_completion() -> None:
    repository = FakeRepository()
    observer = make_run_observer(repository)
    observer.start()
    observer.stage_started("LLM fields")
    observer.stage_finished("LLM fields", duration_seconds=1.25)
    observer.finish_completed(
        {
            "fields": {"tenderStatus": "Согласовано КУ ЦП"},
            "warnings": ["warning"],
            "decision": {"decision": "approve"},
            "productCheck": {"coverage": 75, "total": 4, "supplied": 3},
        }
    )

    assert repository.started is not None
    assert repository.stages == ["LLM fields"]
    assert [event["status"] for event in repository.events] == [
        "started",
        "started",
        "completed",
        "completed",
    ]
    assert repository.finished is not None
    assert repository.finished["status"] == "completed"
    assert repository.finished["warnings_count"] == 1
    assert repository.finished["result_summary"]["coverage"] == 75


def test_llm_observation_counts_tokens_and_actual_fallback_model() -> None:
    observer = FakeObserver()
    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        llm_api_key="test",
        llm_model_attempt_1="primary",
        llm_model_attempt_2="fallback",
        llm_model_attempt_3="third",
    )
    client = LlmClient(settings, attempt=1, observer=observer)  # type: ignore[arg-type]
    response = SimpleNamespace(
        id="generation-1",
        model="fallback",
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150),
    )

    client._observe_llm(
        operation="extract_tender_fields",
        primary_model="primary",
        model_chain=["primary", "fallback"],
        started=0.0,
        response=response,
    )

    event = observer.events[0]
    assert event["model"] == "fallback"
    assert event["primary_model"] == "primary"
    assert event["prompt_tokens"] == 120
    assert event["completion_tokens"] == 30
    assert event["counters"]["llm_requests"] == 1
    assert event["counters"]["llm_fallbacks"] == 1
    assert event["details"]["actualPromptTokens"] == 120
    assert event["details"]["actualCompletionTokens"] == 30
    assert event["details"]["actualTotalTokens"] == 150


def test_llm_observation_records_budget_input_hash_and_performance_debug() -> None:
    observer = FakeObserver()
    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        llm_api_key="test",
        llm_model_attempt_1="primary",
        llm_model_attempt_2="fallback",
    )
    client = LlmClient(settings, attempt=3, observer=observer)  # type: ignore[arg-type]
    response = SimpleNamespace(
        id="generation-2",
        model="primary",
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    client._observe_llm(
        operation="analyze_document: spec.xlsx",
        primary_model="primary",
        model_chain=["primary", "fallback"],
        started=0.0,
        response=response,
        configured_max_completion_tokens=24000,
        input_chars=12345,
        schema_chars=678,
        input_sha256="input-hash",
        logical_call_id="logical-id",
        physical_call_index=1,
    )

    event = observer.events[0]
    details = event["details"]
    assert details["jobAttempt"] == 3
    assert details["configuredMaxCompletionTokens"] == 24000
    assert details["timeoutSeconds"] is None
    assert details["configuredMaxAttemptsPerUnit"] is None
    assert details["inputChars"] == 12345
    assert details["schemaChars"] == 678
    assert details["inputSha256"] == "input-hash"
    assert details["logicalCallId"] == "logical-id"
    assert details["outputChars"] == 2
    assert details["llmPerformance"]["logicalCalls"] == 1
    assert details["llmPerformance"]["physicalCalls"] == 1
