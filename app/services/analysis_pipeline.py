"""Shared document and product stages for tender processing and offline matching."""

from collections.abc import Callable
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from openai import OpenAIError
from pydantic import ValidationError

from app.models import DocumentAnalysisResult, DocumentAnalysisUnit
from app.services.document_analysis import (
    compact_document_analysis_results,
    prepare_consolidation_payload,
    result_from_unit,
)
from app.services.llm import LlmMalformedResponseError, LlmResponseTruncatedError
from app.services.product_characteristics import associate_characteristic_sets
from app.services.field_policy import is_tender_field_enabled, is_decision_reason_enabled


def direct_stage(name: str, call: Callable[[], Any]) -> Any:
    return call()


def analyze_document_units(
    llm: Any,
    units: list[DocumentAnalysisUnit],
    warnings: list[str],
    *,
    run_stage: Callable = direct_stage,
) -> tuple[list[DocumentAnalysisResult], list[str]]:
    """An exhausted document call is partial evidence; a task soft limit is fatal."""
    incomplete: list[str] = []

    def analyze(unit: DocumentAnalysisUnit) -> list[DocumentAnalysisResult]:
        try:
            return [result_from_unit(unit, llm.analyze_document_unit(unit))]
        except SoftTimeLimitExceeded:
            raise
        except (
            OpenAIError, ValidationError, ValueError, TimeoutError,
            LlmMalformedResponseError, LlmResponseTruncatedError,
        ) as exc:
            children = list(unit.batchedDocumentUnits)
            if children:
                warnings.append(
                    f"Document Analysis batched unit {unit.unitId} failed with "
                    f"{type(exc).__name__}; retrying {len(children)} documents individually."
                )
                return [result for child in children for result in analyze_staged(child, retry=True)]
            detail = (
                "returned a truncated LLM response"
                if isinstance(exc, LlmResponseTruncatedError)
                else f"failed with {type(exc).__name__}"
            )
            warning = (
                f"Document Analysis unit {unit.unitId} "
                f"({unit.fileName or unit.sourceType} {unit.partIndex}/{unit.partTotal}) "
                f"{detail} and was marked incomplete; deterministic positions preserved."
            )
            warnings.append(warning)
            incomplete.append(unit.unitId)
            return [DocumentAnalysisResult(
                unitId=unit.unitId, inputSha256=unit.inputSha256,
                sourceType=unit.sourceType, fileName=unit.fileName,
                partIndex=unit.partIndex, partTotal=unit.partTotal,
                analysisIncomplete=True, warnings=[warning],
            )]

    def analyze_staged(unit: DocumentAnalysisUnit, retry: bool = False) -> list[DocumentAnalysisResult]:
        label = "Analyze Document Unit Retry" if retry else "Analyze Document Unit"
        return run_stage(
            f"{label}: {unit.fileName or unit.sourceType} {unit.partIndex}/{unit.partTotal}",
            lambda: analyze(unit),
        )

    results = [result for unit in units for result in analyze_staged(unit)]
    return results, incomplete


def consolidate_analysis(
    llm: Any,
    results: list[DocumentAnalysisResult],
    incomplete_unit_ids: list[str],
    warnings: list[str],
    *,
    run_stage: Callable = direct_stage,
):
    """Consolidation and its validation must succeed before tender processing continues."""
    compact_payload, dedup_debug = run_stage(
        "Deduplicate Document Analysis Products",
        lambda: compact_document_analysis_results(results),
    )
    payload, payload_debug = run_stage(
        "Compact Document Analysis Consolidation Payload",
        lambda: prepare_consolidation_payload(compact_payload),
    )
    dedup_debug.update(payload_debug)
    consolidation = run_stage(
        "Consolidate Tender Analysis", lambda: llm.consolidate_document_analysis(payload),
    )
    incomplete = list(dict.fromkeys([*consolidation.incompleteUnitIds, *incomplete_unit_ids]))
    consolidation_warnings = list(consolidation.warnings)
    if incomplete_unit_ids:
        consolidation_warnings.append(
            "Document Analysis completed with incomplete units: " + ", ".join(incomplete_unit_ids)
        )
    products, unmatched, association_warnings, association_debug = run_stage(
        "Associate Product Characteristics",
        lambda: associate_characteristic_sets(consolidation.products, consolidation.characteristicSets),
    )
    consolidation = consolidation.model_copy(update={
        "products": products,
        "characteristicSets": unmatched,
        "incompleteUnitIds": incomplete,
        "warnings": list(dict.fromkeys([*consolidation_warnings, *association_warnings])),
        "fieldCandidates": [
            candidate for candidate in consolidation.fieldCandidates
            if is_tender_field_enabled(candidate.fieldName.strip())
        ],
        "reasonHits": [
            reason for reason in consolidation.reasonHits
            if is_decision_reason_enabled(reason.reason)
        ],
    })
    warnings.extend(consolidation.warnings)
    return consolidation, dedup_debug, association_debug
