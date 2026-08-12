# Архитектура Python Tender Autofill Service

## Решение

Используется FastAPI + Celery + Redis + PostgreSQL.

Celery выбран потому, что для этой нагрузки критичны готовые process isolation controls:

- prefork worker process;
- `worker_concurrency` и deployment с `--concurrency=1`;
- `worker_max_tasks_per_child=1` для уничтожения дочернего процесса после каждого tender;
- soft/hard task time limits;
- Redis broker;
- сигналы и стандартные метрики.

Dramatiq легче, но lifecycle child process и hard time limits потребовали бы дополнительного supervisor-кода. RQ проще, но слабее для управляемого prefork lifecycle. Отдельный самописный queue runner здесь неоправдан.

## Поток данных

```text
n8n Dispatcher
  -> PostgreSQL jobs=queued

n8n Controller
  -> atomic queued -> dispatching
  -> Aggregate claimed items
  -> POST /jobs/batch (быстрый 202)
       -> validate jobRecordKey/batchId
       -> publish record_key to Redis/Celery

Celery worker container (concurrency=1)
  -> fetch input_json from PostgreSQL
  -> atomic dispatching -> processing, attempt+1
  -> one TemporaryDirectory per tender
  -> execute pipeline
  -> processing -> completed + compatible result_json
  -> on catchable failure: processing -> failed
  -> process exits after one task

n8n Controller
  -> owns retry/requeue
  -> existing Finalizer reads result_json
```

В Redis передаётся только `record_key`/`batch_id`, не документы и не полный binary payload. PostgreSQL остаётся источником job input/status/result. Документы существуют только в isolated temporary directory текущего tender.

## Границы ответственности

### n8n

- Dispatcher и запись исходных jobs;
- claim окна `queued -> dispatching`;
- retry policy, max attempts и stale recovery;
- terminal batch detection;
- CSV/email Finalizer.

### Python API

- принимает до `API_MAX_BATCH_JOBS` claimed jobs;
- проверяет формат и наличие job в `dispatching`;
- публикует задачи;
- отвечает 202, не ждёт processing.

### Python worker

- атомарно claim `dispatching -> processing`;
- увеличивает попытку;
- выполняет один tender;
- пишет `completed/result_json` или `failed/error_message`;
- не выполняет retry.

## Идемпотентность и гонки

PostgreSQL status transition — финальный idempotency gate. Дубликат сообщения Redis не запустит второй processing, потому что только один worker сможет выполнить `UPDATE ... WHERE status='dispatching' RETURNING`.

Если API опубликовал задачу, но worker не стартовал, Controller вернёт stale `dispatching` в queued. Старое сообщение после этого не сможет claim job. Если процесс погиб после claim, stale `processing` восстановит Controller.

Celery application retry отключён. `acks_late=false`: OOM не создаёт независимый бесконечный broker retry; восстановление выполняется PostgreSQL Controller.

## Process/memory isolation

- один worker container: `--concurrency=1 --pool=prefork`;
- один child: один tender (`--max-tasks-per-child=1`);
- Compose scaling: `docker compose up -d --scale tender-worker=5`;
- optional container memory limits на deployment level;
- soft/hard task limits;
- subprocess limits для LibreOffice/7z;
- HTTP connect/read timeout;
- per-stage elapsed logging и RSS memory.

Defaults: soft 25 минут, hard 28 минут. Это намеренно меньше существующего n8n stale-processing
порога 30 минут; увеличивать Python timeout без одновременного изменения Controller нельзя.

`TemporaryDirectory` удаляется в normal/error path. Если child уничтожен hard timeout/OOM и `finally`
невозможен, новый prefork child до получения следующей задачи удаляет только service-owned `tender-*`
directories из `TEMP_ROOT`. После restart container выполняется та же очистка.

Если нужен один OS process без Celery parent на tender, потребуется Kubernetes/nomad job-per-tender или отдельный spawning supervisor. Для Docker Compose Celery parent + disposable prefork child даёт требуемую изоляцию при меньшей сложности.

## Структура проекта

```text
app/
  api.py                 FastAPI routes
  celery_app.py          Celery configuration
  config.py              environment settings
  db.py                  PostgreSQL repository/status transitions
  logging.py             structured context/stage timing/RSS
  models.py              Pydantic API/LLM/result contracts
  tasks.py               Celery task boundary and temp lifecycle
  pipeline.py            orchestration only
  services/
    normalization.py
    seldon.py
    documents.py
    customer.py
    catalog.py
    coverage.py
    decision.py
    result.py
    llm.py
    parsers/
      common.py
      archives.py
      pdf.py
      word.py
      spreadsheets.py
tests/
```

## Limits

Настраиваются environment variables:

- размер одного download и общий размер downloads;
- max document count;
- archive depth/count/uncompressed bytes/compression ratio;
- max text per file и combined LLM chars;
- HTTP, conversion и task timeouts;
- max API batch jobs;
- max LLM output tokens.

Защита archive extraction проверяет canonical destination path, количество members, declared/uncompressed size, compression ratio и запрещает links/path traversal. Nested archive depth ограничен.

## API

### `POST /jobs/batch`

Input:

```json
{
  "jobs": [
    {
      "jobRecordKey": "daily:form-...:1:123",
      "batchId": "form-...",
      "reportId": 1,
      "seldonId": "123",
      "reportFields": {},
      "toCode": "..."
    }
  ]
}
```

Response `202 Accepted`:

```json
{
  "status": "accepted",
  "accepted": 1,
  "rejected": 0,
  "jobs": [{"jobRecordKey":"...","taskId":"..."}]
}
```

### `GET /health`

Проверяет процесс, PostgreSQL и Redis.

### `GET /metrics`

Prometheus metrics для API accepts/rejects. Длительности и outcome worker доступны в structured logs;
для их централизованного Prometheus-сбора нужен log collector/OpenTelemetry или Pushgateway.

## LLM

Все LLM calls идут через OpenAI-compatible client с JSON structured output, отдельными Pydantic schemas и timeout. Mapping попыток задаётся:

```text
LLM_MODEL_ATTEMPT_1
LLM_MODEL_ATTEMPT_2
LLM_MODEL_ATTEMPT_3
```

Defaults отражают экспортированный workflow, а не неподтверждённый список GPT. Истинный attempt берётся из атомарного PostgreSQL claim.

## Catalog adapter

`CatalogMatcher` имеет два режима:

- `http` — рекомендуемый контракт внутреннего catalog search endpoint;
- `qdrant` — прямой HTTP REST search во внешнем Qdrant после Ollama embedding. Сервис Qdrant в Compose и Python SDK не используются. Современный `/points/query` имеет fallback на `/points/search` для старой версии сервера.

Поскольку exported workflow не содержит соединённой product matching цепочки и payload schema Qdrant, адаптер не должен угадывать payload keys. До задания `CATALOG_*` он возвращает controlled `Товар не найден` и warning. Это сохраняет безопасность, но не является подтверждённым эквивалентом каталожного этапа.

## Изменение Controller

Ноды до `Подготовить jobs для Tender Worker` сохраняются. `Запустить свободные Tender Worker` заменяется:

1. Aggregate всех items в `{jobs: [...]}`;
2. HTTP Request POST `${TENDER_PYTHON_URL}/jobs/batch`;
3. timeout 30s, response JSON, не ждать processing.

Точный n8n body приведён в `N8N_INTEGRATION.md`.
