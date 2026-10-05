from __future__ import annotations

import re
from typing import Any

from app.config import Settings
from app.models import ParsedDocument, SpreadsheetRow, SpreadsheetTable, SpreadsheetTableSchemaResponse


_NUMBER_PATTERN = re.compile(r"^[+-]?\d+(?:[,.]\d+)?$")
_UNIT_PATTERN = re.compile(
    r"^(?:\u0448\u0442\.?|\u0448\u0442\u0443\u043a\u0430|\u0435\u0434\.?|\u043c|\u043a\u043c|\u043a\u0433|\u0433|\u043b|\u043c2|\u043c3|\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442|\u043d\u0430\u0431\u043e\u0440)$",
    re.IGNORECASE,
)
_CODE_PATTERN = re.compile(r"^(?:\d{2,}|\d{2}(?:[.\-]\d{1,4}){2,})$")
_TEXT_TABLE_MARKER = re.compile(r"^\s*Таблица\s+(?:Word|PDF|RTF)\s+\d+\s*$", re.IGNORECASE)
_TEXT_ROW = re.compile(r"^\s*Строка\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
_TEXT_CELL = re.compile(r"\s*([A-Z]{1,3})\s*:\s*(.*?)(?=\s*\|\s*[A-Z]{1,3}\s*:|$)")
_BLOCKED_TABLE_ROLES = {"delivery_schedule", "offer_form", "contract_template", "other", "ambiguous"}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _column_order(column: str) -> int:
    result = 0
    for char in column.upper():
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _table_columns(table: SpreadsheetTable) -> list[str]:
    return sorted(
        {column for row in table.rows for column in row.cells},
        key=_column_order,
    )


def _sample_row_indexes(row_count: int, limit: int = 16) -> list[int]:
    if row_count <= limit:
        return list(range(row_count))
    indexes = set(range(min(6, row_count)))
    indexes.update(range(max(0, row_count - 6), row_count))
    for numerator in range(1, 5):
        indexes.add((row_count - 1) * numerator // 5)
    return sorted(indexes)[:limit]


def extract_text_document_tables(documents: list[ParsedDocument]) -> list[SpreadsheetTable]:
    """Build compact table objects from Word/PDF/RTF parser markers."""
    tables: list[SpreadsheetTable] = []
    for document in documents:
        current_name = ""
        current_rows: list[SpreadsheetRow] = []

        def flush() -> None:
            nonlocal current_name, current_rows
            if current_name and current_rows:
                tables.append(SpreadsheetTable(fileName=document.fileName, sheet=current_name, rows=current_rows))
            current_name = ""
            current_rows = []

        for raw_line in (document.text or "").splitlines():
            line = raw_line.strip()
            if _TEXT_TABLE_MARKER.fullmatch(line):
                flush()
                current_name = line
                continue
            if not current_name:
                continue
            match = _TEXT_ROW.match(line)
            if not match:
                continue
            cells = {column: _clean(value) for column, value in _TEXT_CELL.findall(match.group(2)) if _clean(value)}
            if cells:
                current_rows.append(SpreadsheetRow(row=int(match.group(1)), cells=cells))
        flush()
    return tables


def table_schema_summaries(tables: list[SpreadsheetTable]) -> dict[str, list[str]]:
    """Return bounded, document-local hints for the document-analysis LLM."""
    summaries: dict[str, list[str]] = {}
    for table in tables:
        if not table.tableRole or table.tableSchemaConfidence <= 0:
            continue
        mapping = ", ".join(f"{role}={column}" for role, column in table.headerMap.items() if column) or "columns not mapped"
        summaries.setdefault(table.fileName, []).append(
            f"{table.sheet}: role={table.tableRole}; confidence={table.tableSchemaConfidence:.2f}; {mapping}"
        )
    return {file_name: lines[:24] for file_name, lines in summaries.items()}


def build_table_schema_profile(table: SpreadsheetTable) -> dict[str, Any]:
    """Compact a table into enough evidence to classify columns, not rows."""
    columns = _table_columns(table)
    header_values: dict[str, list[str]] = {column: [] for column in columns}
    header_rows = set(table.headerRows)
    for row in table.rows:
        if row.row not in header_rows:
            continue
        for column, value in row.cells.items():
            text = _clean(value)
            if text and text not in header_values.setdefault(column, []):
                header_values[column].append(text[:240])

    column_profiles: list[dict[str, Any]] = []
    for column in columns:
        values = [_clean(row.cells.get(column)) for row in table.rows]
        values = [value for value in values if value]
        count = len(values)
        examples = list(dict.fromkeys(value[:160] for value in values))[:4]
        numeric_count = sum(bool(_NUMBER_PATTERN.fullmatch(value.replace(" ", ""))) for value in values)
        unit_count = sum(bool(_UNIT_PATTERN.fullmatch(value.casefold().rstrip("."))) for value in values)
        code_count = sum(bool(_CODE_PATTERN.fullmatch(value.replace(" ", ""))) for value in values)
        label = " / ".join(header_values.get(column, [])) or _clean(
            table.headerLabels.get(column)
        )
        column_profiles.append(
            {
                "column": column,
                "header": label[:500],
                "nonEmpty": count,
                "numericRatio": round(numeric_count / count, 3) if count else 0,
                "unitRatio": round(unit_count / count, 3) if count else 0,
                "codeRatio": round(code_count / count, 3) if count else 0,
                "examples": examples,
            }
        )

    samples = [
        {"row": table.rows[index].row, "cells": table.rows[index].cells}
        for index in _sample_row_indexes(len(table.rows))
    ]
    return {
        "fileName": table.fileName,
        "sheet": table.sheet,
        "rowCount": len(table.rows),
        "headerRows": table.headerRows,
        "parserHeaderMap": table.headerMap,
        "columns": column_profiles,
        "sampleRows": samples,
    }


def apply_table_schema(
    table: SpreadsheetTable,
    schema: SpreadsheetTableSchemaResponse,
    *,
    min_confidence: float,
) -> tuple[SpreadsheetTable, str | None]:
    if schema.confidence < min_confidence:
        return table, f"confidence={schema.confidence:.2f} below threshold"
    classified = table.model_copy(
        update={"tableRole": schema.tableRole, "tableSchemaConfidence": schema.confidence}
    )
    if schema.tableRole in _BLOCKED_TABLE_ROLES:
        return classified, None

    columns = set(_table_columns(table))
    selected = {
        "product": schema.productColumn,
        "unit": schema.unitColumn,
        "quantity": schema.quantityColumn,
        "unit_price": schema.unitPriceColumn,
        "line_total": schema.lineTotalColumn,
    }
    selected = {role: column for role, column in selected.items() if column}
    if not selected.get("product"):
        return table, "schema has no product column"
    if any(column not in columns for column in selected.values()):
        return table, "schema references an unknown column"
    if selected.get("quantity") and selected["quantity"] in {
        selected.get("unit_price"),
        selected.get("line_total"),
    }:
        return table, "price column cannot be quantity column"

    labels = dict(table.headerLabels)
    for column in selected.values():
        if column not in labels:
            labels[column] = column
    return (
        classified.model_copy(
            update={
                "headerMap": selected,
                "headerLabels": labels,
            }
        ),
        None,
    )


def classify_spreadsheet_tables(
    llm: Any,
    tables: list[SpreadsheetTable],
    settings: Settings,
) -> tuple[list[SpreadsheetTable], list[str], dict[str, Any]]:
    """Classify compact table schemas before deterministic row extraction."""
    if not settings.table_schema_audit_enabled:
        return tables, [], {"enabled": False, "reviewed": 0, "applied": 0}

    warnings: list[str] = []
    result: list[SpreadsheetTable] = []
    debug: dict[str, Any] = {"enabled": True, "reviewed": 0, "applied": 0, "tables": []}
    limit = max(0, int(settings.table_schema_llm_max_tables))
    for index, table in enumerate(tables):
        if index >= limit or len(table.rows) < 2:
            result.append(table)
            continue
        debug["reviewed"] += 1
        try:
            schema = llm.classify_spreadsheet_table_schema(build_table_schema_profile(table))
            updated, skipped_reason = apply_table_schema(
                table,
                schema,
                min_confidence=float(settings.table_schema_min_confidence),
            )
        except Exception as exc:
            result.append(table)
            warnings.append(
                f"Table schema audit unavailable for {table.fileName}/{table.sheet}: "
                f"{type(exc).__name__}; retained parser headers."
            )
            continue
        result.append(updated)
        debug["tables"].append(
            {
                "fileName": table.fileName,
                "sheet": table.sheet,
                "role": schema.tableRole,
                "confidence": schema.confidence,
                "applied": skipped_reason is None,
                "reason": skipped_reason or schema.rationale[:300],
            }
        )
        if skipped_reason is None:
            debug["applied"] += 1
        else:
            warnings.append(
                f"Table schema audit did not override {table.fileName}/{table.sheet}: "
                f"{skipped_reason}."
            )
    return result, warnings, debug
