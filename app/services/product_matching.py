from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.models import DocumentAnalysisResult, ParsedDocument, TenderPositionsResponse
from app.services.catalog import CatalogMatcher
from app.services.coverage import summarize_product_coverage
from app.services.document_analysis import build_document_analysis_units, result_from_unit
from app.services.documents import DocumentProcessor, build_combined_text, safe_filename
from app.services.llm import LlmClient, LlmResponseTruncatedError
from app.services.products import extract_deterministic_positions, merge_positions


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
            deterministic_positions = extract_deterministic_positions(
                combined_text,
                spreadsheet_tables,
            )

            units, unit_warnings = build_document_analysis_units(
                "",
                parsed_documents,
                deterministic_positions,
                settings,
                skip_spreadsheet_candidate_units=False,
            )
            warnings.extend(unit_warnings)

            analysis_results: list[DocumentAnalysisResult] = []
            incomplete_unit_ids: list[str] = []
            for unit in units:
                try:
                    response = llm.analyze_document_unit(unit)
                    analysis_results.append(result_from_unit(unit, response))
                except LlmResponseTruncatedError as exc:
                    incomplete_unit_ids.append(unit.unitId)
                    warnings.append(
                        f"{unit.fileName or unit.sourceType} {unit.partIndex}/{unit.partTotal}: {exc}"
                    )
                    analysis_results.append(
                        DocumentAnalysisResult(
                            unitId=unit.unitId,
                            inputSha256=unit.inputSha256,
                            sourceType=unit.sourceType,
                            fileName=unit.fileName,
                            partIndex=unit.partIndex,
                            partTotal=unit.partTotal,
                            analysisIncomplete=True,
                            warnings=[str(exc)],
                        )
                    )

            consolidation = llm.consolidate_document_analysis(
                [result.model_dump(mode="json") for result in analysis_results]
            )
            warnings.extend(consolidation.warnings)
            for unit_id in incomplete_unit_ids:
                if unit_id not in consolidation.incompleteUnitIds:
                    consolidation.incompleteUnitIds.append(unit_id)

            positions, position_warnings = merge_positions(
                deterministic_positions,
                TenderPositionsResponse(
                    products=consolidation.products,
                    warnings=consolidation.warnings,
                ),
                [],
            )
            warnings.extend(position_warnings)

            match_items, catalog_warnings = catalog.match_all(positions)
            warnings.extend(catalog_warnings)
            product_check = summarize_product_coverage(
                match_items,
                supply_value_threshold_enabled=False,
                lot_divisible=None,
            )

            from app.services.product_matching_export import build_product_matching_workbook

            workbook = build_product_matching_workbook(product_check)
            debug = {
                "tenderName": tender_name,
                "documentsParsed": sum(document.textQualityOk for document in parsed_documents),
                "documentCount": len(parsed_documents),
                "documentTextLengths": document_lengths,
                "documentAnalysisUnits": len(units),
                "documentAnalysisResults": len(analysis_results),
                "incompleteUnitIds": consolidation.incompleteUnitIds,
                "deterministicProductCount": len(deterministic_positions),
                "consolidatedProductCount": len(consolidation.products),
                "matchedProductCount": product_check.get("total"),
                "warnings": list(dict.fromkeys(warnings)),
            }
            return workbook, debug
        finally:
            catalog.close()
            processor.close()