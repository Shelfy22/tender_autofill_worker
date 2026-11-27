from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HoldingRuntimeSettings:
    jobs_root: Path
    celery_queue: str
    soft_time_limit_seconds: int
    hard_time_limit_seconds: int
    api_key: str | None
    redis_url: str


def get_holding_runtime_settings() -> HoldingRuntimeSettings:
    api_key = os.getenv("API_KEY", "").strip() or None
    return HoldingRuntimeSettings(
        jobs_root=Path(
            os.getenv(
                "HOLDING_COMPANIES_JOBS_ROOT",
                "/tmp/tender-autofill/holding-company-jobs",
            )
        ),
        celery_queue=os.getenv("HOLDING_COMPANIES_CELERY_QUEUE", "holding-companies"),
        soft_time_limit_seconds=int(
            os.getenv("HOLDING_COMPANIES_SOFT_TIME_LIMIT_SECONDS", "1800")
        ),
        hard_time_limit_seconds=int(
            os.getenv("HOLDING_COMPANIES_HARD_TIME_LIMIT_SECONDS", "1920")
        ),
        api_key=api_key,
        redis_url=os.getenv("REDIS_URL", "redis://tender-redis:6379/0"),
    )
