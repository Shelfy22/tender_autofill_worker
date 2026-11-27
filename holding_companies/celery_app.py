from __future__ import annotations

from celery import Celery

from holding_companies.settings import get_holding_runtime_settings


runtime = get_holding_runtime_settings()
celery_app = Celery(
    "holding_companies",
    broker=runtime.redis_url,
    backend=None,
    include=["holding_companies.tasks"],
)
celery_app.conf.update(
    task_default_queue=runtime.celery_queue,
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    task_track_started=False,
    task_ignore_result=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1,
    task_soft_time_limit=runtime.soft_time_limit_seconds,
    task_time_limit=runtime.hard_time_limit_seconds,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": runtime.hard_time_limit_seconds + 600},
    accept_content=["json"],
    task_serializer="json",
)
