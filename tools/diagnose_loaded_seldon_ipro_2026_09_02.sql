-- Диагностика 10 тендеров из daily_autofill_223_2026-09-02_corrected (1).csv,
-- у которых в CSV указан статус «Загружен Seldon».
--
-- Запрос только читает данные. Для каждого Seldon ID берётся последняя job.
-- Старые версии Worker не сохраняли сырой HTTP body IPro. Поэтому для старых
-- запусков колонка ipro_lookup содержит всё, что было сохранено в
-- result_json.debug.counterpartyLookup. После текущего изменения новые запуски
-- дополнительно сохраняют httpStatus, apiCode, apiRowsCount и candidateRows.

WITH requested(seldon_id, csv_inn) AS (
    VALUES
        ('22887777', '4105045327'),
        ('22885064', '2403002685'),
        ('22886373', '5904412180'),
        ('22888038', '7716103391'),
        ('22888105', '7711000924'),
        ('22887457', '5609088434'),
        ('22885253', '6671163413'),
        ('22887521', '6726022823'),
        ('22888263', '7706433961'),
        ('22887418', '5256133344')
),
selected AS (
    SELECT
        requested.seldon_id AS requested_seldon_id,
        requested.csv_inn,
        job.*
    FROM requested
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM tender_autofill_jobs AS candidate
        WHERE candidate.seldon_id::text = requested.seldon_id
        ORDER BY candidate.updated_at DESC, candidate.attempt DESC
        LIMIT 1
    ) AS job ON TRUE
),
latest_run AS (
    SELECT
        selected.record_key,
        run.run_id,
        run.status AS run_status,
        run.current_stage,
        run.started_at,
        run.finished_at,
        run.error_type AS run_error_type,
        run.error_message AS run_error_message
    FROM selected
    LEFT JOIN LATERAL (
        SELECT candidate.*
        FROM tender_autofill_job_runs AS candidate
        WHERE candidate.record_key = selected.record_key
        ORDER BY candidate.attempt DESC, candidate.started_at DESC
        LIMIT 1
    ) AS run ON TRUE
),
ipro_events AS (
    SELECT
        latest_run.record_key,
        jsonb_agg(
            jsonb_build_object(
                'eventTime', event.event_time,
                'eventType', event.event_type,
                'stage', event.stage,
                'status', event.status,
                'service', event.service,
                'operation', event.operation,
                'httpStatus', event.http_status,
                'resultCount', event.result_count,
                'errorType', event.error_type,
                'errorMessage', event.error_message,
                'details', event.details
            )
            ORDER BY event.event_time, event.event_id
        ) AS events
    FROM latest_run
    JOIN tender_autofill_job_events AS event
      ON event.run_id = latest_run.run_id
    WHERE COALESCE(event.stage, '') ILIKE '%ipro%'
       OR COALESCE(event.service, '') ILIKE '%ipro%'
       OR COALESCE(event.operation, '') ILIKE '%ipro%'
    GROUP BY latest_run.record_key
)
SELECT
    selected.requested_seldon_id AS seldon_id,
    selected.csv_inn,
    selected.record_key,
    selected.batch_id,
    selected.report_id,
    selected.status AS job_status,
    selected.attempt,
    selected.updated_at,
    selected.error_message AS job_error_message,

    COALESCE(
        selected.result_json #>> '{fields,tenderStatus}',
        selected.result_json #>> '{reportFields,Статус тендера}',
        selected.report_fields ->> 'Статус тендера'
    ) AS final_tender_status,
    COALESCE(
        selected.result_json #>> '{fields,tenderStatusReason}',
        selected.result_json #>> '{reportFields,Причина статуса}',
        selected.report_fields ->> 'Причина статуса'
    ) AS final_reason,
    COALESCE(
        selected.result_json #>> '{fields,tenderStatusNote}',
        selected.result_json #>> '{reportFields,Примечание к статусу}',
        selected.report_fields ->> 'Примечание к статусу'
    ) AS final_note,

    selected.result_json #>> '{decision,status}' AS decision_status,
    selected.result_json #>> '{decision,reason}' AS decision_reason,
    selected.result_json #>> '{decision,reasonOrigin}' AS reason_origin,
    selected.result_json #>> '{decision,counterpartyRequiresWork}'
        AS counterparty_requires_work,
    selected.result_json #>> '{decision,manualReviewRequired}'
        AS manual_review_required,

    selected.result_json #> '{debug,counterpartyLookup}' AS ipro_lookup,
    selected.result_json #>> '{debug,counterpartyLookup,status}' AS ipro_status,
    selected.result_json #>> '{debug,counterpartyLookup,reason}' AS ipro_reason,
    COALESCE(
        selected.result_json #>> '{debug,counterpartyLookup,requestedInn}',
        selected.result_json #>> '{debug,counterpartyLookup,inn}'
    ) AS ipro_requested_inn,
    selected.result_json #>> '{debug,counterpartyLookup,matchType}'
        AS ipro_match_type,
    selected.result_json #>> '{debug,counterpartyLookup,httpStatus}'
        AS ipro_http_status,
    selected.result_json #>> '{debug,counterpartyLookup,apiCode}'
        AS ipro_api_code,
    selected.result_json #>> '{debug,counterpartyLookup,apiRowsCount}'
        AS ipro_rows_count,
    selected.result_json #>> '{debug,counterpartyLookup,byInnCount}'
        AS ipro_rows_with_requested_inn,
    selected.result_json #>> '{debug,counterpartyLookup,fullNameOrg}'
        AS ipro_matched_name,
    selected.result_json #> '{debug,counterpartyLookup,candidateRows}'
        AS ipro_candidate_rows,

    selected.result_json #>> '{fields,counterpartyInn}' AS worker_inn,
    selected.result_json #>> '{fields,counterpartyKpp}' AS worker_kpp,
    selected.result_json #>> '{fields,counterpartyName}' AS worker_counterparty_name,
    selected.report_fields ->> 'ИНН контрагента' AS report_inn,
    selected.report_fields ->> 'Код контрагента' AS report_counterparty_code,
    selected.report_fields ->> 'Название контрагента' AS report_counterparty_name,

    selected.result_json #>> '{productCheck,validation,requiresManualReview}'
        AS product_validation_manual_review,
    selected.result_json #> '{productCheck,validation,unresolved}'
        AS product_validation_unresolved,
    selected.result_json #>> '{productCheck,total}' AS product_total,
    selected.result_json #>> '{productCheck,coveragePercent}' AS coverage_percent,

    latest_run.run_id,
    latest_run.run_status,
    latest_run.current_stage,
    latest_run.started_at,
    latest_run.finished_at,
    latest_run.run_error_type,
    latest_run.run_error_message,
    COALESCE(ipro_events.events, '[]'::jsonb) AS ipro_timeline_events
FROM selected
LEFT JOIN latest_run USING (record_key)
LEFT JOIN ipro_events USING (record_key)
ORDER BY selected.requested_seldon_id;

-- Короткая выборка результатов IPro, требующих ручной проверки.
-- Для тендера без подтверждённых причин отказа ожидаем:
-- final_tender_status='Загружен Seldon' при
-- ipro_status IN ('not_found', 'lookup_error').
WITH requested(seldon_id) AS (
    VALUES
        ('22887777'), ('22885064'), ('22886373'), ('22888038'), ('22888105'),
        ('22887457'), ('22885253'), ('22887521'), ('22888263'), ('22887418')
),
latest AS (
    SELECT job.*
    FROM requested
    JOIN LATERAL (
        SELECT candidate.*
        FROM tender_autofill_jobs AS candidate
        WHERE candidate.seldon_id::text = requested.seldon_id
        ORDER BY candidate.updated_at DESC, candidate.attempt DESC
        LIMIT 1
    ) AS job ON TRUE
)
SELECT
    seldon_id,
    result_json #>> '{fields,tenderStatus}' AS final_tender_status,
    result_json #>> '{debug,counterpartyLookup,status}' AS ipro_status,
    result_json #>> '{debug,counterpartyLookup,reason}' AS ipro_reason,
    result_json #>> '{decision,reasonOrigin}' AS reason_origin,
    result_json #>> '{productCheck,validation,requiresManualReview}'
        AS product_validation_manual_review
FROM latest
WHERE result_json #>> '{debug,counterpartyLookup,status}' IS DISTINCT FROM 'matched'
ORDER BY seldon_id;
