from __future__ import annotations

import json
import re
import time
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
_OBVIOUS_NON_ITEM_TABLE_MARKERS = (
    "график постав",
    "календарный план",
    "форма заявки",
    "форма предложения",
    "реквизиты",
    "сведения об участнике",
    "сведения о поставщике",
)
_ITEM_TABLE_MARKERS = (
    "наименован",
    "товар",
    "продукц",
    "материал",
    "оборудован",
    "номенклатур",
    "техническ",
)


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


def _clip(value: Any, limit: int) -> str:
    text = _clean(value)
    return text if len(text) <= limit else f"{text[: max(1, limit - 3)]}..."


def _compact_sample_cells(cells: dict[str, str], *, cell_limit: int, row_limit: int) -> dict[str, str]:
    compact: dict[str, str] = {}
    used = 0
    for column in sorted(cells, key=_column_order):
        remaining = row_limit - used
        if remaining <= 0:
            break
        value = _clip(cells[column], min(cell_limit, remaining))
        if not value:
            continue
        compact[column] = value
        used += len(column) + len(value)
    return compact


def _shrink_profile(profile: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """Keep structural coverage while bounding the LLM request deterministically."""
    def size() -> int:
        return len(json.dumps(profile, ensure_ascii=False, separators=(",", ":")))

    # Remove middle samples first: the first/last rows are the most useful for
    # headers, totals and delivery schedules.
    while len(profile["sampleRows"]) > 4 and size() > max_chars:
        del profile["sampleRows"][len(profile["sampleRows"]) // 2]
    if size() <= max_chars:
        return profile
    for column in profile["columns"]:
        column["header"] = _clip(column.get("header"), 160)
        column["examples"] = list(column.get("examples") or [])[:1]
    if size() <= max_chars:
        return profile
    for column in profile["columns"]:
        column["examples"] = []
    return profile


def build_table_schema_profile(
    table: SpreadsheetTable,
    *,
    max_cell_chars: int = 320,
    max_chars: int = 20_000,
) -> dict[str, Any]:
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
        examples = list(dict.fromkeys(_clip(value, min(160, max_cell_chars)) for value in values))[:4]
        numeric_count = sum(bool(_NUMBER_PATTERN.fullmatch(value.replace(" ", ""))) for value in values)
        unit_count = sum(bool(_UNIT_PATTERN.fullmatch(value.casefold().rstrip("."))) for value in values)
        code_count = sum(bool(_CODE_PATTERN.fullmatch(value.replace(" ", ""))) for value in values)
        label = " / ".join(header_values.get(column, [])) or _clean(
            table.headerLabels.get(column)
        )
        column_profiles.append(
            {
                "column": column,
                "header": _clip(label, min(500, max_cell_chars)),
                "nonEmpty": count,
                "numericRatio": round(numeric_count / count, 3) if count else 0,
                "unitRatio": round(unit_count / count, 3) if count else 0,
                "codeRatio": round(code_count / count, 3) if count else 0,
                "examples": examples,
            }
        )

    samples = [
        {
            "row": table.rows[index].row,
            "cells": _compact_sample_cells(
                table.rows[index].cells,
                cell_limit=max_cell_chars,
                row_limit=max(800, max_cell_chars * 4),
            ),
        }
        for index in _sample_row_indexes(len(table.rows))
    ]
    profile = {
        "fileName": table.fileName,
        "sheet": table.sheet,
        "rowCount": len(table.rows),
        "headerRows": table.headerRows,
        "parserHeaderMap": table.headerMap,
        "columns": column_profiles,
        "sampleRows": samples,
    }
    return _shrink_profile(profile, max_chars=max_chars)


def _schema_table_text(table: SpreadsheetTable) -> str:
    header_text = " ".join(table.headerLabels.values())
    first_rows = " ".join(
        " ".join(row.cells.values()) for row in table.rows[:2]
    )
    return _clean(f"{table.fileName} {table.sheet} {header_text} {first_rows}").casefold()


def _is_obvious_non_item_table(table: SpreadsheetTable) -> bool:
    text = _schema_table_text(table)
    return any(marker in text for marker in _OBVIOUS_NON_ITEM_TABLE_MARKERS)


def _schema_priority(table: SpreadsheetTable) -> int:
    text = _schema_table_text(table)
    return sum(marker in text for marker in _ITEM_TABLE_MARKERS) * 10 + min(len(table.rows), 9)


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
    result = list(tables)
    debug: dict[str, Any] = {
        "enabled": True,
        "reviewed": 0,
        "applied": 0,
        "skippedDeterministically": 0,
        "budgetExceeded": False,
        "tables": [],
    }
    limit = max(0, int(settings.table_schema_llm_max_tables))
    stage_budget = float(getattr(settings, "table_schema_audit_max_seconds", 240) or 0)
    per_table_timeout = float(getattr(settings, "table_schema_timeout_seconds", 75) or 75)
    per_table_attempts = max(1, int(getattr(settings, "table_schema_llm_max_attempts", 1) or 1))
    max_cell_chars = int(getattr(settings, "table_schema_profile_max_cell_chars", 320) or 320)
    max_profile_chars = int(getattr(settings, "table_schema_profile_max_chars", 20_000) or 20_000)
    started = time.monotonic()
    reviewed = 0
    candidate_indexes = sorted(
        range(len(tables)),
        key=lambda index: _schema_priority(tables[index]),
        reverse=True,
    )
    for index in candidate_indexes:
        table = tables[index]
        if reviewed >= limit or len(table.rows) < 2:
            continue
        if _is_obvious_non_item_table(table):
            debug["skippedDeterministically"] += 1
            result[index] = table.model_copy(update={"tableRole": "other", "tableSchemaConfidence": 1.0})
            continue
        elapsed = time.monotonic() - started
        if stage_budget and elapsed + per_table_timeout * per_table_attempts > stage_budget:
            debug["budgetExceeded"] = True
            warnings.append(
                "Table schema audit budget exhausted; retained parser headers for remaining tables."
            )
            break
        debug["reviewed"] += 1
        reviewed += 1
        try:
            schema = llm.classify_spreadsheet_table_schema(
                build_table_schema_profile(
                    table,
                    max_cell_chars=max_cell_chars,
                    max_chars=max_profile_chars,
                )
            )
            updated, skipped_reason = apply_table_schema(
                table,
                schema,
                min_confidence=float(settings.table_schema_min_confidence),
            )
        except Exception as exc:
            warnings.append(
                f"Table schema audit unavailable for {table.fileName}/{table.sheet}: "
                f"{type(exc).__name__}; retained parser headers."
            )
            continue
        result[index] = updated
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
