# Интеграция Python Worker с n8n Controller

Готовая импортируемая версия находится в:

```text
reference/Daily Tender Controller + Finalizer.json
```

Имя после импорта:

```text
Daily Tender Controller + Finalizer — Python HTTP TEST 1
```

Workflow специально экспортирован с `active=false` и без исходных `id/versionId`,
поэтому его можно сначала импортировать рядом с production Controller и проверить
вручную. Старый workflow автоматически не изменяется.

## 1. Что изменено

Dispatcher и весь Finalizer сохранены. Сохранены также:

- stale recovery для `dispatching/processing`;
- единственный business retry `failed` до трёх попыток;
- атомарный PostgreSQL claim через `FOR UPDATE SKIP LOCKED`;
- получение готового batch, формирование CSV и завершение batch.

Удалён только вызов `Execute Sub-workflow`. Новая цепочка:

```text
Postgres — занять 1 слот (первый тест)
  -> Подготовить jobs для Tender Worker
  -> Собрать HTTP batch для Python
  -> HTTP — dispatch Tender jobs в Python
  -> Проверить ответ Python API
```

Начальный SQL-параметр:

```javascript
={{ [1] }}
```

Поэтому одновременно может быть только один `dispatching + processing` job.

## 2. Environment n8n

HTTP node обращается по внутренней сети `ai-net`:

```text
http://tender-api:8000/jobs/batch
```

Секрет не записан в workflow. Добавьте во все сервисы `n8n-main` и `n8n-worker-*`:

```dotenv
TENDER_PYTHON_API_KEY=<то же значение, что API_KEY в tender_autofill_worker/.env>
```

После изменения n8n Compose пересоздайте его сервисы. Если n8n запрещает `$env`
в expressions, создайте Header Auth credential с header `X-API-Key` и выберите его
в HTTP node вместо expression.

## 3. HTTP-контракт

HTTP Request выполняет один быстрый POST с raw JSON:

```json
{
  "jobs": [
    {
      "batchId": "form-...",
      "jobRecordKey": "daily:form-...:1:22812391",
      "reportId": 1,
      "seldonId": "22812391",
      "reportFields": {},
      "seldonPurchase": {},
      "asyncDispatch": true,
      "maxTenderActive": 1,
      "controllerDispatchAt": "2026-08-13T...Z"
    }
  ]
}
```

Python API отвечает `202 Accepted` сразу после публикации в отдельный Redis:

```json
{
  "status": "accepted",
  "accepted": 1,
  "rejected": 0,
  "jobs": [
    {
      "jobRecordKey": "daily:...",
      "taskId": "uuid"
    }
  ],
  "rejectedJobs": []
}
```

Нода `Проверить ответ Python API` завершает n8n execution ошибкой, если API не
принял весь отправленный batch. Она не создаёт дополнительный business retry.

## 4. Безопасный первый запуск одного tender

1. Импортируйте новый workflow, но не активируйте его.
2. Проверьте PostgreSQL credential во всех Postgres nodes.
3. Убедитесь, что старый Controller пока активен только если в очереди нет тестовых
   jobs. Перед тестом деактивируйте старый Controller, иначе он тоже может сделать claim.
4. Dispatcher должен создать batch с хотя бы одним `queued`, `input_valid=true` job.
5. Нажмите `Execute workflow` вручную один раз.
6. Убедитесь, что HTTP response содержит `accepted=1`, `rejected=0`.
7. Не запускайте Controller второй раз до завершения проверки. Поскольку workflow
   не активирован, следующий queued job автоматически не стартует.

В worker logs должно появиться:

```bash
cd /opt/stack/tender_autofill_worker
docker compose logs -f --since=5m tender-worker
```

Ожидаемые события: task received, переход `dispatching -> processing`, стадии
pipeline, затем `job_completed` либо диагностируемый `job_failed`.

## 5. Проверка PostgreSQL job и result_json

Подставьте реальный `record_key`:

```sql
SELECT
  record_key,
  batch_id,
  status,
  attempt,
  worker_execution_id,
  started_at,
  finished_at,
  error_message,
  jsonb_typeof(result_json) AS result_type,
  result_json ? 'fields' AS has_fields,
  result_json ? 'meta' AS has_meta,
  result_json ? 'productCheck' AS has_product_check,
  result_json ? 'decision' AS has_decision,
  result_json ? 'warnings' AS has_warnings,
  result_json ? 'logs' AS has_logs,
  result_json ? 'debug' AS has_debug,
  result_json ? 'reportId' AS has_report_id,
  result_json ? 'seldonId' AS has_seldon_id,
  result_json ? 'batchId' AS has_batch_id,
  result_json ? 'jobRecordKey' AS has_job_record_key,
  result_json ? 'reportFields' AS has_report_fields
FROM tender_autofill_jobs
WHERE record_key = '<RECORD_KEY>';
```

Для успешного job ожидается `status=completed`, `result_type=object`, все флаги
`has_* = true`, а `jobRecordKey` внутри JSON должен совпадать с колонкой:

```sql
SELECT
  record_key,
  result_json->>'jobRecordKey' AS json_record_key,
  batch_id,
  result_json->>'batchId' AS json_batch_id,
  seldon_id,
  result_json->>'seldonId' AS json_seldon_id,
  report_id,
  result_json->>'reportId' AS json_report_id
FROM tender_autofill_jobs
WHERE record_key = '<RECORD_KEY>';
```

Это именно поля, которые дальше читает существующий Finalizer.

## 6. Проверка Seldon, документов, LLM, Ollama и Qdrant

Заголовок попытки и counters:

```sql
SELECT
  run_id,
  record_key,
  attempt,
  worker_name,
  status,
  current_stage,
  duration_seconds,
  peak_memory_rss_mb,
  documents_requested,
  documents_parsed,
  download_bytes,
  llm_requests,
  llm_successes,
  llm_failures,
  llm_fallbacks,
  embedding_queries,
  embedding_http_requests,
  qdrant_queries,
  qdrant_http_requests,
  qdrant_results,
  error_type,
  error_message
FROM tender_autofill_job_runs
WHERE record_key = '<RECORD_KEY>'
ORDER BY started_at DESC;
```

Полный timeline выбранной попытки:

```sql
SELECT
  event_time,
  event_type,
  stage,
  status,
  service,
  operation,
  primary_model,
  model AS actual_model,
  http_status,
  duration_seconds,
  memory_rss_mb,
  prompt_tokens,
  completion_tokens,
  result_count,
  error_type,
  error_message,
  details
FROM tender_autofill_job_events
WHERE run_id = '<RUN_ID>'
ORDER BY event_time, event_id;
```

Проверка по смыслу:

- Seldon и documents видны как успешные pipeline stages;
- `documents_requested/parsed/download_bytes` подтверждают работу документов;
- `service=llm` содержит каждый OpenRouter call и фактическую модель;
- `service=ollama_http` и `service=embedding` подтверждают embeddings;
- `service=qdrant_http` показывает реальные REST calls;
- `service=qdrant` показывает logical product queries и количество результатов.

## 7. Проверка совместимости Finalizer

Finalizer не требует изменений: он по-прежнему читает `result_json` из
`tender_autofill_jobs`. После завершения всех jobs тестового batch вручную выполните
Controller ещё раз либо активируйте проверенную версию. Проверьте:

```sql
SELECT
  batch_id,
  status,
  expected_count,
  completed_count,
  failed_count,
  finished_at
FROM tender_autofill_batches
WHERE batch_id = '<BATCH_ID>';
```

В execution Finalizer должны успешно пройти ноды:

```text
Postgres — получить jobs batch
  -> Собрать Daily Batch Summary из PostgreSQL
  -> Сформировать CSV по типам тендеров
  -> Создать CSV binary
```

Сравните сформированную строку CSV с `reportFields`, `fields.tenderStatus`,
`fields.tenderStatusReason` и `fields.tenderStatusNote` тестового `result_json`.

## 8. Переход с одного job на пять

Только после успешного end-to-end теста откройте ноду:

```text
Postgres — занять 1 слот (первый тест)
```

и замените Query Parameters:

```javascript
={{ [1] }}
```

на:

```javascript
={{ [5] }}
```

При желании переименуйте ноду в `Postgres — занять свободные слоты до 5`.
Пять Docker workers уже запущены с `concurrency=1`, поэтому Controller и физическая
параллельность совпадут. После этого активируйте новый workflow. Старую версию
Controller оставьте деактивированной.

## 9. Retry ownership

Business retry остаётся только в ноде:

```text
Postgres — повторить failed до 3 попыток
```

Celery task имеет `max_retries=0`, HTTP node не выполняет application retry. Python
атомарно увеличивает `attempt` при `dispatching -> processing` и выбирает модель
по реальному значению attempt.
