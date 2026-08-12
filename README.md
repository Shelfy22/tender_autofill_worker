# Tender Autofill Python Worker

Python 3.12 service, заменяющий тяжёлый n8n `Tender Autofill Worker`. Dispatcher, PostgreSQL job queue и Finalizer остаются в n8n.

PostgreSQL в этом Compose не запускается. `POSTGRES_DSN` должен указывать на существующую базу n8n с таблицами `tender_autofill_batches` и `tender_autofill_jobs`.

Redis, наоборот, запускается отдельный: сервис `tender-redis` обслуживает только Celery/Python и не имеет отношения к Redis/BullMQ n8n. Его порт не публикуется наружу, а доступ ограничен внутренней Docker network `tender-queue`.

Перед запуском прочитайте [PROCESS_FLOW.md](PROCESS_FLOW.md) и [MIGRATION_ANALYSIS.md](MIGRATION_ANALYSIS.md), особенно раздел с неподтверждённым catalog matching contract.

## Запуск

```bash
cp .env.example .env
# заполнить POSTGRES_DSN, Seldon, LLM и catalog credentials
docker compose up -d --build --scale tender-worker=5
```

Проверки:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/metrics
docker compose logs -f tender-api tender-worker
```

Полный audit каждой попытки, timeline стадий, точные LLM/Qdrant/Ollama counters и подключение уже существующих Prometheus/Grafana описаны в [OBSERVABILITY.md](OBSERVABILITY.md). Проект не запускает собственные Grafana/Prometheus containers.

Масштабирование:

```bash
docker compose up -d --scale tender-worker=10
```

Каждый worker container использует `concurrency=1`; Celery child уничтожается после одного tender.

## Тесты

```bash
python -m pytest
```

## Важные условия production rollout

1. Отозвать credentials, обнаруженные внутри n8n export, и использовать только environment variables.
2. Для внешнего Qdrant задать `CATALOG_MODE=qdrant`, `QDRANT_URL`, коллекцию и при необходимости API key/vector name. Обращение выполняется напрямую по REST; локальный Qdrant и Python SDK не используются.
3. Сверить Qdrant payload schema на реальных catalog points.
4. Прогнать shadow comparison `result_json` Python vs n8n на наборе реальных tender разных типов.
5. После parity test заменить Execute Sub-workflow по [N8N_INTEGRATION.md](N8N_INTEGRATION.md).

RAR4/RAR5 распаковываются системными `lsar/unar` с проверкой путей и размеров. XLS сначала преобразуется LibreOffice в XLSX; XLSX потоково читается `openpyxl` с сохранением sheet/row/column coordinates. OpenRouter model fallback для каждого AI-вызова управляется `LLM_ENABLE_MODEL_FALLBACK` и `LLM_FALLBACK_MODELS`; подробности — в [PROCESS_FLOW.md](PROCESS_FLOW.md).
