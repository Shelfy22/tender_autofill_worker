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


_SPREADSHEET_TEXT_EXTENSIONS = {".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".csv"}


def _document_extension(document: ParsedDocument) -> str:
    extension = str(document.fileExtension or "").strip().lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if not extension:
        file_name = str(document.fileName or "").strip().lower()
        if "." in file_name:
            extension = f".{file_name.rsplit('.', 1)[-1]}"
    return extension


def _is_spreadsheet_text_fallback(document: ParsedDocument) -> bool:
    return bool(document.spreadsheetTables) or _document_extension(document) in _SPREADSHEET_TEXT_EXTENSIONS


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

    documents_per_unit = max(
        1,
        int(getattr(settings, "document_analysis_documents_per_unit", 1)),
    )
    current_documents: list[ParsedDocument] = []
    current_raw_parts: list[str] = []
    current_sections: list[str] = []
    current_chars = 0

    def document_section(document: ParsedDocument, text_part: str, index: int, total: int) -> str:
        return "\n".join(
            section
            for section in (
                f"--- DOCUMENT {document.documentIndex} ---",
                f"fileName: {document.fileName}",
                (
                    f"originalFileName: {document.originalFileName}"
                    if document.originalFileName and document.originalFileName != document.fileName
                    else ""
                ),
                f"documentKind: {document.documentKind}",
                f"extension: {document.fileExtension}",
                f"parserStatus: {document.parserStatus}",
                f"part: {index}/{total}",
                "",
                text_part,
            )
            if section is not None
        ).strip()

    def make_document_unit(
        *,
        unit_id: str,
        document_index: int | None,
        file_name: str,
        document_kind: str,
        part_index: int,
        part_total: int,
        text: str,
        batched_document_units: list[DocumentAnalysisUnit] | None = None,
    ) -> DocumentAnalysisUnit:
        return DocumentAnalysisUnit(
            unitId=unit_id,
            sourceType="document",
            documentIndex=document_index,
            fileName=file_name,
            documentKind=document_kind,
            partIndex=part_index,
            partTotal=part_total,
            text=text,
            batchedDocumentUnits=batched_document_units or [],
            inputSha256=_sha256(f"{unit_id}\n{text}"),
        )

    def add_document_unit(unit: DocumentAnalysisUnit) -> None:
        nonlocal unit_number
        units.append(unit)
        unit_number += 1

    def make_single_unit(
        document: ParsedDocument,
        text_part: str,
        *,
        part_index: int = 1,
        part_total: int = 1,
        unit_id: str | None = None,
    ) -> DocumentAnalysisUnit:
        actual_unit_id = unit_id or f"unit:{unit_number}:document:{document.documentIndex}:part:{part_index}"
        return make_document_unit(
            unit_id=actual_unit_id,
            document_index=document.documentIndex,
            file_name=document.fileName,
            document_kind=document.documentKind,
            part_index=part_index,
            part_total=part_total,
            text=text_part,
        )

    def flush_document_batch() -> None:
        nonlocal current_documents, current_raw_parts, current_sections, current_chars
        if not current_documents:
            return
        if len(current_documents) == 1:
            add_document_unit(make_single_unit(current_documents[0], current_raw_parts[0]))
        else:
            indexes = "-".join(str(document.documentIndex) for document in current_documents)
            unit_id = f"unit:{unit_number}:documents:{indexes}"
            file_names = "; ".join(document.fileName for document in current_documents)
            text = "\n\n".join(current_sections).strip()
            child_units = [
                make_single_unit(
                    document,
                    text_part,
                    unit_id=f"{unit_id}:document:{document.documentIndex}",
                )
                for document, text_part in zip(current_documents, current_raw_parts)
            ]
            add_document_unit(
                make_document_unit(
                    unit_id=unit_id,
                    document_index=None,
                    file_name=file_names,
                    document_kind="document_batch",
                    part_index=1,
                    part_total=1,
                    text=text,
                    batched_document_units=child_units,
                )
            )
        current_documents = []
        current_raw_parts = []
        current_sections = []
        current_chars = 0

    for document in documents:
        if document.spreadsheetTables and not skip_spreadsheet_candidate_units:
            flush_document_batch()
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
            section = document_section(document, text_part, index, total)
            section_chars = len(section)
            can_batch_whole_document = total == 1 and section_chars <= max_chars
            can_batch_document = can_batch_whole_document and not _is_spreadsheet_text_fallback(document)
            if not can_batch_document:
                flush_document_batch()
                add_document_unit(make_single_unit(document, text_part, part_index=index, part_total=total))
                continue
            if current_documents and (
                len(current_documents) >= documents_per_unit
                or current_chars + section_chars > max_chars
            ):
                flush_document_batch()
            current_documents.append(document)
            current_raw_parts.append(text_part)
            current_sections.append(section)
            current_chars += section_chars

    flush_document_batch()
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
