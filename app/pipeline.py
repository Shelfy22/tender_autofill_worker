from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.config import Settings
from app.logging import stage
from app.models import DocumentAnalysisResult, JobClaim, ParsedDocument, TenderPositionsResponse, TenderResult
from app.observability import RunObserver
from app.services.catalog import CatalogMatcher
from app.services.coverage import summarize_product_coverage
from app.services.customer import IProClient, resolve_actual_customer
from app.services.decision import (
    apply_final_decision,
    build_decision_prompt,
    calculate_hard_reasons,
)
from app.services.document_analysis import (
    build_document_analysis_units,
    fields_from_consolidation,
    result_from_unit,
)
from app.services.documents import (
    DocumentProcessor,
    build_combined_text as build_deterministic_text,
    document_processing_context,
)
from app.services.llm import LlmClient, LlmResponseTruncatedError
from app.services.normalization import deduplicate_strings, normalize_job_payload
from app.services.product_validation import (
    review_spreadsheet_candidate_positions,
    validate_product_candidates,
)
from app.services.products import (
    extract_deterministic_positions,
    extract_seldon_positions,
    merge_positions,
)
from app.services.result import build_result_json
from app.services.seldon import SeldonClient, build_page_text
from app.services.validation import validate_fields


T = TypeVar("T")
logger = logging.getLogger(__name__)
def build_product_extraction_text(
    deterministic_text: str,
    documents: list[ParsedDocument],
) -> tuple[str, list[str], bool]:
    spreadsheet_documents = [
        document
        for document in documents
        if document.spreadsheetTables and document.text.strip()
    ]
    if not spreadsheet_documents:
        return deterministic_text, [], False

    sections = [
        "--- SPREADSHEET DOCUMENTS FOR PRODUCT EXTRACTION ONLY ---",
    ]
    for number, document in enumerate(spreadsheet_documents, start=1):
        sections.extend(
            [
                "",
                f"--- DOCUMENT {number} ---",
                f"fileName: {document.fileName}",
                (
                    f"originalFileName: {document.originalFileName}"
                    if document.originalFileName and document.originalFileName != document.fileName
                    else ""
                ),
                f"documentKind: {document.documentKind}",
                f"extension: {document.fileExtension}",
                f"parserStatus: {document.parserStatus}",
                f"spreadsheetTableCount: {len(document.spreadsheetTables)}",
                document.text,
            ]
        )
    text = "\n".join(section for section in sections if section)
    return (
        text,
        [
            "LLM product extraction was limited to spreadsheet documents; "
            "non-spreadsheet tender text remains available for fields and decision checks."
        ],
        True,
    )

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
        documents_processor = DocumentProcessor(
            self.settings,
            self.temp_dir,
            llm,
            referer_url=job.tender_url or self.settings.seldon_base_url,
            observer=self.observer,
        )
        ipro = IProClient(self.settings)
        catalog = CatalogMatcher(self.settings, llm, observer=self.observer)
        try:
            token = self._run_stage("Проверка/получение токена Seldon", lambda: seldon.get_token(job.seldon_token))
            seldon_documents = self._run_stage(
                "Получение документов Seldon",
                lambda: seldon.get_purchase_documents(job, token),
            )
            descriptors = seldon_documents.documents
            self.warnings.extend(seldon_documents.warnings)
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
            documents_context = document_processing_context(
                descriptors, parsed_documents
            )
            document_context = seldon_documents.decision_context()
            if descriptors:
                document_context.update(documents_context)
            if documents_context.get("documentationUnavailable") is True:
                document_context["documentationMissing"] = True
                documentation_note = str(
                    documents_context.get("documentationNote") or ""
                ).strip()
                if documentation_note:
                    self.warnings.append(documentation_note)
                    self.result_logs.append(
                        {
                            "time": datetime.now(timezone.utc).isoformat(),
                            "step": "Проверка доступности документации",
                            "status": "warning",
                            "details": documentation_note,
                        }
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

            deterministic_text, document_lengths, deterministic_text_warnings = self._run_stage(
                "Prepare Deterministic Text for Regex Rules",
                lambda: build_deterministic_text(page_text, parsed_documents, self.settings),
            )
            self.warnings.extend(deterministic_text_warnings)

            seldon_positions = self._run_stage(
                "Детерминированное извлечение товарных позиций из Seldon purchase",
                lambda: extract_seldon_positions(job.seldon_purchase),
            )
            spreadsheet_tables = [
                table
                for document in parsed_documents
                for table in document.spreadsheetTables
            ]
            deterministic_positions = self._run_stage(
                "Детерминированное извлечение товарных позиций из Excel",
                lambda: extract_deterministic_positions(
                    deterministic_text,
                    spreadsheet_tables,
                ),
            )

            document_analysis_debug: dict[str, Any] = {"enabled": self.settings.enable_document_analysis_pipeline}
            document_consolidation = None
            spreadsheet_review_debug: dict[str, Any] = {"reviewRequested": False}
            if self.settings.enable_document_analysis_pipeline:
                deterministic_positions, spreadsheet_review_warnings, spreadsheet_review_debug = self._run_stage(
                    "Analyze Spreadsheet Candidate Units",
                    lambda: review_spreadsheet_candidate_positions(llm, deterministic_positions),
                )
                self.warnings.extend(spreadsheet_review_warnings)
                document_analysis_units, unit_warnings = self._run_stage(
                    "Build Document Analysis Units",
                    lambda: build_document_analysis_units(
                        page_text,
                        parsed_documents,
                        [],
                        self.settings,
                        skip_spreadsheet_candidate_units=True,
                    ),
                )
                self.warnings.extend(unit_warnings)
                document_analysis_results: list[DocumentAnalysisResult] = []
                incomplete_document_unit_ids: list[str] = []

                def analyze_unit_safely(unit: Any) -> DocumentAnalysisResult:
                    try:
                        return result_from_unit(unit, llm.analyze_document_unit(unit))
                    except LlmResponseTruncatedError as exc:
                        warning = (
                            f"Document Analysis unit {unit.unitId} "
                            f"({unit.fileName or unit.sourceType} {unit.partIndex}/{unit.partTotal}) "
                            f"returned a truncated LLM response and was marked incomplete: {exc}"
                        )
                        self.warnings.append(warning)
                        incomplete_document_unit_ids.append(unit.unitId)
                        return DocumentAnalysisResult(
                            unitId=unit.unitId,
                            inputSha256=unit.inputSha256,
                            sourceType=unit.sourceType,
                            fileName=unit.fileName,
                            partIndex=unit.partIndex,
                            partTotal=unit.partTotal,
                            analysisIncomplete=True,
                            warnings=[warning],
                        )

                for unit in document_analysis_units:
                    document_analysis_results.append(
                        self._run_stage(
                            f"Analyze Document Unit: {unit.fileName or unit.sourceType} {unit.partIndex}/{unit.partTotal}",
                            lambda unit=unit: analyze_unit_safely(unit),
                        )
                    )
                document_consolidation = self._run_stage(
                    "Consolidate Tender Analysis",
                    lambda: llm.consolidate_document_analysis(
                        [result.model_dump(mode="json") for result in document_analysis_results]
                    ),
                )
                if incomplete_document_unit_ids:
                    incomplete_ids = list(document_consolidation.incompleteUnitIds)
                    for unit_id in incomplete_document_unit_ids:
                        if unit_id not in incomplete_ids:
                            incomplete_ids.append(unit_id)
                    consolidation_warnings = list(document_consolidation.warnings)
                    warning = (
                        "Document Analysis completed with incomplete units: "
                        + ", ".join(incomplete_document_unit_ids)
                    )
                    if warning not in consolidation_warnings:
                        consolidation_warnings.append(warning)
                    document_consolidation = document_consolidation.model_copy(
                        update={
                            "incompleteUnitIds": incomplete_ids,
                            "warnings": consolidation_warnings,
                        }
                    )
                self.warnings.extend(document_consolidation.warnings)
                extracted_fields = self._run_stage(
                    "Build Fields From Document Analysis",
                    lambda: fields_from_consolidation(document_consolidation),
                )
                document_analysis_debug.update(
                    {
                        "unitCount": len(document_analysis_units),
                        "resultCount": len(document_analysis_results),
                        "consolidatedProductCount": len(document_consolidation.products),
                        "reasonHitCount": len(document_consolidation.reasonHits),
                        "fieldCandidateCount": len(document_consolidation.fieldCandidates),
                        "incompleteUnitIds": document_consolidation.incompleteUnitIds,
                    }
                )
                product_extraction_text = ""
                product_text_spreadsheet_only = True
                llm_positions = TenderPositionsResponse(
                    products=document_consolidation.products,
                    warnings=document_consolidation.warnings,
                )
            else:
                extracted_fields = self._run_stage(
                    "AI Agent - Extract Tender Fields2",
                    lambda: llm.extract_fields(deterministic_text),
                )
                deterministic_positions, spreadsheet_review_warnings, spreadsheet_review_debug = self._run_stage(
                    "Review Spreadsheet Product Candidates",
                    lambda: review_spreadsheet_candidate_positions(llm, deterministic_positions),
                )
                self.warnings.extend(spreadsheet_review_warnings)
                product_extraction_text, product_text_warnings, product_text_spreadsheet_only = self._run_stage(
                    "Prepare Product Extraction Text",
                    lambda: build_product_extraction_text(deterministic_text, parsed_documents),
                )
                self.warnings.extend(product_text_warnings)
                llm_positions = self._run_stage(
                    "AI Agent - Extract Tender Positions",
                    lambda: llm.extract_products(
                        product_extraction_text,
                        [position.model_dump() for position in deterministic_positions],
                        trust_deterministic=product_text_spreadsheet_only and bool(deterministic_positions),
                    ),
                )

            fields, meta, validation_warnings = self._run_stage(
                "Validate Fields2",
                lambda: validate_fields(job, extracted_fields, deterministic_text, parsed_documents),
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

            positions, position_warnings = self._run_stage(
                "Parse Tender Positions",
                lambda: merge_positions(
                    deterministic_positions,
                    llm_positions,
                    seldon_positions,
                ),
            )
            self.warnings.extend(position_warnings)

            if document_consolidation is None:
                positions, validation_warnings, validation_debug = self._run_stage(
                    "Validate Product Candidates",
                    lambda: validate_product_candidates(llm, positions),
                )
                self.warnings.extend(validation_warnings)
            else:
                validation_debug = {
                    "reviewRequested": False,
                    "skipped": "Tender Consolidator already performed product candidate cleanup.",
                    "originalPositionCount": len(positions),
                    "validatedPositionCount": len(positions),
                    "requiresManualReview": bool(document_consolidation.incompleteUnitIds),
                    "hierarchy": {},
                }

            match_items, catalog_warnings = self._run_stage(
                "Поиск товаров в каталоге/Qdrant",
                lambda: catalog.match_all(positions),
            )
            self.warnings.extend(catalog_warnings)
            product_check = self._run_stage(
                "Summarize Product Coverage",
                lambda: summarize_product_coverage(
                    match_items,
                    supply_value_threshold_enabled=job.report_id == 3,
                    lot_divisible=fields.get("lotDivisible"),
                ),
            )
            product_check["validation"] = validation_debug
            product_check["hierarchy"] = validation_debug.get("hierarchy", {})
            product_check["validationAdvisoryOnly"] = (
                validation_debug.get("requiresManualReview") is True
            )
            product_check["coverageDecisionEligible"] = True

            hard_reasons, checks = self._run_stage(
                "Детерминированные правила решения",
                lambda: calculate_hard_reasons(
                    job,
                    fields,
                    product_check,
                    deterministic_text,
                    document_context=document_context,
                ),
            )
            checks["counterpartyRequiresWork"] = counterparty_lookup.get("status") != "matched"
            checks["counterpartyEvidence"] = counterparty_lookup.get("reason") or "Контрагент найден в IPro"
            if document_consolidation is not None:
                checks["documentReasonHits"] = [
                    reason.model_dump(mode="json")
                    for reason in document_consolidation.reasonHits
                ]
                checks["documentAnalysisIncomplete"] = bool(document_consolidation.incompleteUnitIds)
                checks["documentAnalysisIncompleteUnitIds"] = document_consolidation.incompleteUnitIds
            decision_prompt = build_decision_prompt(
                fields=fields,
                hard_reasons=hard_reasons,
                checks=checks,
                product_check=product_check,
                all_text=(
                    json.dumps(
                        {
                            "documentAnalysis": document_consolidation.model_dump(mode="json"),
                            "note": "Compact structured analysis; raw tender text is not passed to final decision.",
                        },
                        ensure_ascii=False,
                    )
                    if document_consolidation is not None
                    else deterministic_text
                ),
                maximum_text_chars=self.settings.max_decision_text_chars,
                report_id=job.report_id,
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
                    report_id=job.report_id,
                ),
            )

            debug = {
                "htmlLength": len(page_text),
                "documentCount": len(parsed_documents),
                "parsedDocumentCount": sum(document.textQualityOk for document in parsed_documents),
                "documentLinks": [descriptor.get("url") for descriptor in descriptors],
                "seldonDocumentsStatus": document_context,
                "seldonStructuredProductsCount": len(seldon_positions),
                "deterministicTextLength": len(deterministic_text),
                "llmTextLength": 0 if document_consolidation is not None else len(deterministic_text),
                "productExtractionTextLength": len(product_extraction_text),
                "productExtractionSpreadsheetOnly": product_text_spreadsheet_only,
                "documentAnalysis": document_analysis_debug,
                "deterministicTextWasClipped": bool(deterministic_text_warnings),
                "wasClipped": bool(deterministic_text_warnings),
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
                "spreadsheetCandidateReview": spreadsheet_review_debug,
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
