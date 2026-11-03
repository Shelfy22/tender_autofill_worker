# Миграционный анализ: n8n Worker → Python Tender Autofill Service

## Текущее разделение ответственности

| Область | Владелец |
|---|---|
| CSV/Seldon вход, batch и job | n8n Dispatcher |
| Окно параллельности, retry, stale recovery | PostgreSQL + n8n Controller |
| Документы, LLM, каталог, решение | Python Celery worker |
| CSV/e-mail/следующие шаги | n8n Finalizer |
| Timeline и counters | PostgreSQL observability |

Python использует существующие `tender_autofill_batches` и `tender_autofill_jobs`; он не создаёт миграции этих бизнес-таблиц автоматически.

## Стабильные контракты

Рефакторинг Python не должен менять без совместной проверки:

- статусы и атомарные переходы jobs;
- `input_json` и `result_json` n8n;
- `POST /jobs/batch` и ответ `202`;
- ключи `jobRecordKey`, `batchId`, Seldon/ЭТП;
- Controller как единственный retry owner;
- Finalizer как единственный владелец завершения batch и итогового CSV.

## Что перенесено

В Python перенесены парсинг и безопасное извлечение документов, LLM-анализ и консолидация, товарная дедупликация, каталог/Qdrant, аудит соответствия, детерминированные причины и построение наружного результата. n8n намеренно оставлен оркестратором batch и интеграций.

## Поведение при сбоях

| Сбой | Результат | Восстановление |
|---|---|---|
| API не опубликовал Celery-задачу | job возвращается в `queued` | Следующий Controller pass. |
| Дубликат сообщения Redis | второй worker не проходит claim | Безопасный пропуск. |
| Worker умер в `processing` | stale job | Controller requeue. |
| Не обработан один документ | `completed` возможен с incomplete warning | Ручная проверка при необходимости. |
| Ошибка консолидации/обязательного аудита | `failed` | Controller retry в пределах лимита. |
| Ошибка Яндекс.Диска | `completed` с warning | Отдельная повторная выгрузка. |

## Временные решения

Поля национального режима, отгрузки/сроков и отсрочки оплаты выключены policy switch, а не удалены. Их промпты и archived tests сохраняются для возврата.

Отдельный `tender-product-matching` — вспомогательный сервис тестового/будущего самостоятельного автоподбора. Он изолирован от Daily.

Яндекс.Диск — временное хранилище XLSX. При появлении внутреннего storage следует заменить адаптер выгрузки, сохранив внешний факт файла в `debug.productMatchingReport`, без изменения lifecycle Daily job.

## Безопасный rollout и перенос в GitLab

1. Применить observability migration отдельно от business schema.
2. Собрать image во внутреннем CI и передать секреты через внутренний secret store.
3. Проверить `/health`, один known-good tender и один failed/retry сценарий.
4. Сопоставить `result_json`, автоподбор и CSV Finalizer.
5. Включить production schedule и масштабировать workers постепенно.
6. Перед переносом подтвердить, что runtime не требует доступа к GitHub: контейнер должен запускаться из внутреннего registry/GitLab CI.
