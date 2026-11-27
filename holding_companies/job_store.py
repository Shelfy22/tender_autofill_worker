from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from holding_companies.models import HoldingTenderInput, HoldingTenderResult


class HoldingJobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, job_id: str, payload: HoldingTenderInput) -> dict[str, Any]:
        directory = self._directory(job_id)
        directory.mkdir(parents=True, exist_ok=False)
        self._write_json(directory / "input.json", payload.model_dump(mode="json"))
        state = {
            "jobId": job_id,
            "tenderId": payload.tenderId,
            "attachmentName": payload.attachmentName,
            "status": "queued",
            "createdAt": self._now(),
            "updatedAt": self._now(),
        }
        self._write_json(directory / "state.json", state)
        return state

    def claim(self, job_id: str) -> dict[str, Any] | None:
        state = self.read(job_id)
        if state is None or state.get("status") != "queued":
            return None
        return self._update(job_id, status="processing", startedAt=self._now())

    def input(self, job_id: str) -> HoldingTenderInput:
        directory = self._directory(job_id)
        payload = self._read_json(directory / "input.json")
        if payload is None:
            raise FileNotFoundError(job_id)
        return HoldingTenderInput.model_validate(payload)

    def complete(
        self,
        job_id: str,
        *,
        result: HoldingTenderResult,
        report_file_name: str,
        warnings: list[str],
    ) -> dict[str, Any]:
        directory = self._directory(job_id)
        self._write_json(directory / "result.json", result.model_dump(mode="json"))
        return self._update(
            job_id,
            status="completed",
            finishedAt=self._now(),
            reportFileName=report_file_name,
            warnings=warnings[:100],
        )

    def fail(self, job_id: str, error_type: str) -> dict[str, Any]:
        return self._update(
            job_id,
            status="failed",
            finishedAt=self._now(),
            errorType=error_type,
        )

    def read(self, job_id: str) -> dict[str, Any] | None:
        return self._read_json(self._directory(job_id) / "state.json")

    def result(self, job_id: str) -> HoldingTenderResult | None:
        payload = self._read_json(self._directory(job_id) / "result.json")
        return HoldingTenderResult.model_validate(payload) if payload is not None else None

    def report_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "report.xlsx"

    def public_state(self, job_id: str) -> dict[str, Any] | None:
        state = self.read(job_id)
        if state is None:
            return None
        return {
            key: state.get(key)
            for key in (
                "jobId",
                "tenderId",
                "attachmentName",
                "status",
                "createdAt",
                "updatedAt",
                "startedAt",
                "finishedAt",
                "reportFileName",
                "warnings",
                "errorType",
            )
            if key in state
        }

    def _update(self, job_id: str, **values: Any) -> dict[str, Any]:
        directory = self._directory(job_id)
        state = self.read(job_id)
        if state is None:
            raise FileNotFoundError(job_id)
        state.update(values)
        state["updatedAt"] = self._now()
        self._write_json(directory / "state.json", state)
        return state

    def _directory(self, job_id: str) -> Path:
        return self.root / str(UUID(job_id))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
