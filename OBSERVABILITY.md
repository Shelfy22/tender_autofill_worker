# Наблюдаемость Tender Autofill Service

## Durable audit

При `OBSERVABILITY_ENABLED=true` сервис записывает историю каждой попытки Celery в PostgreSQL. Миграция `migrations/001_observability.sql` не изменяет `tender_autofill_batches` и `tender_autofill_jobs`.

| Таблица | Содержимое |
|---|---|
| `tender_autofill_job_runs` | Одна запись на попытку: статус, время, этап, RSS, счётчики LLM/документов/каталога, ошибка и краткий итог. |
| `tender_autofill_job_events` | Упорядоченная timeline этапов и внешних вызовов. |
| `tender_autofill_metric_counters` | Накопительные счётчики для Prometheus без сканирования всей timeline. |

Binary, тексты документов, LLM prompts и credentials в audit не записываются.

## Установка

Примените SQL к той же PostgreSQL, которую использует n8n:

```powershell
Get-Content -Raw migrations/001_observability.sql | psql $env:POSTGRES_DSN
```

```dotenv
OBSERVABILITY_ENABLED=true
OBSERVABILITY_HEARTBEAT_SECONDS=30
```

Если audit временно недоступен, worker отключает observability для текущего запуска и продолжает тендер. Soft timeout при этом всё равно остаётся фатальным.

## API

Все маршруты, кроме health/metrics по вашей сетевой политике, используют `X-API-Key`:

| Route | Назначение |
|---|---|
| `GET /observability/jobs/{record_key}/runs?limit=20` | История попыток job. |
| `GET /observability/runs/{run_id}` | Run и timeline событий. |
| `GET /metrics` | Prometheus: dispatch/reject, runs, LLM, embedding, Qdrant, downloads. |
| `GET /health` | API, PostgreSQL и Redis. |

`/metrics` остаётся доступным при отключённой observability, но без агрегированных PostgreSQL counters. Ошибка чтения метрик не ломает API.

## Диагностика

| Симптом | Где смотреть |
|---|---|
| `processing` завис | Последний heartbeat/run, затем stale SQL Controller. |
| `failed` | `error_type`, `error_message`, последний failed stage, `attempt`. |
| Частичная документация | `result_json.debug.documentAnalysis.incompleteUnitIds` и `warnings`. |
| Нет XLSX | `debug.productMatchingReport`, warning и storage adapter. |
| Медленная/дорогая LLM | Tokens и model/status в events/counters. |
| Нет товарных кандидатов | Qdrant/embedding counters, events и `productCheck`. |

Шаблоны Grafana: `observability/grafana/tender-autofill-overview.json` и `observability/grafana/tender-autofill-execution.json`. Пример scrape — `observability/prometheus-scrape.example.yml`.

## Retention

Audit-таблицы растут на каждую попытку и этап. Retention следует вводить отдельной согласованной SQL-задачей; нельзя удалять runs, пока они нужны для расследования инцидентов или связанной timeline.
