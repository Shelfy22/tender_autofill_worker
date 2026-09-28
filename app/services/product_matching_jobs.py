from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from app.config import Settings


class ProductMatchingJobStore:
    def __init__(self, settings: Settings) -> None:
        self.root = settings.product_matching_jobs_root

    def create(self, job_id: str, *, tender_name: str, files: list[str]) -> dict[str, Any]:
        directory = self._directory(job_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "documents").mkdir()
        state = {
            "jobId": job_id,
            "status": "uploading",
            "tenderName": tender_name,
            "files": files,
            "createdAt": self._now(),
            "updatedAt": self._now(),
        }
        self._write_state(directory, state)
        return state

    def document_path(self, job_id: str, index: int, file_name: str) -> Path:
        return self._directory(job_id) / "documents" / f"{index:03d}_{file_name}"

    def queue(self, job_id: str) -> dict[str, Any]:
        return self._update(job_id, status="queued")

    def claim(self, job_id: str) -> dict[str, Any] | None:
        state = self.read(job_id)
        if state is None or state.get("status") != "queued":
            return None
        return self._update(job_id, status="processing", startedAt=self._now())

    def complete(
        self,
        job_id: str,
        *,
        report_file_name: str,
        report_disk_path: str | None,
        warnings: list[str],
    ) -> dict[str, Any]:
        return self._update(
            job_id,
            status="completed",
            finishedAt=self._now(),
            reportFileName=report_file_name,
            reportDiskPath=report_disk_path,
            warnings=warnings[:100],
        )

    def fail(self, job_id: str, error_type: str) -> dict[str, Any]:
        return self._update(
            job_id,
            status="failed",
            finishedAt=self._now(),
            errorType=error_type,
        )

    def report_path(self, job_id: str) -> Path:
        return self._directory(job_id) / "report.xlsx"

    def read(self, job_id: str) -> dict[str, Any] | None:
        state_path = self._directory(job_id) / "state.json"
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def public_state(self, job_id: str) -> dict[str, Any] | None:
        state = self.read(job_id)
        if state is None:
            return None
        return {
            key: state.get(key)
            for key in (
                "jobId",
                "status",
                "tenderName",
                "files",
                "createdAt",
                "updatedAt",
                "startedAt",
                "finishedAt",
                "reportFileName",
                "reportDiskPath",
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
        self._write_state(directory, state)
        return state

    def _directory(self, job_id: str) -> Path:
        normalized = str(UUID(job_id))
        return self.root / normalized

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _write_state(directory: Path, state: dict[str, Any]) -> None:
        temporary = directory / "state.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "state.json")
