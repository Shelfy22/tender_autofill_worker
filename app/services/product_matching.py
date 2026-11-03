from __future__ import annotations

from billiard.exceptions import SoftTimeLimitExceeded

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from app.config import Settings
from app.models import ParsedDocument, TenderPositionsResponse
from app.services.catalog import CatalogMatcher
from app.services.coverage import summarize_product_coverage
from app.services.analysis_pipeline import analyze_document_units, consolidate_analysis
from app.services.document_analysis import build_document_analysis_units
from app.services.documents import DocumentProcessor, build_combined_text, safe_filename
from app.services.llm import LlmClient
from app.services.product_pipeline import process_products
from app.services.products import extract_deterministic_positions, merge_positions
from app.services.table_schema import (
    classify_spreadsheet_tables,
    extract_text_document_tables,
    table_schema_summaries,
)


@dataclass(frozen=True)
class LocalDocument:
    file_name: str
    path: Path


def run_product_matching_from_files(
    files: list[LocalDocument],
    settings: Settings,
    *,
    tender_name: str = "",
) -> tuple[bytes, dict[str, Any]]:
    if not files:
        raise ValueError("Не переданы документы для обработки")

    settings.temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="product-match-", dir=settings.temp_root) as temp_name:
        temp_dir = Path(temp_name)
        llm = LlmClient(settings, attempt=1)
        processor = DocumentProcessor(settings, temp_dir, llm)
        catalog = CatalogMatcher(settings, llm)
        try:
            parsed_documents: list[ParsedDocument] = []
            warnings: list[str] = []
            uploads_dir = temp_dir / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)

            for index, item in enumerate(files[: settings.max_documents], start=1):
                file_name = safe_filename(item.file_name, f"document_{index}")
                destination = uploads_dir / f"{index:03d}_{file_name}"
                shutil.copyfile(item.path, destination)
                descriptor = {"index": index, "fileName": file_name, "url": ""}
                try:
                    parsed_documents.extend(processor._process_path(destination, descriptor, depth=0))
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    warnings.append(f"{file_name}: {exc}")
                    parsed_documents.append(
                        ParsedDocument(
                            documentIndex=index,
                            fileName=file_name,
                            originalFileName=file_name,
                            parserStatus="error",
                            parserWarning=str(exc),
                            parserError=str(exc),
                        )
                    )

            combined_text, document_lengths, text_warnings = build_combined_text(
                "", parsed_documents, settings
            )
            warnings.extend(text_warnings)
            spreadsheet_tables = [
                table for document in parsed_documents for table in document.spreadsheetTables
            ]
            text_document_tables = extract_text_document_tables(parsed_documents)
            classified_tables, table_schema_warnings, table_schema_debug = (
                classify_spreadsheet_tables(
                    llm,
                    spreadsheet_tables + text_document_tables,
                    settings,
                )
            )
            spreadsheet_tables = classified_tables[: len(spreadsheet_tables)]
            text_document_tables = classified_tables[len(spreadsheet_tables):]
            document_table_summaries = table_schema_summaries(classified_tables)
            warnings.extend(table_schema_warnings)
            deterministic_positions = extract_deterministic_positions(
                combined_text,
                spreadsheet_tables,
                settings.max_tender_positions,
                text_document_tables,
            )

            deterministic_positions, canonicalization_warnings = merge_positions(
                deterministic_positions,
                None,
                None,
                settings.max_tender_positions,
            )
            warnings.extend(canonicalization_warnings)

            units, unit_warnings = build_document_analysis_units(
                "",
                parsed_documents,
                deterministic_positions,
                settings,
                skip_spreadsheet_candidate_units=False,
                table_schema_summaries=document_table_summaries,
            )
            warnings.extend(unit_warnings)

            analysis_results, incomplete_unit_ids = analyze_document_units(llm, units, warnings)
            consolidation, consolidation_dedup_debug, characteristic_association_debug = consolidate_analysis(
                llm, analysis_results, incomplete_unit_ids, warnings,
            )
            unresolved_characteristic_sets = consolidation.characteristicSets

            positions, position_warnings = merge_positions(
                deterministic_positions,
                TenderPositionsResponse(
                    products=consolidation.products,
                    warnings=consolidation.warnings,
                ),
                [],
                settings.max_tender_positions,
            )
            warnings.extend(position_warnings)
            processed = process_products(llm, catalog, settings, positions, warnings)
            positions = processed.positions
            match_items = processed.matches
            characteristic_finalize_debug = processed.characteristics
            validation_debug = processed.validation
            search_query_debug = processed.search
            final_validation_debug = processed.final_validation
            product_check = summarize_product_coverage(
                match_items,
                supply_value_threshold_enabled=False,
                lot_divisible=None,
                source_position_count=len(positions),
                position_limit=settings.catalog_match_max_positions,
            )

            from app.services.product_matching_export import build_product_matching_workbook

            workbook = build_product_matching_workbook(
                product_check,
                selected_catalog_params=[item.match.catalog_params for item in match_items],
            )
            debug = {
                "tenderName": tender_name,
                "documentsParsed": sum(document.textQualityOk for document in parsed_documents),
                "documentCount": len(parsed_documents),
                "documentTextLengths": document_lengths,
                "tableSchemaAudit": table_schema_debug,
                "documentAnalysisUnits": len(units),
                "documentAnalysisResults": len(analysis_results),
                "preConsolidationDeduplication": consolidation_dedup_debug,
                "incompleteUnitIds": consolidation.incompleteUnitIds,
                "productCandidateValidation": validation_debug,
                "finalProductIntegrityValidation": final_validation_debug,
                "characteristicAssociation": {
                    **characteristic_association_debug,
                    **characteristic_finalize_debug,
                    "unresolvedSetCount": len(unresolved_characteristic_sets),
                },
                "productSearchQuery": search_query_debug,
                "deterministicProductCount": len(deterministic_positions),
                "consolidatedProductCount": len(consolidation.products),
                "extractedProductCount": len(positions),
                "matchedProductCount": product_check.get("total"),
                "catalogPositionLimit": settings.catalog_match_max_positions,
                "catalogPositionLimitApplied": product_check.get(
                    "catalogPositionLimitApplied"
                ),
                "warnings": list(dict.fromkeys(warnings)),
            }
            return workbook, debug
        finally:
            catalog.close()
            processor.close()
