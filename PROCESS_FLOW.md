# Полный процесс обработки batch: от Dispatcher до Finalizer

Этот документ описывает целевой production-поток после замены тяжёлого n8n Worker на Python. Источником истины для очереди, статусов, retry и финального результата остаётся существующий PostgreSQL.

Новый Docker Compose не запускает PostgreSQL. `tender-api` и `tender-worker` подключаются через `POSTGRES_DSN` к тому же экземпляру PostgreSQL и тем же таблицам, которые уже используют n8n Dispatcher/Controller/Finalizer. Создавать копию базы или синхронизировать две базы не требуется.

## Общая схема

```text
n8n Daily Tender Dispatcher
  -> PostgreSQL: batch + jobs(status=queued)

n8n Daily Tender Controller (по расписанию или вручную)
  -> stale/retry SQL
  -> queued -> dispatching
  -> Aggregate jobs
  -> POST Python /jobs/batch
       -> Redis/Celery: только record_key + batch_id
       -> 202 Accepted

Python tender-worker, один процесс на один tender
  -> PostgreSQL: dispatching -> processing, attempt + 1
  -> Seldon и документы
  -> извлечение данных и товаров
  -> Ollama HTTP: embedding
  -> внешний Qdrant HTTP: поиск кандидатов
  -> LLM: выбор товара/аналога и решение
  -> PostgreSQL: completed + result_json
     или failed + error_message

n8n Daily Tender Controller + Finalizer
  -> ждёт terminal batch
  -> batch processing -> finalizing
  -> читает result_json всех jobs
  -> создаёт CSV 223/gos/kom
  -> отправляет email
  -> batch finalizing -> finished
```

## 1. n8n Daily Tender Dispatcher

Основной вход — `On form submission`. Пользователь загружает до трёх CSV: `file223`, `fileGos`, `fileKom`; `testLimit` может ограничивать число строк. Отключённый в экспорте `When Executed by Seldon Workflow` является альтернативным входом и в основном потоке не участвует.

Последовательность нод:

1. `Прочитать CSV из формы и собрать пакет` читает CSV как UTF-8 с fallback Windows-1251, определяет разделитель и тип отчёта.
2. `Развернуть и нормализовать JSON-пакет` превращает строки CSV в отдельные tender items и сохраняет исходные колонки в `reportFields`.
3. `Собрать batch и задания` формирует `batchId`, `jobRecordKey`, `reportId`, `seldonId/etpId`, `Код ТО`, `Код ФЗ`, `Осталось дней` и полный `input_json`.
4. `Postgres — создать batch и jobs` записывает batch и jobs в существующие таблицы.
5. `Ответ диспетчера` сообщает, сколько строк принято и сколько признано невалидными.

Валидный job создаётся как:

```text
status=queued
attempt=0
input_valid=true
result_json=NULL
```

Невалидная строка создаётся как terminal `failed` с `input_valid=false`. Controller не будет повторять такую строку, но Finalizer включит её в CSV как ошибку обработки.

Dispatcher ничего не отправляет в Redis и не запускает Python напрямую. Он только создаёт надёжное состояние batch/jobs в PostgreSQL.

## 2. n8n Daily Tender Controller

Один проход Controller запускается нодой `Проверять готовые batch каждую минуту` или `Ручная проверка готовых batch`. В приложенном JSON schedule-нода отключена; для автоматической production-работы её нужно включить либо вызывать workflow внешним расписанием.

В начале каждого прохода Controller выполняет обслуживание очереди:

1. `processing` старше 30 минут возвращает в `queued`.
2. `failed` с `input_valid=true` и `attempt < 3` возвращает в `queued`.
3. Под PostgreSQL advisory lock считает активные `dispatching + processing` jobs.
4. `Postgres — занять свободные слоты до 10` выбирает через `FOR UPDATE SKIP LOCKED` столько `queued` jobs, сколько помещается в настроенное окно, и переводит их в `dispatching`.
5. `Подготовить jobs для Tender Worker` восстанавливает ожидаемый Worker payload.

Активное окно в экспортированном workflow фактически равно 5. Его рекомендуется задавать через `TENDER_MAX_ACTIVE` и держать равным количеству контейнеров `tender-worker`.

### Подключение Python-контейнеров к существующему PostgreSQL

В `.env` указывается DSN той же базы n8n:

```dotenv
POSTGRES_DSN=postgresql://tender_user:password@postgres-host:5432/n8n_database
```

Если PostgreSQL — отдельный сервер, `postgres-host` является его DNS/IP. Если PostgreSQL работает в Docker рядом с n8n, предпочтительно подключить `tender-api` и `tender-worker` к существующей Docker network n8n и использовать имя PostgreSQL service/container как hostname. Альтернатива — опубликованный порт PostgreSQL и `host.docker.internal` на той же машине.

Нельзя использовать `localhost` внутри `tender-api/tender-worker`: там `localhost` означает сам Python-контейнер, а не контейнер PostgreSQL.

Python не создаёт свои версии таблиц и не запускает автоматические schema migrations. Таблицы остаются созданными SQL-нодой Dispatcher `Postgres — создать таблицы`; Python использует доказанные существующие колонки.

## 3. HTTP dispatch из n8n в Python

Существующая нода `Запустить свободные Tender Worker` типа `Execute Sub-workflow` заменяется двумя нодами:

1. Code/Aggregate собирает все claimed items в `{ "jobs": [...] }`.
2. HTTP Request вызывает `POST /jobs/batch`.

Полные параметры и JSON body приведены в `N8N_INTEGRATION.md`.

Python API для каждого item проверяет, что `jobRecordKey` существует в PostgreSQL и всё ещё имеет статус `dispatching`. После этого в Redis публикуются только:

```json
{
  "jobRecordKey": "daily:...",
  "batchId": "..."
}
```

Полный `input_json`, документы и результаты через Redis не передаются. API сразу отвечает `202 Accepted`, поэтому n8n не ждёт обработки tender.

Если публикация в Redis явно не состоялась, API возвращает job из `dispatching` в `queued`. Повторный HTTP-вызов безопасен: только один worker сможет атомарно выполнить следующий переход статуса.

## 4. Redis и масштабирование workers

Redis используется только как транспорт задач Celery. Источником истины остаётся PostgreSQL.

Это отдельный сервис `tender-redis` из данного Compose. Он не использует Redis/BullMQ n8n, не подключён к Docker network n8n и не публикует порт `6379` на host. `tender-api` и `tender-worker` видят его только во внутренней сети `tender-queue`. Очереди, ключи, память и persistence n8n и Python полностью разделены.

Каждый контейнер `tender-worker` работает с:

```text
prefork
concurrency=1
prefetch-multiplier=1
max-tasks-per-child=1
```

Поэтому один контейнер одновременно обрабатывает один tender, а дочерний процесс уничтожается после каждой задачи. Пять контейнеров дают максимум пять одновременных тяжёлых tender, десять контейнеров — десять.

## 5. Обработка одного tender в Python

Получив сообщение, worker не доверяет одному Redis. Он выполняет атомарный SQL:

```text
dispatching -> processing
attempt = attempt + 1
worker_execution_id = Celery task id
```

Если другой worker уже забрал job или Controller изменил его статус, `UPDATE ... WHERE status='dispatching'` не возвращает строку и дубликат задачи пропускается.

Далее создаётся отдельная директория `/tmp/tender-autofill/tender-...`, в которой проходит весь pipeline:

1. Нормализация `input_json`, `batchId`, `jobRecordKey`, `reportId`, `reportFields`, `seldonId`, `Код ТО` и служебных полей.
2. Получение или обновление токена Seldon.
3. Получение списка документов закупки через Seldon.
4. Формирование структурированного текста страницы из Seldon purchase; необязательный HTML GET включается только через `ENABLE_TENDER_HTML_FETCH`.
5. Потоковое скачивание документов с per-file и общим лимитом.
6. Безопасная распаковка ZIP/7Z/RAR и парсинг PDF, DOC/DOCX, XLS/XLSX/CSV.
7. Сбор ограниченного общего текста.
8. LLM extraction полей tender и детерминированная валидация.
9. Определение фактического заказчика и проверка контрагента в IPro.
10. Детерминированное и LLM-извлечение товарных позиций, количества и диагностических цен из документов.
11. Поиск товаров в удалённом каталоге Qdrant.
12. Расчёт coverage, стоимости и детерминированных hard reasons.
13. LLM decision и применение приоритетов существующих статусов.
14. Построение совместимого `result_json`.

При успехе worker атомарно записывает:

```text
processing -> completed
result_json=<объект для Finalizer>
report_fields=result_json.reportFields
finished_at=NOW()
```

Временная директория удаляется. После задачи дочерний worker process завершается, освобождая накопленную память.

### Диагностические цены из документов

Для XLS/XLSX/CSV цена единицы и сумма строки извлекаются детерминированно по заголовкам
колонок. Сохраняются имя файла, лист, строка, колонки, исходные заголовки и evidence.
Для PDF/DOC и неструктурированных таблиц те же поля может вернуть LLM извлечения товарных
позиций. Сумма строки не считается ценой единицы; деление на количество выполняется только
как отдельный диагностический показатель.

`documentUnitPriceRub` и `documentLineTotalRub` не участвуют в коммерческом решении.
`priceEvaluationComplete`, сумма поставки и порог 1 млн руб. по-прежнему рассчитываются
только как цена выбранного товара Qdrant × количество. В `productCheck.details` сохраняются
обе цены, их отклонение, проверка `documentUnitPriceRub × quantity` против суммы строки и
`documentPriceUsedForSupplyValue=false`.

## 6. Удалённый Qdrant по HTTP

Python не запускает контейнер Qdrant, не хранит его volume и не обновляет коллекцию. Коллекцией продолжает управлять существующий внешний процесс на сервере; worker видит её актуальное состояние при каждом поиске.

Поток поиска одной товарной позиции:

```text
productQuery
  -> POST OLLAMA_URL/api/embed
  -> embedding vector
  -> POST QDRANT_URL/collections/{collection}/points/query
  -> top-K id/score/payload
  -> Python нормализует point_id, product_id, цену и реквизиты payload
  -> LLM возвращает только выбранный point_id, тип соответствия и обоснование
  -> Python восстанавливает цену, артикул и ссылку из выбранного payload
```

Для Qdrant используется заголовок `api-key`. Для современных серверов вызывается `/points/query`; если сервер отвечает 404/405, используется совместимый старый `/points/search`. Python SDK `qdrant-client` не нужен.

Переменные окружения:

```dotenv
CATALOG_MODE=qdrant
QDRANT_URL=https://qdrant.company.example
QDRANT_API_KEY=...
QDRANT_COLLECTION=products
QDRANT_VECTOR_NAME=
QDRANT_TOP_K=50

OLLAMA_URL=https://ollama.company.example
OLLAMA_EMBEDDING_MODEL=qwen3-embedder-ft:latest
```

`QDRANT_VECTOR_NAME` оставляется пустым для default/unnamed vector. Если коллекция использует named vector, сюда необходимо записать его точное имя. Размер embedding Ollama должен совпадать с размером vector в коллекции Qdrant.

Поддерживаются текущие и старые названия поля цены (`price`, `Медианная цена`,
`Медианная цена, руб.`, `Цена`, `medianPrice`, `median_price`). Медиана рассчитывается только
между записями с одинаковым `productId`; цены разных товарных кандидатов не смешиваются.
Перед переключением production всё равно нужен shadow test на реальных позициях.

## 7. Ошибки, OOM, timeout и retry

Если Python ловит исключение, он пишет:

```text
processing -> failed
error_message=<тип и текст ошибки>
```

Celery сам tender не повторяет. На следующем проходе n8n Controller вернёт retryable `failed` в `queued`, пока `attempt < 3`.

Если процесс убит OOM/SIGKILL и не успел записать `failed`, job остаётся `processing`. Через 30 минут существующая stale-нода Controller вернёт его в `queued`. Hard timeout Python по умолчанию 28 минут, чтобы не пересекаться со stale-порогом.

После третьей неуспешной попытки job остаётся `failed` и становится terminal. Ошибка одного tender не блокирует остальные worker containers.

## 8. n8n Finalizer

После retry/dispatch ветки Controller проверяет готовые batches. `Postgres — захватить готовый batch` выбирает batch только когда:

- все `expected_count` jobs имеют terminal статус `completed` или `failed`;
- не осталось `failed` jobs с `input_valid=true` и `attempt < 3`.

Batch атомарно переходит `processing -> finalizing`, после чего:

1. `Postgres — получить jobs batch` читает исходный `input_json`, `report_fields`, `result_json`, `attempt` и `error_message` всех jobs.
2. `Собрать Daily Batch Summary из PostgreSQL` использует сохранённый Python `result_json`. Для failed jobs строит совместимый fallback со статусом `Ошибка обработки`.
3. `Сформировать CSV по типам тендеров` группирует строки по `reportId`: 1 = 223, 2 = gos, 3 = kom. Исходные CSV-колонки берутся из `input_json/reportFields`, автозаполняемые поля — из `result_json.fields` и decision/debug fallback.
4. `Создать CSV binary` создаёт UTF-8 CSV с BOM и разделителем `;`.
5. `Собрать вложения` объединяет сформированные файлы и считает completed/failed.
6. `Отправить итоговые CSV — настройте SMTP` отправляет письмо с вложениями.
7. `Postgres — batch finished` переводит batch `finalizing -> finished` и записывает `finished_at`.

Finalizer не вызывает Python API и не читает Redis/Qdrant. Для него миграция прозрачна: он продолжает читать те же таблицы и тот же формат `result_json`.

## 9. Как разбираются RAR, XLS и XLSX

### RAR

RAR4/RAR5 определяется по расширению или magic bytes `Rar!`. Сначала `lsar -json` получает список members без распаковки. До извлечения проверяются:

- path traversal и абсолютные пути;
- symbolic links;
- число файлов;
- суммарный uncompressed size;
- compression ratio.

После проверки `unar` распаковывает архив в isolated temporary directory tender. Файлы снова проверяются по фактическому пути и размеру, затем каждый проходит обычное определение типа и parser pipeline. Вложенные ZIP/7Z/RAR разрешены только до `MAX_ARCHIVE_DEPTH`.

### XLS

Старый бинарный XLS не разбирается эвристиками. LibreOffice Calc в отдельном subprocess преобразует его в XLSX с timeout `CONVERSION_TIMEOUT_SECONDS`. Затем применяется тот же XLSX parser.

### XLSX

`openpyxl` открывает workbook с `read_only=true`, `data_only=true`, `keep_links=false`. Это означает:

- строки читаются последовательно без загрузки всех листов целиком в Python objects;
- OOXML ZIP-контейнер до открытия проходит те же проверки количества файлов, распакованного размера и compression ratio;
- обрабатываются все worksheets;
- используются сохранённые вычисленные значения формул;
- внешние Excel links не загружаются;
- каждый непустой cell сохраняет адрес колонки: `A: ... | B: ... | D: ...`, поэтому пустая колонка C не сдвигает D/E;
- вывод ограничивается `MAX_TEXT_CHARS_PER_FILE`.

Полученный текст используется дважды: детерминированный parser сначала ищет строки `наименование + единица + количество`, затем LLM получает эту же таблицу вместе с найденными позициями. Если keyword quality check низкий, таблица больше не выбрасывается: сохраняется текст и warning.

Ограничение `data_only=true`: если автор XLSX никогда не пересчитывал формулы и не сохранил cached result, openpyxl увидит пустое значение такой формулы. Для старых XLS преобразование LibreOffice обычно пересчитывает workbook. Если production-файлы XLSX регулярно содержат несохранённые формулы, можно отдельно включить их предварительный прогон через LibreOffice.

## 10. Модели OpenRouter и fallback

Номер попытки job по-прежнему задаёт primary model:

```text
attempt 1 -> LLM_MODEL_ATTEMPT_1
attempt 2 -> LLM_MODEL_ATTEMPT_2
attempt 3 -> LLM_MODEL_ATTEMPT_3
```

При `LLM_ENABLE_MODEL_FALLBACK=true` каждый структурированный AI-запрос дополнительно отправляет OpenRouter ordered model fallback. Порядок ротируется вместе с attempt:

```text
attempt 1: model 1 -> model 2 -> model 3
attempt 2: model 2 -> model 3 -> model 1
attempt 3: model 3 -> model 1 -> model 2
```

OpenRouter сам переключает модель при model/provider rate limit, downtime и других routing errors. Это происходит внутри одного API-запроса и не увеличивает PostgreSQL `attempt`. Фактически использованные модели сохраняются в `debug.aiModelsUsed`, полный порядок — в `debug.aiModelChain`.

`LLM_FALLBACK_MODELS` позволяет задать отдельный comma-separated порядок. При `LLM_ENABLE_MODEL_FALLBACK=false` используется только primary model попытки.

OCR настраивается отдельно: fallback применяется только к моделям из `OCR_FALLBACK_MODELS`, потому что они должны поддерживать PDF/file input. Общие текстовые модели автоматически в OCR-цепочку не добавляются.

Если исчерпаны общие деньги/credits API key и OpenRouter отвечает 402, смена модели не поможет. Если исчерпан лимит конкретной модели/provider и доступна другая модель, fallback должен продолжить запрос.

## 11. Что где настраивается

| Компонент | Ответственность |
|---|---|
| n8n Dispatcher | CSV, нормализация входа, создание batch/jobs |
| PostgreSQL | источник истины, статусы, попытки, input/result JSON |
| n8n Controller | concurrency window, stale recovery, единственный business retry |
| FastAPI | проверка dispatching jobs, быстрая публикация в Redis |
| Redis/Celery | доставка задачи свободному worker |
| Python worker | полный тяжёлый pipeline одного tender |
| Ollama | создание embedding |
| внешний Qdrant | read-only поиск актуальных catalog candidates |
| n8n Finalizer | terminal batch, CSV, email, finished |
