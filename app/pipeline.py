from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.config import Settings
from app.logging import stage
from app.models import JobClaim, TenderResult
from app.observability import RunObserver
from app.services.catalog import CatalogMatcher
from app.services.coverage import summarize_product_coverage
from app.services.customer import IProClient, resolve_actual_customer
from app.services.decision import (
    apply_final_decision,
    build_decision_prompt,
    calculate_hard_reasons,
)
from app.services.documents import DocumentProcessor, build_combined_text
from app.services.llm import LlmClient
from app.services.normalization import deduplicate_strings, normalize_job_payload
from app.services.products import extract_deterministic_positions, merge_positions
from app.services.result import build_result_json
from app.services.seldon import SeldonClient, build_page_text
from app.services.validation import validate_fields


T = TypeVar("T")
logger = logging.getLogger(__name__)


class TenderPipeline:
    def __init__(
        self,
        settings: Settings,
        temp_dir: Path,
        observer: RunObserver | None = None,
    ) -> None:
        self.settings = settings
        self.temp_dir = temp_dir
        self.result_logs: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.observer = observer

    def _run_stage(self, name: str, function: Callable[[], T]) -> T:
        started = time.monotonic()
        if self.observer:
            self.observer.stage_started(name)
        with stage(logger, name):
            try:
                result = function()
            except Exception as exc:
                duration = round(time.monotonic() - started, 3)
                if self.observer:
                    self.observer.stage_finished(name, duration_seconds=duration, error=exc)
                self.result_logs.append(
                    {
                        "time": datetime.now(timezone.utc).isoformat(),
                        "step": name,
                        "status": "error",
                        "durationSeconds": duration,
                        "details": str(exc),
                    }
                )
                raise
        duration = round(time.monotonic() - started, 3)
        if self.observer:
            self.observer.stage_finished(name, duration_seconds=duration)
        self.result_logs.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "step": name,
                "status": "ok",
                "durationSeconds": duration,
                "details": "completed",
            }
        )
        return result

    def run(self, claim: JobClaim) -> dict[str, Any]:
        job = self._run_stage("Normalize Input2", lambda: normalize_job_payload(claim))
        llm = LlmClient(self.settings, job.attempt, observer=self.observer)
        seldon = SeldonClient(self.settings)
        documents_processor = DocumentProcessor(self.settings, self.temp_dir, llm)
        ipro = IProClient(self.settings)
        catalog = CatalogMatcher(self.settings, llm, observer=self.observer)
        try:
            token = self._run_stage("Проверка/получение токена Seldon", lambda: seldon.get_token(job.seldon_token))
            descriptors, seldon_warnings = self._run_stage(
                "Получение документов Seldon",
                lambda: seldon.get_purchase_documents(job, token),
            )
            self.warnings.extend(seldon_warnings)
            page_text = build_page_text(job, descriptors)

            if self.settings.enable_tender_html_fetch and job.tender_url:
                html_text, html_warnings = self._run_stage(
                    "Получение HTML страницы тендера",
                    lambda: documents_processor.fetch_tender_html(job.tender_url or ""),
                )
                self.warnings.extend(html_warnings)
                if html_text:
                    page_text = f"{page_text}\n\n--- HTML СТРАНИЦЫ ТЕНДЕРА ---\n{html_text}"

            parsed_documents, parser_warnings = self._run_stage(
                "Скачивание/парсинг документов",
                lambda: documents_processor.process_all(descriptors),
            )
            self.warnings.extend(parser_warnings)
            if self.observer:
                self.observer.counters(
                    documents_requested=len(descriptors),
                    documents_parsed=sum(document.textQualityOk for document in parsed_documents),
                    download_bytes=documents_processor.downloaded_total,
                )
            logger.info(
                "documents_processed",
                extra={
                    "event": {
                        "stage": "documents",
                        "downloaded_bytes": documents_processor.downloaded_total,
                        "document_count": len(parsed_documents),
                    }
                },
            )
            self.result_logs.append(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "step": "Размер скачанных документов",
                    "status": "ok",
                    "details": f"downloadedBytes={documents_processor.downloaded_total}",
                }
            )
            for document in parsed_documents:
                if document.parserWarning:
                    self.warnings.append(f"{document.fileName}: {document.parserWarning}")

            combined_text, document_lengths, combined_warnings = self._run_stage(
                "Prepare Combined Text for LLM2",
                lambda: build_combined_text(page_text, parsed_documents, self.settings),
            )
            self.warnings.extend(combined_warnings)

            extracted_fields = self._run_stage(
                "AI Agent - Extract Tender Fields2",
                lambda: llm.extract_fields(combined_text),
            )
            fields, meta, validation_warnings = self._run_stage(
                "Validate Fields2",
                lambda: validate_fields(job, extracted_fields, combined_text, parsed_documents),
            )
            self.warnings.extend(validation_warnings)

            fields, meta, customer_warnings, customer_debug = self._run_stage(
                "Resolve Actual Customer",
                lambda: resolve_actual_customer(
                    llm, job, fields, meta, parsed_documents, page_text
                ),
            )
            self.warnings.extend(customer_warnings)

            fields, meta, counterparty_lookup, ipro_warnings = self._run_stage(
                "Проверка контрагента в IPro",
                lambda: ipro.lookup(fields, meta),
            )
            self.warnings.extend(ipro_warnings)

            deterministic_positions = self._run_stage(
                "Детерминированное извлечение товарных позиций из Excel",
                lambda: extract_deterministic_positions(combined_text),
            )
            llm_positions = self._run_stage(
                "AI Agent - Extract Tender Positions",
                lambda: llm.extract_products(
                    combined_text, [position.model_dump() for position in deterministic_positions]
                ),
            )
            positions, position_warnings = self._run_stage(
                "Parse Tender Positions",
                lambda: merge_positions(deterministic_positions, llm_positions),
            )
            self.warnings.extend(position_warnings)

            match_items, catalog_warnings = self._run_stage(
                "Поиск товаров в каталоге/Qdrant",
                lambda: catalog.match_all(positions),
            )
            self.warnings.extend(catalog_warnings)
            product_check = self._run_stage(
                "Summarize Product Coverage",
                lambda: summarize_product_coverage(match_items),
            )

            hard_reasons, checks = self._run_stage(
                "Детерминированные правила решения",
                lambda: calculate_hard_reasons(job, fields, product_check, combined_text),
            )
            checks["counterpartyRequiresWork"] = counterparty_lookup.get("status") != "matched"
            checks["counterpartyEvidence"] = counterparty_lookup.get("reason") or "Контрагент найден в IPro"
            decision_prompt = build_decision_prompt(
                fields=fields,
                hard_reasons=hard_reasons,
                checks=checks,
                product_check=product_check,
                all_text=combined_text,
                maximum_text_chars=self.settings.max_decision_text_chars,
            )
            llm_decision = self._run_stage(
                "AI Agent - Decide Tender Status",
                lambda: llm.decide(decision_prompt),
            )
            fields, meta, decision = self._run_stage(
                "Apply Tender Decision",
                lambda: apply_final_decision(
                    fields=fields,
                    meta=meta,
                    product_check=product_check,
                    hard_reasons=hard_reasons,
                    counterparty_lookup=counterparty_lookup,
                    llm_decision=llm_decision,
                ),
            )

            debug = {
                "htmlLength": len(page_text),
                "documentCount": len(parsed_documents),
                "parsedDocumentCount": sum(document.textQualityOk for document in parsed_documents),
                "documentLinks": [descriptor.get("url") for descriptor in descriptors],
                "combinedTextLength": len(combined_text),
                "llmTextLength": len(combined_text),
                "wasClipped": bool(combined_warnings),
                "toCode": job.to_code,
                "remainingDays": job.remaining_days,
                "reportId": job.report_id,
                "documentTextLengths": document_lengths,
                "aiModel": llm.model,
                "aiModelChain": llm.model_chain,
                "aiModelsUsed": llm.models_used,
                "workerAttempt": job.attempt,
                "actualCustomerResolution": customer_debug,
                "counterpartyLookup": counterparty_lookup,
                "productCheck": product_check,
                "tenderDecision": decision,
                "decisionContext": {**checks, "hardReasons": [reason.as_dict() for reason in hard_reasons]},
            }
            return build_result_json(
                job,
                fields=fields,
                meta=meta,
                product_check=product_check,
                decision=decision,
                warnings=deduplicate_strings(self.warnings),
                logs=self.result_logs,
                debug=debug,
            )
        finally:
            catalog.close()
            ipro.close()
            documents_processor.close()
            seldon.close()
