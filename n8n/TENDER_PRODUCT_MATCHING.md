# Tender Product Matching

Import `n8n/Tender Product Matching Async.json` into n8n.

Required n8n environment variables:

```env
TENDER_PYTHON_API_KEY=<same value as worker API_KEY>
TENDER_PRODUCT_MATCHING_URL=http://tender-api:8000/product-matching/jobs
```

`TENDER_PRODUCT_MATCHING_URL` is optional if n8n can reach the API at the default Docker service URL above.

Worker/API environment checklist:

```env
ENABLE_DOCUMENT_ANALYSIS_PIPELINE=true
CATALOG_MODE=qdrant
QDRANT_URL=<qdrant url>
OLLAMA_URL=<ollama url>
LLM_API_KEY=<openrouter key>
LLM_REASONING_EFFORT=none
YANDEX_DISK_TOKEN=<OAuth token for report upload>
```

Usage:

1. Open the n8n form trigger.
2. Upload tender documents or archives.
3. Run the workflow. The chat response returns a `jobId` immediately.
4. The completed XLSX is available from `GET /product-matching/jobs/{jobId}/download`
   and, when configured, on Yandex Disk.

The API endpoint accepts documents, stores them in a volume shared with the Celery worker,
and returns without waiting for LLM/Qdrant processing. Query
`GET /product-matching/jobs/{jobId}` for `queued`, `processing`, `completed`, or `failed`.
The worker uses the same parser, LLM analysis, and catalog matching as the synchronous
endpoint, then uploads the XLSX to Yandex Disk when `YANDEX_DISK_TOKEN` is configured.
