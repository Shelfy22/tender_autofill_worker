# Анализ миграции Tender Autofill Worker

Дата анализа: 2026-08-07.

Источник истины — три экспортированных workflow из `reference/`:

- `Daily Tender Dispatcher.json` — 10 нод;
- `Daily Tender Controller + Finalizer.json` — 33 ноды;
- `Tender Autofill Worker (1).json` — 121 нода.

В анализ включены параметры нод, JavaScript Code nodes, SQL, связи, настройки моделей и альтернативные ветви. Секреты, присутствующие в экспорте, здесь не повторяются. Их следует отозвать/заменить и вынести в environment variables.

## 1. Текущая архитектура

```text
CSV form или Seldon workflow
  -> Daily Tender Dispatcher
     -> tender_autofill_batches
     -> tender_autofill_jobs(status=queued|failed для невалидного input)

Schedule/Manual trigger
  -> Daily Tender Controller
     -> requeue stale dispatching/processing
     -> requeue failed с attempt < 3
     -> атомарно claim свободных jobs: queued -> dispatching
     -> Execute Sub-workflow (each, wait=false)
        -> Tender Autofill Worker
           -> атомарно dispatching -> processing, attempt=attempt+1
           -> Seldon/документы/LLM/каталог/решение
           -> processing -> completed, result_json=...

Controller/Finalizer
  -> claim terminal batch: processing -> finalizing
  -> читает jobs и result_json
  -> формирует 223/gos/kom CSV
  -> email
  -> batch -> finished
```

Dispatcher не запускает Worker. Ограничение активных execution реализовано Controller через PostgreSQL и advisory lock.

## 2. Dispatcher

### 2.1 Входы

Активный путь начинается с n8n Form Trigger. Поддерживаются до трёх CSV:

- `file223`;
- `fileGos`;
- `fileKom`;
- `testLimit` — ограничение строк каждого файла.

Есть отключённый Execute Workflow Trigger для входа из Seldon workflow.

CSV читается как UTF-8, затем Windows-1251. Разделитель выбирается из `;`, tab, `,`. Тип отчёта определяется по имени файла, заголовкам и `Код ФЗ`. Дубликат одного типа файла считается ошибкой.

### 2.2 Нормализация строки

Для каждой строки сохраняются исходные колонки в `reportFields`. Вводятся совместимые aliases:

- `Название заказчика -> Организатор`, если Организатор пуст;
- `Торговая площадка -> Электронная площадка`, если она пуста;
- `ИНН заказчика -> ИНН заказчика/организатора`, если он пуст.

Нормализуются:

- `reportId`: 1 = 223, 2 = gos (44/94), 3 = kom;
- `purchaseType`;
- `seldonId` или, при его отсутствии, `etpId`;
- `lawCode` (`Код ФЗ`);
- `toCode` (`Код ТО`);
- `reportFields`;
- метаданные файла и строки.

Валидная строка должна иметь `reportId` и ровно один идентификатор: `seldonId` или `etpId`.

### 2.3 Ключи и payload

Типичный `record_key`:

```text
daily:<batchId>:<reportId>:<seldonId>
daily:<batchId>:<reportId>:etp:<etpId>
daily:<batchId>:invalid:<rowNumber>
```

`input_json` содержит исходную нормализованную строку плюс:

- `batchId`, `batchDate`, `rowNumber`;
- `jobRecordKey`;
- `asyncDispatch=true`;
- `batchMode=true`;
- `batchCacheKey` и `batchResultKey`, равные `record_key`.

## 3. PostgreSQL schema

### 3.1 `tender_autofill_batches`

| Колонка | Тип/назначение |
|---|---|
| `batch_id` | TEXT PRIMARY KEY |
| `batch_date` | DATE |
| `expected_count` | всего строк, включая невалидные |
| `valid_count` / `invalid_count` | валидация Dispatcher |
| `completed_count` / `failed_count` | агрегаты jobs |
| `dispatched_count` | накопительный счётчик dispatch |
| `status` | `processing`, `finalizing`, `finished` (DDL default `created`) |
| `source_files` | JSONB с описанием CSV |
| timestamps | `created_at`, `dispatched_at`, `finished_at`, `updated_at` |

### 3.2 `tender_autofill_jobs`

| Колонка | Тип/назначение |
|---|---|
| `record_key` | TEXT PRIMARY KEY |
| `batch_id` | логическая связь с batch; явный FK отсутствует |
| `report_id`, `report_name` | тип исходного CSV |
| `seldon_id`, `etp_id`, `row_number` | идентификаторы/порядок |
| `status` | `queued`, `dispatching`, `processing`, `completed`, `failed` |
| `attempt` | увеличивается при переходе в processing |
| `input_valid` | валидность Dispatcher |
| `input_json` | полный вход Worker |
| `report_fields` | исходные CSV поля |
| `result_json` | итог Worker, читаемый Finalizer |
| `error_message` | последняя ошибка/причина requeue |
| `worker_execution_id` | n8n execution id |
| timestamps | `created_at`, `started_at`, `finished_at`, `updated_at` |

Индексы: batch, status, `(batch_id,status)`, report и `(status,created_at,row_number)`.

## 4. Жизненный цикл job и retry

### 4.1 Обычный путь

1. Dispatcher создаёт валидный job как `queued`, `attempt=0`.
2. Controller под advisory lock считает `dispatching + processing` для processing batches.
3. До конфигурированного окна (в SQL сейчас параметр `5`, хотя note говорит 10) jobs атомарно переводятся `queued -> dispatching` через `FOR UPDATE SKIP LOCKED`.
4. Worker первым SQL делает `dispatching -> processing`, увеличивает `attempt`, записывает `started_at` и n8n execution id.
5. При успехе Worker делает `processing -> completed`, пишет `result_json`, обновляет `report_fields` и batch counters.

### 4.2 Существующий retry

Единственный владелец retry должен остаться n8n Controller:

- `dispatching` старше 10 минут возвращается в `queued`;
- `processing` старше 30 минут возвращается в `queued`;
- `failed` с `input_valid=true` и `attempt < 3` возвращается в `queued`;
- batch из `finalizing`, зависший более 30 минут, возвращается в `processing`.

Python/Celery не должен делать business retry. Он должен завершать пойманную ошибку статусом `failed`; Controller решит, можно ли повторять. OOM/SIGKILL/hard timeout, при котором Python не успел записать `failed`, восстанавливается stale-processing SQL Controller.

### 4.3 Найденная неоднозначность retry

В экспортированном Worker нет Error Trigger/SQL, переводящего job в `failed`, и не задан `errorWorkflow`. Обычное исключение оставляет job `processing`, после чего срабатывает stale requeue. SQL retry для `failed` полезен только если статус устанавливает внешняя автоматизация, отсутствующая в трёх JSON, либо ручной процесс.

Также Controller передаёт только `input_json`, не присоединяя колонку `attempt`. Поэтому `workerAttempt` в текущем Worker обычно defaults to 1, несмотря на model selectors по попыткам.

## 5. Активный путь Tender Autofill Worker

Ниже — последовательность от `Execute Sub-workflow Trigger`. Отключённые webhook/form ветви указаны отдельно.

1. `Postgres — job processing`: claim job и `attempt + 1`.
2. `Восстановить input после job processing`: прекращает дублирующий execution, если claim не состоялся.
3. `Normalize Input2`: объединяет body/query/input, определяет `reportId`, идентификатор, batch context, `reportFields`, `Код ТО`, `Код ФЗ`, `Осталось дней`, строит/принимает полный `seldonPurchase`.
4. `Load Seldon Token1` -> `IF Seldon Token Valid1` -> при необходимости Seldon Login -> сохранение токена.
5. `Use Input Seldon Purchase1`: повторный `/Purchases/Get` не выполняется; используется `seldonPurchase` из Dispatcher/Seldon input.
6. `Seldon Get Purchase Documents1`: POST `/PurchasesDocuments/Get` по `reportId+seldonId` или `reportId+etpId`.
7. `Build Seldon Tender Context1`: выбирает purchase, фильтрует документы `Version=0`, предпочитает `urlSeldon`, строит structured `pageText` и сохраняет служебные поля.
8. Если документы есть: разворот ссылок -> streaming download в n8n binary -> определение типа по имени/MIME/magic -> parser route.
9. Парсинг:
   - DOC/DOCX: Word extractor; для DOCX из архива fallback через `word/document.xml`;
   - PDF: PDF parser; текст <500 символов идёт в OCR через OpenRouter file-parser (`mistral-ocr`);
   - XLS/XLSX/CSV: Extract From File -> строки в текст;
   - ZIP: compression node;
   - RAR/7Z: archive nodes;
   - вложенные архивы не распаковываются рекурсивно;
   - неподдерживаемые файлы сохраняются как warning.
10. `Collect Parsed Documents2` и восстановление batch-context.
11. `Prepare Combined Text for LLM2`: `pageText + documents`, максимум 500000 символов.
12. `AI Agent - Extract Tender Fields2`: structured JSON extraction.
13. Восстановление контекста после Agent.
14. `Validate Fields2`: parse JSON, aliases, fallbacks, нормализация дат/денег/ИНН/КПП, ГОЗ, special account, security, delivery, national regime, `Код ТО`, warnings/debug.
15. `Extract Actual Customer Candidates`: кандидаты из договора/ТЗ/извещения/Seldon, приоритет грузополучателя/филиала/заказчика.
16. `AI Agent - Resolve Actual Customer` -> `Apply Actual Customer Resolution`.
17. `Получить организацию по ИНН` (IPro `/orgByBir`) -> `Parse IPro Counterparty Response`.
18. Подготовка product extraction -> детерминированный Excel extraction -> LLM product extraction -> parse/merge до 100 позиций.
19. Для каждой позиции должна выполняться catalog matching, затем `Parse Product Match Result`.
20. `Summarize Product Coverage`: supplied/coverage/prices/quantity/threshold.
21. `Prepare Tender Decision Prompt2`: детерминированные hard reasons + LLM context.
22. `AI Agent - Decide Tender Status` -> `Apply Tender Decision`.
23. HTML report/callback ветка; для batch mode — `Return Batch Cache Result`.
24. `Postgres — job completed` -> возврат async result.

### Альтернативные ветви

- `Webhook - Start Autofill2` отключён, но ведёт в Seldon API путь.
- `Webhook - Start Autofill3` отключён; эта ветка может получать uploaded documents и отдельно загружать HTML страницы тендера.
- Form upload/archive ветка не связана с production sub-workflow entry.
- Scheduled Seldon token refresh — отдельный вход.

Важно: в активном production path реальная HTML-страница по `tenderUrl` не скачивается. `pageText` — структурированный Seldon JSON. HTML fetching находится в альтернативной webhook ветке.

## 6. Вход Worker

Worker принимает один объект `input_json`. Поля, реально используемые активным путём:

```json
{
  "batchId": "form-...",
  "batchDate": "YYYY-MM-DD",
  "rowNumber": 1,
  "jobRecordKey": "daily:...",
  "batchMode": true,
  "batchCacheKey": "daily:...",
  "batchResultKey": "daily:...",
  "reportId": 1,
  "purchaseType": "223-ФЗ",
  "seldonId": "123",
  "etpId": null,
  "toCode": "...",
  "lawCode": "223",
  "reportFields": {},
  "seldonPurchase": {},
  "remainingDays": 3,
  "sourceFile": "form-upload://...",
  "asyncDispatch": true
}
```

Поддерживаются aliases и плоские русские колонки. `seldonPurchase` предпочтителен; при его отсутствии Worker строит минимальный purchase из строки Daily.

## 7. Формат `result_json`

Finalizer ожидает объект, а не envelope API:

```json
{
  "fields": {},
  "meta": {},
  "productCheck": null,
  "decision": null,
  "warnings": [],
  "logs": [],
  "debug": null,
  "reportId": 1,
  "seldonId": "123",
  "etpId": null,
  "purchaseType": "223-ФЗ",
  "purchaseNumber": null,
  "tenderUrl": null,
  "batchId": "...",
  "batchDate": "...",
  "rowNumber": 1,
  "jobRecordKey": "...",
  "remainingDays": 3,
  "toCode": "...",
  "lawCode": "223",
  "sectionName": null,
  "filterName": null,
  "reportFields": {},
  "sourceTender": {},
  "processedAt": "ISO-8601"
}
```

`fields` содержит все поля автозаполнения, включая aliases `counterparty/inn/kpp`; `legalEntity` всегда `null`; `toCode` добавляется в `fields` программно.

Finalizer читает следующие ключи особенно явно:

- `fields.*` для всех autofill CSV columns;
- `decision`, `decisionResult` или `debug.tenderDecision`;
- `reportId`, `seldonId`, `toCode`, `lawCode`;
- `reportFields`, `sourceTender.reportFields`, `sourceReportFields`;
- warnings/debug не определяют CSV напрямую, но используются для диагностики и fallback decision.

## 8. Бизнес-правила, которые нельзя потерять

### Товары и стоимость

- `fullMatch` требует `Соответствие == "Полное соответствие"` и catalog evidence (артикул или ссылка).
- `analogMatch` требует `Соответствие == "Аналог"` и catalog evidence.
- `analogAccepted = analogMatch && analogsAllowed !== false`.
- `supplied = fullMatch || analogAccepted`.
- coverage = количество supplied / количество извлечённых позиций.
- согласование ассортимента возможно только при `coveragePercent > 50`; ровно 50% — reject.
- цена позиции = median unit price × positive quantity.
- итог = сумма рассчитанных supplied positions.
- price evaluation complete только когда цена и quantity есть для каждой supplied position.
- порог расчётной стоимости применим только при coverage >50 и complete price evaluation.
- итог < 1 000 000 RUB — hard reject.
- отдельно исходная `initialPrice`, если она реально задана и >0, также вызывает hard reject при <1 000 000.
- `initialPrice` 0/пусто/null не является основанием для reject.

### Решение и статусы

- непоставляемый ассортимент — только детерминированная причина при coverage <=50;
- правило неделимого лота по порогу 80% отключено;
- менее 3 рабочих дней определяется только колонкой `Осталось дней`; условие строго `<3`, значение 3 проходит;
- если IPro lookup не `matched`, итоговый статус имеет приоритет `Проработка контрагента`, причина `Прочее`;
- затем по приоритету hard reasons -> `Отказано КУ ЦП`;
- затем подтверждённый LLM reject -> `Отказано КУ ЦП`;
- LLM approve без hard reasons -> `Согласовано КУ ЦП`;
- невалидный/пустой LLM decision без hard reasons -> `Загружен Seldon` / `Прочее`.

Детерминированные hard reasons включают: duplicate, сроки, payment-after-contractor, чрезмерную отсрочку, удалённые территории (Якутия/Саха, Калининград), consignment/storage, 35+ kV, frequency drive 6–10 kV, spare kit/ZIP/drawing-made goods, military acceptance, atomic acceptance, supply with works, remaining days, self-submission by MOPP и другие оргкритерии из справочника ноды.

### Служебные поля

- `Код ТО` приходит из Seldon filters/Daily и не должен вычисляться LLM или по товарам.
- `Осталось дней` также служебное поле Daily/Seldon.
- `reportFields` должны сохранять оригинальные CSV columns.
- `legalEntity` всегда null.
- counterparty должен разрешаться по фактическому получателю/филиалу, не по поставщику/банку/УФК.

## 9. Модели и попытки

В пяти model selectors одинаковая логика: attempt `1/2/3` выбирает input 1/2/3. Но подключённые модели в JSON:

| attempt | Фактическая модель в JSON |
|---|---|
| 1 | `google/gemini-3.5-flash` |
| 2 | `google/gemini-2.5-pro` |
| 3 | `openai/gpt-5.5` |

Это не `GPT-5 -> GPT-5-mini -> GPT-4.1`. Кроме того, attempt сейчас не добавляется Controller в payload, поэтому селектор обычно видит 1. В ходе миграции пользователь отдельно подтвердил прямой OpenAI API и mapping `gpt-5 -> gpt-5-mini -> gpt-4.1`; Python получает истинную попытку из PostgreSQL.

## 10. Внешние зависимости

- PostgreSQL — batches/jobs и единственный владелец retry.
- Seldon API — login, `/PurchasesDocuments/Get`; purchase приходит во входе.
- В исходном JSON: OpenRouter/OpenAI-compatible API. В Python-миграции: прямой OpenAI API для четырёх LLM стадий и OCR PDF.
- IPro ETM — lookup организации по ИНН.
- Qdrant collection `products`, topK=50.
- Ollama embeddings model `qwen3-embedder-ft:latest`.
- Sub-workflow ETM поиска товара по артикулу.
- SMTP Finalizer.
- HTTP document sources; callback URL для non-batch ветки.

## 11. Node -> Python mapping

| n8n стадия | Python module/function |
|---|---|
| Execute trigger / PG processing | `app.tasks.process_tender`, `Repository.claim_for_processing` |
| Normalize Input2 | `app.services.normalization.normalize_job_payload` |
| Seldon token/login/documents | `app.services.seldon.SeldonClient` |
| Build Seldon context | `app.services.seldon.build_tender_context` |
| Download Document | `app.services.documents.DocumentDownloader` |
| Type detection / switches | `app.services.parsers.detect_file_type` |
| ZIP/7Z/RAR guard | `app.services.parsers.archives` |
| PDF/OCR | `app.services.parsers.pdf`, `app.services.llm.ocr_pdf` |
| DOC/DOCX/LibreOffice | `app.services.parsers.word` |
| XLS/XLSX/CSV | `app.services.parsers.spreadsheets` |
| Collect/combined text | `app.services.documents.collect_documents/build_combined_text` |
| Tender field Agent/Validate | `app.services.llm.extract_fields`, `app.services.validation` |
| Actual customer | `app.services.customer` |
| IPro | `app.services.catalog.IProClient` |
| Product extraction | `app.services.products.extract_positions` |
| Qdrant/Ollama/article lookup | `app.services.catalog.CatalogMatcher` |
| Product coverage | `app.services.coverage.summarize_product_coverage` |
| Decision prompt/rules | `app.services.decision` |
| Return Batch Cache Result | `app.services.result.build_result_json` |
| PG completed/failed | `Repository.complete_job/fail_job` |

## 12. Спорные и непонятные места

1. Qdrant node и article-search tool присутствуют, но в exported connections не подключены как tools к product matching Agent. Отдельного product matching Agent нет. `Loop Tender Products` идёт прямо в `Parse Product Match Result`, поэтому точный runtime контракт каталога восстановить из этих трёх JSON нельзя.
2. URL/credentials Qdrant и Ollama находятся в n8n credentials, не в export.
3. Контракт sub-workflow поиска по артикулу неизвестен: есть только input `query`, output schema отсутствует.
4. Текущий production entry не скачивает HTML tender page; это делает другая отключённая webhook ветка.
5. `failed` не устанавливается Worker в export; внешний Error workflow не указан.
6. Note/тексты говорят максимум 10, активный Controller SQL вызывается с 5. Dispatcher response также противоречив: `maxTenderActive=5`, а `nextStep` говорит 10.
7. Расхождение исходных OpenRouter models с желаемыми GPT attempts разрешено отдельным решением пользователя: Python использует прямой OpenAI API и модели `gpt-5`, `gpt-5-mini`, `gpt-4.1`.
8. DDL не содержит FK `jobs.batch_id -> batches.batch_id` и CHECK constraints; добавлять их без отдельной миграции нельзя.
9. Seldon login credentials оказались встроены в JSON HTTP node. Их необходимо отозвать; Python не должен копировать их.

Реализация ниже сохраняет доказанные контракты и deterministic rules. Для catalog matching предусмотрен адаптер и явная ошибка конфигурации, а не выдуманный формат Qdrant payload.
