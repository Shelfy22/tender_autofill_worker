# Наблюдаемость полного Python Tender Worker

Grafana и Prometheus в `docker-compose.yml` этого проекта не запускаются. Используются уже развёрнутые на сервере экземпляры.

## 1. Что сохраняется

Миграция `migrations/001_observability.sql` добавляет в ту же PostgreSQL две audit-таблицы и одну маленькую таблицу агрегированных counters:

- `tender_autofill_job_runs` — одна строка на одну попытку tender;
- `tender_autofill_job_events` — timeline стадий и внешних вызовов.
- `tender_autofill_metric_counters` — all-time counters для быстрого Prometheus scrape без полного сканирования timeline.

Существующие `tender_autofill_batches` и `tender_autofill_jobs` миграция не изменяет. Binary, тексты документов, LLM prompts и credentials в audit не записываются.

Run хранит `run_id` (Celery task ID), `record_key`, batch/Seldon/attempt, container hostname, status, текущую стадию, heartbeat, длительность, peak RSS и точные counters. Event хранит начало/конец стадии либо один внешний вызов с operation/model/request ID/tokens/duration/error.

Если worker убит OOM/SIGKILL, финального event не будет, но останутся `status=running`, последняя `current_stage` и просроченный `heartbeat_at`. При новой попытке предыдущий незавершённый run этого `record_key` помечается `interrupted`.

## 2. Применение миграции

Сначала сделать backup PostgreSQL. Затем:

```bash
docker compose build tender-api tender-worker
docker compose run --rm --no-deps tender-api python -m app.migrate
```

Команда идемпотентна: используется `CREATE TABLE/INDEX IF NOT EXISTS`. Проверка:

```sql
SELECT to_regclass('public.tender_autofill_job_runs');
SELECT to_regclass('public.tender_autofill_job_events');
SELECT to_regclass('public.tender_autofill_metric_counters');
```

В `.env`:

```dotenv
OBSERVABILITY_ENABLED=true
OBSERVABILITY_HEARTBEAT_SECONDS=30
```

После миграции:

```bash
docker compose up -d --build --scale tender-worker=5
```

## 3. API просмотра execution

С тем же `X-API-Key`, что использует n8n:

```bash
curl -H "X-API-Key: $TENDER_API_KEY" \
  http://server:8000/observability/jobs/RECORD_KEY/runs
```

Timeline конкретной попытки:

```bash
curl -H "X-API-Key: $TENDER_API_KEY" \
  http://server:8000/observability/runs/CELERY_TASK_ID
```

## 4. Подключение существующего Prometheus

Добавить job из `observability/prometheus-scrape.example.yml` в конфигурацию существующего Prometheus.

Если Prometheus находится в общей Docker network:

```yaml
targets: ["tender-api:8000"]
```

Если он находится вне network проекта:

```yaml
targets: ["python-server.example:8000"]
```

После reload проверить Targets и запрос:

```promql
up{job="tender-autofill-python"}
```

Основные метрики:

```promql
tender_autofill_worker_runs{status="running"}
tender_autofill_worker_runs{status="stale"}
tender_autofill_worker_total{metric="llm_requests"}
tender_autofill_worker_total{metric="llm_total_tokens"}
tender_autofill_worker_total{metric="qdrant_queries"}
tender_autofill_worker_total{metric="qdrant_http_requests"}
tender_autofill_worker_total{metric="embedding_queries"}
tender_autofill_llm_requests_by_model_total
tender_autofill_llm_tokens_by_model_total
```

`stale` входит в `running`, но означает, что heartbeat процесса не обновлялся
минимум `max(120 секунд, 4 × OBSERVABILITY_HEARTBEAT_SECONDS)`. Так виден hard OOM
или аварийно уничтоженный контейнер, даже если процесс не успел записать ошибку.

Это gauges, восстановленные из PostgreSQL при каждом scrape. Рестарт `tender-api` не обнуляет статистику, а disposable Celery children не требуют Prometheus multiprocess directory.

## 5. Grafana Overview через существующий Prometheus

1. `Dashboards -> New -> Import`.
2. Загрузить `observability/grafana/tender-autofill-overview.json`.
3. Выбрать существующий Prometheus datasource.
4. Сохранить dashboard.

Dashboard показывает running/completed/errors, LLM calls, Qdrant logical/HTTP calls, embeddings и download bytes.

## 6. Grafana Execution через PostgreSQL

Prometheus намеренно не получает `recordKey`/`run_id` в labels: это вызвало бы высокую cardinality. Подробный execution dashboard читает audit-таблицы напрямую из PostgreSQL.

Рекомендуется отдельный read-only пользователь, с реальным именем базы и новым паролем:

```sql
CREATE ROLE tender_grafana LOGIN PASSWORD 'replace-with-strong-password';
GRANT CONNECT ON DATABASE your_database TO tender_grafana;
GRANT USAGE ON SCHEMA public TO tender_grafana;
GRANT SELECT ON tender_autofill_job_runs TO tender_grafana;
GRANT SELECT ON tender_autofill_job_events TO tender_grafana;
GRANT SELECT ON tender_autofill_metric_counters TO tender_grafana;
```

В существующей Grafana:

1. `Connections -> Data sources -> Add data source -> PostgreSQL`.
2. Указать текущий PostgreSQL host/database, пользователя `tender_grafana` и SSL mode сервера.
3. `Save & test`.
4. Импортировать `observability/grafana/tender-autofill-execution.json`.
5. Выбрать созданный PostgreSQL datasource.

Первая таблица показывает последние execution. Вверху dashboard выбираются `recordKey`, затем
`run_id/attempt`; остальные панели показывают заголовок выбранной попытки, counters, полный
timeline, длительность стадий и отдельные LLM calls.

## 7. SQL для собственных панелей

LLM calls за диапазон Grafana:

```sql
SELECT
  $__timeGroupAlias(event_time, '1h'),
  model,
  COUNT(*) AS calls
FROM tender_autofill_job_events
WHERE $__timeFilter(event_time)
  AND service = 'llm'
  AND event_type = 'external_call'
GROUP BY 1, model
ORDER BY 1;
```

Tokens по моделям:

```sql
SELECT
  model,
  SUM(prompt_tokens) AS prompt_tokens,
  SUM(completion_tokens) AS completion_tokens
FROM tender_autofill_job_events
WHERE $__timeFilter(event_time)
  AND service = 'llm'
GROUP BY model
ORDER BY SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)) DESC;
```

Qdrant logical против HTTP:

```sql
SELECT service, operation, status, COUNT(*) AS calls, SUM(result_count) AS results
FROM tender_autofill_job_events
WHERE $__timeFilter(event_time)
  AND service IN ('qdrant', 'qdrant_http')
GROUP BY service, operation, status
ORDER BY service, operation, status;
```

Зависшие/OOM candidates:

```sql
SELECT
  run_id, record_key, attempt, worker_name, current_stage,
  heartbeat_at, NOW() - heartbeat_at AS heartbeat_age, peak_memory_rss_mb
FROM tender_autofill_job_runs
WHERE status = 'running'
  AND heartbeat_at < NOW() - INTERVAL '2 minutes'
ORDER BY heartbeat_at;
```

Последние execution:

```sql
SELECT
  started_at, record_key, attempt, status, current_stage, duration_seconds,
  llm_requests, qdrant_queries, peak_memory_rss_mb, error_message
FROM tender_autofill_job_runs
WHERE $__timeFilter(started_at)
ORDER BY started_at DESC
LIMIT 500;
```

## 8. Значение fallback counter

Один вызов OpenRouter из Python считается одним `llm_requests`, даже если OpenRouter внутри перебирал providers/models. `llm_fallbacks` увеличивается, когда фактическая `response.model` отличается от primary model. Сохраняются primary, фактическая модель и request ID. Внутреннее число provider attempts OpenRouter не сообщает в обычном Chat Completions response; его можно дополнительно сверять в кабинете/OpenRouter generation API.

## 9. Retention

Timeline со временем растёт. Удаление намеренно не автоматизировано: срок хранения должен
быть согласован с вашей эксплуатацией. Например, после backup можно хранить events 180 дней,
а runs один год:

```sql
DELETE FROM tender_autofill_job_events
WHERE event_time < NOW() - INTERVAL '180 days';

DELETE FROM tender_autofill_job_runs
WHERE finished_at < NOW() - INTERVAL '365 days'
  AND status IN ('completed', 'failed', 'interrupted');
```

`tender_autofill_metric_counters` не удалять: это накопительные значения для Prometheus.
