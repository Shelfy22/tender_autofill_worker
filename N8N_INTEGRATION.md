# Интеграция с n8n Controller

Dispatcher и Finalizer остаются без изменений. В Controller сохраняются SQL-ноды:

- stale `dispatching/processing` recovery;
- retry `failed` до 3 попыток;
- `Postgres — занять свободные слоты до 10` (активный параметр сейчас 5);
- `Подготовить jobs для Tender Worker`;
- terminal batch claim и весь CSV/email Finalizer.

Удаляется/отключается только `Запустить свободные Tender Worker` (`Execute Sub-workflow`).

## 1. Новая Code node: `Собрать HTTP batch для Python`

Соединение:

```text
Подготовить jobs для Tender Worker
  -> Собрать HTTP batch для Python
  -> HTTP — dispatch Tender jobs в Python
```

Mode: `Run Once for All Items`.

Код:

```javascript
const jobs = $input.all().map((item) => item.json || {});

if (!jobs.length) {
  return [];
}

return [{
  json: {
    jobs
  }
}];
```

Это сохраняет исходный payload каждого item без повторной сборки полей.

## 2. HTTP Request node

- Method: `POST`
- URL: `={{ $env.TENDER_PYTHON_URL + '/jobs/batch' }}`
- Send Headers: true
- Header `Content-Type`: `application/json`
- Header `X-API-Key`: `={{ $env.TENDER_PYTHON_API_KEY }}`
- Send Body: true
- Body Content Type: JSON
- Specify Body: Using JSON
- JSON Body: `={{ { jobs: $json.jobs } }}`
- Response Format: JSON
- Timeout: `30000` ms

Точный wire JSON:

```json
{
  "jobs": [
    {
      "batchId": "form-2026-08-07-...",
      "batchDate": "2026-08-07",
      "rowNumber": 1,
      "jobRecordKey": "daily:form-...:1:22812391",
      "batchMode": true,
      "batchCacheKey": "daily:form-...:1:22812391",
      "batchResultKey": "daily:form-...:1:22812391",
      "reportId": 1,
      "purchaseType": "223-ФЗ",
      "seldonId": "22812391",
      "etpId": null,
      "toCode": "...",
      "lawCode": "223",
      "remainingDays": 3,
      "reportFields": {},
      "seldonPurchase": {},
      "asyncDispatch": true,
      "controllerDispatchAt": "2026-08-07T...Z"
    }
  ]
}
```

API отвечает `202 Accepted` сразу после публикации в Redis. HTTP node не ждёт завершения tender.

## 3. Ответ API

```json
{
  "status": "accepted",
  "accepted": 5,
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

Если Redis publish явно завершился ошибкой, API сам атомарно возвращает ещё не начавшийся `dispatching` job в `queued`. Если ответ потерян после фактической публикации, PostgreSQL claim делает повторную HTTP-публикацию безопасной: только один task сможет выполнить `dispatching -> processing`.

## 4. Concurrency

Окно Controller и число Docker workers следует менять согласованно:

```text
Controller SQL max active = 5
docker compose up -d --scale tender-worker=5
```

Затем:

```text
Controller SQL max active = 10
docker compose up -d --scale tender-worker=10
```

В `Postgres — занять свободные слоты до 10` параметр сейчас фактически `={{ [5] }}`. Для конфигурации через n8n env замените его на:

```javascript
={{ [Number($env.TENDER_MAX_ACTIVE || 5)] }}
```

Имя advisory lock желательно также сделать нейтральным (`tender_autofill_controller_window`), но это не обязательно для корректности.

## 5. Retry ownership

Не добавляйте retry tender в HTTP node или Celery task. Допустим только короткий retry самого HTTP transport, потому что duplicate publish безопасен. Business retry остаётся в ноде `Postgres — повторить failed до 3 попыток`.

Python увеличивает `attempt` атомарно при `dispatching -> processing` и выбирает LLM model по реальному значению колонки. Это исправляет потерю `attempt` в текущем Controller payload.
