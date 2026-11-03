# Интеграция n8n Controller с Python Tender Autofill Service

## Граница

n8n сохраняет Dispatcher, Controller, PostgreSQL-очередь и Finalizer. Controller заменяет тяжёлый `Execute Sub-workflow` быстрым HTTP-вызовом Python. API не принимает полный тендер: он получает ключи уже созданных PostgreSQL jobs.

```dotenv
TENDER_PYTHON_URL=http://tender-api:8000
TENDER_PYTHON_API_KEY=<значение API_KEY у Python>
TENDER_MAX_ACTIVE=10
```

Используйте внутренний Docker DNS/API address, а не `localhost` другого контейнера. Секрет храните в environment/credentials n8n, не в workflow export.

## Нода HTTP Request

Controller оставляет прежними stale/retry SQL и шаг `queued → dispatching`, затем агрегирует claimed records и вызывает:

| Параметр | Значение |
|---|---|
| Method | `POST` |
| URL | `{{$env.TENDER_PYTHON_URL}}/jobs/batch` |
| Header | `X-API-Key: {{$env.TENDER_PYTHON_API_KEY}}` |
| Body | JSON |
| Timeout | 30 секунд: API ждёт только постановку Celery-задачи. |

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
      "controllerDispatchAt": "2026-11-03T00:00:00Z"
    }
  ]
}
```

Собирайте `jobs` из claimed items без строковой сериализации вложенных объектов: `reportFields` и `seldonPurchase` остаются JSON-объектами. Успешный ответ — HTTP `202` и JSON с `accepted`, `rejected`, `jobs`, `rejectedJobs`. При `partially_accepted` не отправляйте заново весь массив: принятые задачи уже опубликованы. Логируйте `rejectedJobs` и оставьте следующий цикл Controller разбираться с очередью. При ошибке HTTP не переводите job в `completed`/`failed` из n8n.

## Finalizer

Finalizer читает `tender_autofill_jobs.result_json`, не делает HTTP-вызов к Python. Исходные Seldon/CSV поля «Дата окончания приёма заявок», «Ссылка на тендер», «Начальная цена», «Конечная цена» не надо выбирать из JSON Python.

Python уже кодирует наружные значения:

- `110` Загружен Seldon, `120` Согласовано ИИ, `130` Отказано ИИ;
- `Код ТО` как `Dep_<код>`;
- ГосОборонЗаказ и Лот делимый как `yes`/`no`;
- известные причины статуса как коды словаря.

Не добавляйте в SQL отчёта выключенные/удалённые поля: национальный режим, отгрузку и сроки, отсрочку оплаты, контрагентские поля, обеспечение и worker-служебные столбцы.

## Проверка развёртывания

1. Проверьте `GET /health` из контейнера n8n.
2. Запустите один известный тендер и проследите `queued → dispatching → processing → completed`.
3. Сверьте `result_json` и CSV Finalizer.
4. Проверьте недоступный API и фатальную ошибку LLM: job не должен исчезнуть, Controller должен суметь повторить его по своей политике.
5. Только затем включайте расписание и увеличивайте `TENDER_MAX_ACTIVE` вместе с числом `tender-worker`.

## Дополнительный workflow автоподбора

`n8n/Tender Product Matching Async.json` использует отдельный API:

```dotenv
TENDER_PRODUCT_MATCHING_URL=http://tender-api:8000/product-matching/jobs
TENDER_PYTHON_API_KEY=<API_KEY>
```

Он отправляет файлы multipart, получает `jobId`, затем опрашивает status/download endpoint. Эта ветка не вмешивается в Daily Controller и Finalizer.
