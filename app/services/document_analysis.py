from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import Settings
from app.models import (
    DocumentAnalysisResult,
    DocumentAnalysisUnit,
    ExtractedFieldsResponse,
    FieldValue,
    ParsedDocument,
    TenderConsolidationResponse,
    TenderPosition,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _split_text_source(text: str, max_chars: int) -> list[str]:
    source = str(text or "").strip()
    if not source:
        return []
    if len(source) <= max_chars:
        return [source]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in source.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        block_len = len(block) + 2
        if len(block) > max_chars:
            if current:
                parts.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            for index in range(0, len(block), max_chars):
                chunk = block[index:index + max_chars].strip()
                if chunk:
                    parts.append(chunk)
            continue
        if current and current_len + block_len > max_chars:
            parts.append("\n\n".join(current).strip())
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
    if current:
        parts.append("\n\n".join(current).strip())
    return parts


def _position_candidate_id(position: TenderPosition) -> str:
    if position.candidateId:
        return position.candidateId
    reference = position.sourceReference
    if reference is not None and reference.row is not None:
        return f"xlsx:{reference.fileName}:{reference.sheet}:{reference.row}"
    return _sha256(json.dumps(position.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))[:24]


def _candidate_payload(position: TenderPosition) -> dict[str, Any]:
    return {
        "candidateId": _position_candidate_id(position),
        "product": position.product,
        "productQuery": position.productQuery,
        "quantity": position.quantity,
        "unit": position.unit,
        "sourceReference": (
            position.sourceReference.model_dump()
            if position.sourceReference is not None
            else None
        ),
        "sourceCells": position.sourceCells,
        "evidence": position.evidence[:300],
    }


def _spreadsheet_units_for_document(
    document: ParsedDocument,
    positions: list[TenderPosition],
    settings: Settings,
    unit_number: int,
) -> tuple[list[DocumentAnalysisUnit], int]:
    max_rows = max(1, int(settings.spreadsheet_candidate_review_max_rows))
    max_chars = max(1_000, int(settings.spreadsheet_candidate_review_max_chars))
    candidates = [_candidate_payload(position) for position in positions]
    if not candidates:
        return [], unit_number

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for candidate in candidates:
        candidate_chars = len(json.dumps(candidate, ensure_ascii=False))
        if current and (len(current) >= max_rows or current_chars + candidate_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(candidate)
        current_chars += candidate_chars
    if current:
        batches.append(current)

    units: list[DocumentAnalysisUnit] = []
    total = len(batches)
    for index, batch in enumerate(batches, start=1):
        unit_id = f"unit:{unit_number}:xlsx:{document.documentIndex}:part:{index}"
        normalized = json.dumps(batch, ensure_ascii=False, sort_keys=True)
        units.append(
            DocumentAnalysisUnit(
                unitId=unit_id,
                sourceType="spreadsheet",
                documentIndex=document.documentIndex,
                fileName=document.fileName,
                documentKind=document.documentKind,
                partIndex=index,
                partTotal=total,
                text=(
                    f"Spreadsheet candidate batch for {document.fileName}; "
                    "raw table metadata is in spreadsheetCandidates."
                ),
                spreadsheetCandidates=batch,
                inputSha256=_sha256(f"{unit_id}\n{normalized}"),
            )
        )
        unit_number += 1
    return units, unit_number


def build_document_analysis_units(
    page_text: str,
    documents: list[ParsedDocument],
    deterministic_positions: list[TenderPosition],
    settings: Settings,
    *,
    skip_spreadsheet_candidate_units: bool = False,
) -> tuple[list[DocumentAnalysisUnit], list[str]]:
    """Create bounded source-oriented analysis units before any LLM call.

    This is intentionally not a runtime fallback/chunk recursion. Units are
    derived once from source structure: Seldon page, each document part, and
    spreadsheet candidate row batches.
    """
    warnings: list[str] = []
    max_chars = max(5_000, int(settings.document_analysis_unit_max_chars))
    max_units = max(1, int(settings.document_analysis_max_units))
    units: list[DocumentAnalysisUnit] = []
    unit_number = 1

    page_parts = _split_text_source(page_text, max_chars)
    page_total = max(1, len(page_parts))
    for page_index, text_part in enumerate(page_parts, start=1):
        unit_id = f"unit:{unit_number}:seldon_page:part:{page_index}"
        units.append(
            DocumentAnalysisUnit(
                unitId=unit_id,
                sourceType="seldon_page",
                fileName="seldon_page",
                documentKind="seldon_page",
                partIndex=page_index,
                partTotal=page_total,
                text=text_part,
                inputSha256=_sha256(f"{unit_id}\n{text_part}"),
            )
        )
        unit_number += 1

    for document in documents:
        if document.spreadsheetTables and not skip_spreadsheet_candidate_units:
            document_positions = [
                position
                for position in deterministic_positions
                if position.sourceReference is not None
                and position.sourceReference.fileName == document.fileName
            ]
            spreadsheet_units, unit_number = _spreadsheet_units_for_document(
                document,
                document_positions,
                settings,
                unit_number,
            )
            if spreadsheet_units:
                units.extend(spreadsheet_units)
                continue
            warnings.append(
                f"Spreadsheet document {document.fileName} has no deterministic candidates; "
                "falling back to bounded document text analysis."
            )

        text_parts = _split_text_source(document.text, max_chars)
        total = max(1, len(text_parts))
        for index, text_part in enumerate(text_parts, start=1):
            unit_id = f"unit:{unit_number}:document:{document.documentIndex}:part:{index}"
            units.append(
                DocumentAnalysisUnit(
                    unitId=unit_id,
                    sourceType="document",
                    documentIndex=document.documentIndex,
                    fileName=document.fileName,
                    documentKind=document.documentKind,
                    partIndex=index,
                    partTotal=total,
                    text=text_part,
                    inputSha256=_sha256(f"{unit_id}\n{text_part}"),
                )
            )
            unit_number += 1

    if len(units) > max_units:
        warnings.append(
            f"Document Analysis units limited: {len(units)} -> {max_units}; remaining units marked incomplete."
        )
        units = units[:max_units]
    return units, warnings


def result_from_unit(
    unit: DocumentAnalysisUnit,
    response: Any,
) -> DocumentAnalysisResult:
    parsed = response if isinstance(response, DocumentAnalysisResult) else DocumentAnalysisResult(
        unitId=unit.unitId,
        inputSha256=unit.inputSha256,
        sourceType=unit.sourceType,
        fileName=unit.fileName,
        partIndex=unit.partIndex,
        partTotal=unit.partTotal,
        **response.model_dump(),
    )
    return parsed


def fields_from_consolidation(
    consolidation: TenderConsolidationResponse,
) -> ExtractedFieldsResponse:
    fields: dict[str, FieldValue] = {}
    priority = {"high": 3, "medium": 2, "low": 1}
    for candidate in consolidation.fieldCandidates:
        name = candidate.fieldName.strip()
        if not name:
            continue
        current = fields.get(name)
        if current is not None and priority.get(current.confidence, 0) >= priority.get(candidate.confidence, 0):
            continue
        fields[name] = FieldValue(
            value=candidate.value,
            confidence=candidate.confidence,
            source=json.dumps(candidate.sourceReference, ensure_ascii=False)[:500] or None,
            evidence=candidate.evidence[:500] or None,
        )
    return ExtractedFieldsResponse(fields=fields, warnings=list(consolidation.warnings))
