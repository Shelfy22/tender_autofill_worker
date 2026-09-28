from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.services.product_matching_jobs import ProductMatchingJobStore


def settings(tmp_path: Path) -> Settings:
    return Settings(
        postgres_dsn="postgresql://user:password@localhost/database",
        product_matching_jobs_root=tmp_path / "matching-jobs",
    )


def test_product_matching_job_state_lifecycle(tmp_path: Path) -> None:
    store = ProductMatchingJobStore(settings(tmp_path))
    job_id = str(uuid4())

    created = store.create(job_id, tender_name="Тендер 700", files=["documents.zip"])
    document = store.document_path(job_id, 1, "documents.zip")
    document.write_bytes(b"archive")

    assert created["status"] == "uploading"
    assert store.queue(job_id)["status"] == "queued"
    assert store.claim(job_id) is not None
    assert store.claim(job_id) is None

    report = store.report_path(job_id)
    report.write_bytes(b"xlsx")
    store.complete(
        job_id,
        report_file_name="autopodbor_700.xlsx",
        report_disk_path="disk:/Тендеры автоподбор/autopodbor_700.xlsx",
        warnings=["diagnostic"],
    )

    assert store.public_state(job_id)["status"] == "completed"
    assert store.public_state(job_id)["reportDiskPath"] == "disk:/Тендеры автоподбор/autopodbor_700.xlsx"
