# Развёртывание в `/opt/stack`

Сервис не запускает собственные PostgreSQL, Qdrant, Ollama, Grafana или
Prometheus. Из инфраструктуры этого Compose создаются только `tender-api`,
отдельный Redis для Celery и масштабируемые `tender-worker`.

## 1. Клонирование

```bash
cd /opt/stack
git clone https://github.com/Shelfy22/tender_autofill_worker.git tender_autofill_worker
cd /opt/stack/tender_autofill_worker
cp .env.example .env
chmod 600 .env
nano .env
```

Если каталог уже клонирован:

```bash
cd /opt/stack/tender_autofill_worker
git pull --ff-only
```

## 2. Общая Docker network

Создать один раз:

```bash
docker network inspect ai-net >/dev/null 2>&1 || \
  docker network create ai-net
```

В `.env` оставить:

```dotenv
TENDER_EXTERNAL_NETWORK=ai-net
```

Посмотреть реальные имена контейнеров:

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

Подключить уже работающие контейнеры. Имена `qdrant` и `ollama` заменить на
фактические, если они отличаются:

```bash
docker network connect ai-net tenders-tender-postgres-1
docker network connect ai-net qdrant
docker network connect ai-net ollama
```

Повторный `network connect` выдаёт `endpoint already exists` — это безопасно и
означает, что контейнер уже подключён.

Ручное подключение теряется при пересоздании внешнего контейнера. Для постоянной
конфигурации добавьте эту же external network в Compose проектов PostgreSQL,
Qdrant и Ollama:

```yaml
services:
  service-name:
    networks:
      - default
      - ai-net

networks:
  ai-net:
    external: true
    name: ai-net
```

## 3. Основные значения `.env`

```dotenv
API_KEY=<случайная строка из openssl rand -hex 32>
POSTGRES_DSN=postgresql://tenders:<URL_ENCODED_PASSWORD>@tenders-tender-postgres-1:5432/tenders

SELDON_USERNAME=...
SELDON_PASSWORD=...

LLM_API_KEY=...

CATALOG_MODE=qdrant
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=...
QDRANT_COLLECTION=products
QDRANT_VECTOR_NAME=
QDRANT_TOP_K=50

OLLAMA_URL=http://ollama:11434
OLLAMA_EMBEDDING_MODEL=qwen3-embedder-ft:latest

IPRO_TOKEN=
REDIS_URL=redis://tender-redis:6379/0
WORKER_MEMORY_LIMIT=4g
```

Пароль PostgreSQL в DSN должен быть URL-encoded. `QDRANT_URL` и `OLLAMA_URL`
содержат только базовый URL. Путь `/collections/products` и `/api/embed` код
добавляет самостоятельно.

## 4. Проверка внешних сервисов

После создания network проверить разрешение имён:

```bash
docker run --rm --network ai-net curlimages/curl:8.10.1 \
  --fail --silent --show-error http://ollama:11434/api/tags
```

Qdrant без API key:

```bash
docker run --rm --network ai-net curlimages/curl:8.10.1 \
  --fail --silent --show-error http://qdrant:6333/collections/products
```

Если Qdrant защищён ключом, не помещайте ключ непосредственно в shell history.
Достаточно проверить его после запуска через обработку тестового tender либо
использовать временную shell variable с выключенным history.

Проверка PostgreSQL через код приложения:

```bash
docker compose run --rm --no-deps tender-api \
  python -c "from app.db import get_repository; print(get_repository().ping())"
```

Ожидается `True`.

## 5. Миграция observability

Сначала сделать backup базы. Миграция не меняет существующие
`tender_autofill_jobs` и `tender_autofill_batches`, а добавляет audit-таблицы:

```bash
docker compose build tender-api tender-worker
docker compose run --rm --no-deps tender-api python -m app.migrate
```

Проверка существующих job-таблиц и новых audit-таблиц:

```bash
docker compose run --rm --no-deps tender-api python -c \
  "import psycopg; from app.config import get_settings; s=get_settings(); c=psycopg.connect(s.postgres_dsn.get_secret_value()); q=c.cursor(); q.execute(\"SELECT to_regclass('public.tender_autofill_jobs'), to_regclass('public.tender_autofill_batches'), to_regclass('public.tender_autofill_job_runs')\"); print(q.fetchone())"
```

Все три значения должны быть не `None`.

## 6. Запуск пяти параллельных workers

```bash
docker compose up -d --build --scale tender-worker=5
docker compose ps
```

Каждый из пяти контейнеров имеет `concurrency=1`. В каждый момент он обрабатывает
один tender, а Celery child уничтожается после одной задачи. При лимите `4g`
теоретический общий предел пяти worker-контейнеров — до 20 GiB плюс API, Redis и
остальные сервисы. Перед запуском проверьте доступную RAM:

```bash
free -h
docker stats --no-stream
```

## 7. Проверка запуска

```bash
curl --fail --silent http://127.0.0.1:8000/health
curl --fail --silent http://127.0.0.1:8000/metrics | head
docker compose logs --tail=200 tender-api tender-worker
docker compose exec tender-api \
  celery -A app.celery_app:celery_app inspect ping --timeout=10
```

`/health` должен вернуть `postgres: true`, `redis: true`, а Celery inspect — пять
ответов `pong`.

## 8. Подключение n8n

Самый простой вариант — обращаться по опубликованному порту сервера:

```dotenv
TENDER_PYTHON_URL=http://<IP_СЕРВЕРА>:8000
TENDER_PYTHON_API_KEY=<то же значение, что API_KEY Python-сервиса>
TENDER_MAX_ACTIVE=5
```

Если n8n подключён к `ai-net`, используйте внутренний адрес без
публикации наружу:

```dotenv
TENDER_PYTHON_URL=http://tender-api:8000
```

Карта замены ноды Controller и точное тело HTTP-запроса находятся в
`N8N_INTEGRATION.md`.

Не переключайте все jobs сразу. Сначала отправьте один тестовый tender, проверьте
его timeline/result JSON и Finalizer, затем установите Controller concurrency `5`.

## 9. Обновление и масштабирование

```bash
cd /opt/stack/tender_autofill_worker
git pull --ff-only
docker compose up -d --build --scale tender-worker=5
```

После проверки масштабирование до десяти:

```bash
docker compose up -d --scale tender-worker=10
```

Одновременно поменяйте `TENDER_MAX_ACTIVE` Controller на `10`.
