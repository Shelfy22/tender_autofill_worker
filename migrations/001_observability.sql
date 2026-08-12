-- Exact per-attempt audit for the full Python Tender Autofill Worker.
-- Apply to the same PostgreSQL used by n8n. This migration does not alter
-- tender_autofill_batches or tender_autofill_jobs.

CREATE TABLE IF NOT EXISTS tender_autofill_job_runs (
    run_id TEXT PRIMARY KEY,
    record_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    seldon_id TEXT,
    attempt INTEGER NOT NULL,
    worker_name TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    current_stage TEXT,
    stage_started_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    duration_seconds DOUBLE PRECISION,
    peak_memory_rss_mb DOUBLE PRECISION NOT NULL DEFAULT 0,

    llm_requests INTEGER NOT NULL DEFAULT 0,
    llm_successes INTEGER NOT NULL DEFAULT 0,
    llm_failures INTEGER NOT NULL DEFAULT 0,
    llm_prompt_tokens BIGINT NOT NULL DEFAULT 0,
    llm_completion_tokens BIGINT NOT NULL DEFAULT 0,
    llm_total_tokens BIGINT NOT NULL DEFAULT 0,
    llm_fallbacks INTEGER NOT NULL DEFAULT 0,

    embedding_queries INTEGER NOT NULL DEFAULT 0,
    embedding_http_requests INTEGER NOT NULL DEFAULT 0,
    qdrant_queries INTEGER NOT NULL DEFAULT 0,
    qdrant_http_requests INTEGER NOT NULL DEFAULT 0,
    qdrant_results BIGINT NOT NULL DEFAULT 0,

    documents_requested INTEGER NOT NULL DEFAULT 0,
    documents_parsed INTEGER NOT NULL DEFAULT 0,
    download_bytes BIGINT NOT NULL DEFAULT 0,
    warnings_count INTEGER NOT NULL DEFAULT 0,

    error_type TEXT,
    error_message TEXT,
    result_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tender_job_runs_record_attempt
    ON tender_autofill_job_runs (record_key, attempt DESC);
CREATE INDEX IF NOT EXISTS idx_tender_job_runs_batch_started
    ON tender_autofill_job_runs (batch_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tender_job_runs_status_heartbeat
    ON tender_autofill_job_runs (status, heartbeat_at DESC);
CREATE INDEX IF NOT EXISTS idx_tender_job_runs_started
    ON tender_autofill_job_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS tender_autofill_job_events (
    event_id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES tender_autofill_job_runs(run_id) ON DELETE CASCADE,
    record_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL,
    stage TEXT,
    status TEXT NOT NULL,
    service TEXT,
    operation TEXT,
    model TEXT,
    primary_model TEXT,
    provider_request_id TEXT,
    http_method TEXT,
    http_status INTEGER,
    duration_seconds DOUBLE PRECISION,
    memory_rss_mb DOUBLE PRECISION,
    prompt_tokens BIGINT,
    completion_tokens BIGINT,
    total_tokens BIGINT,
    result_count INTEGER,
    byte_count BIGINT,
    error_type TEXT,
    error_message TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tender_job_events_run_time
    ON tender_autofill_job_events (run_id, event_time, event_id);
CREATE INDEX IF NOT EXISTS idx_tender_job_events_service_time
    ON tender_autofill_job_events (service, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_tender_job_events_type_time
    ON tender_autofill_job_events (event_type, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_tender_job_events_record_time
    ON tender_autofill_job_events (record_key, event_time DESC);

-- Small all-time counter table used by /metrics. It avoids scanning the
-- ever-growing event timeline every 15 seconds.
CREATE TABLE IF NOT EXISTS tender_autofill_metric_counters (
    metric_name TEXT NOT NULL,
    label_model TEXT NOT NULL DEFAULT '',
    label_status TEXT NOT NULL DEFAULT '',
    value BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (metric_name, label_model, label_status)
);

COMMENT ON TABLE tender_autofill_job_runs IS
    'One durable execution record per Celery attempt of the full Python Tender Worker.';
COMMENT ON TABLE tender_autofill_job_events IS
    'Ordered stage/external-call timeline; no prompts, document text, credentials or binary data.';
COMMENT ON TABLE tender_autofill_metric_counters IS
    'Small exact all-time counters for Prometheus scrape without full event scans.';
