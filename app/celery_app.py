from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app.config import get_settings
from app.logging import configure_logging
from app.tempfiles import cleanup_orphaned_temp_dirs


settings = get_settings()

celery_app = Celery(
    "tender_autofill",
    broker=settings.redis_url.get_secret_value(),
    backend=None,
    include=["app.tasks"],
)
celery_app.conf.update(
    task_default_queue=settings.celery_queue,
    task_acks_late=False,
    task_reject_on_worker_lost=False,
    task_track_started=False,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1,
    task_soft_time_limit=settings.celery_soft_time_limit_seconds,
    task_time_limit=settings.celery_hard_time_limit_seconds,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.celery_hard_time_limit_seconds + 600},
    accept_content=["json"],
    task_serializer="json",
)


@worker_process_init.connect
def configure_child_logging(**_: object) -> None:
    configure_logging(settings.log_level)
    cleanup_orphaned_temp_dirs(settings.temp_root)
