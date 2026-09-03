# Tender Product Matching From Documents

Import `n8n/Tender Product Matching From Documents.json` into n8n.

Required n8n environment variables:

```env
TENDER_PYTHON_API_KEY=<same value as worker API_KEY>
TENDER_PRODUCT_MATCHING_URL=http://tender-api:8000/product-matching/from-documents
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
```

Usage:

1. Open the n8n form trigger.
2. Upload tender documents or archives.
3. Run the workflow.
4. The Code node returns a binary XLSX file in the `data` field.

The API endpoint parses uploaded files with the same document parser as `tender_autofill_worker`, runs one LLM analysis per document unit, consolidates extracted products, searches the catalog/vector database for each final product, and returns an XLSX product matching report.