from __future__ import annotations

import re
from typing import Any

from app.models import DocumentPriceSource, TenderPosition, TenderPositionsResponse


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|упак\.?"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()


def _missing(value: Any) -> bool:
    return value is None or value == ""


def parse_quantity(value: Any) -> float | None:
    match = re.search(r"\d+(?:[,.]\d+)?", str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else None


def _normalize_header(value: Any) -> str:
    text = _clean(value).lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9№]+", " ", text).strip()


def _header_role(value: Any) -> str | None:
    header = _normalize_header(value)
    if not header:
        return None
    if re.search(r"(?:общая\s+)?стоимость(?:\s+позиции)?|сумма|итого|всего", header):
        return "line_total"
    if re.search(
        r"цена(?:\s+(?:за|одной|1))?\s*(?:единиц[уы]?|ед\b)|"
        r"стоимость\s+(?:за\s+)?(?:единиц[уы]?|ед\b)|^цена(?:\s+руб(?:лей)?)?$",
        header,
    ):
        return "unit_price"
    if re.search(r"количество|кол во|кол\b", header):
        return "quantity"
    if re.search(r"единица\s+измерения|ед\s+изм", header):
        return "unit"
    if re.search(r"наименование|название\s+(?:товара|продукции)|^товар$|предмет\s+закупки", header):
        return "product"
    return None


def _parse_structured_cells(value: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for raw_part in value.split("|"):
        match = re.match(r"^\s*([A-Z]{1,3}):\s*(.*?)\s*$", raw_part)
        if match and match.group(2):
            cells[match.group(1)] = match.group(2)
    return cells


def _currency_from_price_cells(*values: Any) -> str | None:
    text = " ".join(str(value or "") for value in values).lower()
    if "₽" in text or re.search(r"\bруб(?:\.|лей|ля)?\b", text):
        return "RUB"
    return None


def extract_deterministic_positions(text: str) -> list[TenderPosition]:
    normalized = _clean(text)
    patterns = [
        re.compile(
            rf"(?:^|\s)(\d{{1,4}})\s+([А-ЯA-ZЁ][А-ЯA-Zа-яa-zёЁ0-9\s\-–—\"«»().,/ ]{{2,120}}?)\s+({UNITS})\s+(\d+(?:[,.]\d+)?)(?=\s|$)",
            re.I,
        ),
        re.compile(
            rf"наименование\s+товара[^:]*:\s*([^|\n]{{2,140}})[|\s]+ед\.?\s*изм\.?[^:]*:\s*([^|\n]{{1,40}})[|\s]+(?:кол-?во|количество)[^:]*:\s*(\d+(?:[,.]\d+)?)",
            re.I,
        ),
    ]
    result: list[TenderPosition] = []
    seen: dict[tuple[str, str, float], int] = {}

    def add(
        name: str,
        unit: str,
        raw_quantity: Any,
        evidence: str,
        *,
        document_unit_price: Any = None,
        document_line_total: Any = None,
        document_currency: str | None = None,
        document_price_source: DocumentPriceSource | None = None,
    ) -> None:
        name, unit = _clean(name), _clean(unit)
        quantity = parse_quantity(raw_quantity)
        if quantity is None or re.search(r"наименование\s+товара|кол-?во", name, re.I):
            return
        key = (name.lower().replace("ё", "е"), unit.lower(), quantity)
        if key in seen:
            current = result[seen[key]]
            updates: dict[str, Any] = {}
            for field, value in (
                ("documentUnitPriceRub", document_unit_price),
                ("documentLineTotalRub", document_line_total),
                ("documentCurrency", document_currency),
                ("documentPriceSource", document_price_source),
            ):
                if _missing(getattr(current, field)) and not _missing(value):
                    updates[field] = value
            if updates:
                updates["documentPriceEvidence"] = _clean(evidence)[:500]
                result[seen[key]] = current.model_copy(update=updates)
            return
        seen[key] = len(result)
        result.append(
            TenderPosition(
                product=name,
                productQuery=name,
                quantity=quantity,
                unit=unit,
                evidence=_clean(evidence)[:500],
                source="excel_table_deterministic",
                documentUnitPriceRub=document_unit_price,
                documentLineTotalRub=document_line_total,
                documentCurrency=document_currency,
                documentPriceEvidence=(
                    _clean(evidence)[:500]
                    if document_unit_price not in {None, ""} or document_line_total not in {None, ""}
                    else ""
                ),
                documentPriceSource=document_price_source,
            )
        )

    # Header-aware extraction for text emitted by the XLS/XLSX/CSV parser.
    # Column addresses make this deterministic even when cells between columns are empty.
    current_file = ""
    current_sheet = ""
    header_columns: dict[str, str] = {}
    header_labels: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("--- ДОКУМЕНТ "):
            current_file = ""
            current_sheet = ""
            header_columns = {}
            header_labels = {}
            continue
        if line.lower().startswith("filename:"):
            current_file = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Лист:"):
            current_sheet = line.split(":", 1)[1].strip()
            header_columns = {}
            header_labels = {}
            continue
        row_match = re.match(r"^Строка\s+(\d+)\s*:\s*(.+)$", line, re.I)
        if not row_match:
            continue
        row_number = int(row_match.group(1))
        cells = _parse_structured_cells(row_match.group(2))
        if not cells:
            continue
        detected_headers = {
            role: (column, value)
            for column, value in cells.items()
            if (role := _header_role(value)) is not None
        }
        if detected_headers:
            for role, (column, label) in detected_headers.items():
                header_columns[role] = column
                header_labels[role] = label
            continue
        if not {"product", "unit", "quantity"}.issubset(header_columns):
            continue
        name = cells.get(header_columns["product"], "")
        unit = cells.get(header_columns["unit"], "")
        raw_quantity = cells.get(header_columns["quantity"])
        if not name or not unit or raw_quantity is None:
            continue
        unit_price_column = header_columns.get("unit_price", "")
        line_total_column = header_columns.get("line_total", "")
        raw_unit_price = cells.get(unit_price_column) if unit_price_column else None
        raw_line_total = cells.get(line_total_column) if line_total_column else None
        has_document_price = raw_unit_price not in {None, ""} or raw_line_total not in {None, ""}
        price_source = (
            DocumentPriceSource(
                fileName=current_file,
                sheet=current_sheet,
                row=row_number,
                unitPriceColumn=unit_price_column,
                lineTotalColumn=line_total_column,
                unitPriceHeader=header_labels.get("unit_price", ""),
                lineTotalHeader=header_labels.get("line_total", ""),
                extractionMethod="excel_deterministic",
            )
            if has_document_price
            else None
        )
        add(
            name,
            unit,
            raw_quantity,
            line,
            document_unit_price=raw_unit_price,
            document_line_total=raw_line_total,
            document_currency=_currency_from_price_cells(
                header_labels.get("unit_price"),
                header_labels.get("line_total"),
                raw_unit_price,
                raw_line_total,
            ),
            document_price_source=price_source,
        )
        if len(result) >= 100:
            return result

    # Structured row emitted by the spreadsheet parser:
    # "Строка 2: A: 1 | B: Кабель | D: шт | E: 10".
    for row_match in re.finditer(r"^Строка\s+\d+\s*:\s*(.+)$", text, re.I | re.M):
        parts = []
        for raw_part in row_match.group(1).split("|"):
            part = raw_part.strip()
            # Remove the Excel address but keep colons inside the cell value.
            part = re.sub(r"^[A-Z]{1,3}:\s*", "", part)
            if part:
                parts.append(part)
        for index, part in enumerate(parts):
            if not re.fullmatch(UNITS, part, re.I):
                continue
            if index + 1 >= len(parts):
                continue
            quantity = parse_quantity(parts[index + 1])
            if quantity is None:
                continue
            name = next(
                (
                    candidate
                    for candidate in reversed(parts[:index])
                    if not re.fullmatch(r"\d{1,4}", candidate)
                    and not re.search(r"наименование|ед\.?\s*изм|кол-?во|количество", candidate, re.I)
                ),
                "",
            )
            if name:
                add(name, part, quantity, row_match.group(0))
            if len(result) >= 100:
                return result

    for pattern_index, pattern in enumerate(patterns):
        for match in pattern.finditer(normalized if pattern_index == 0 else text):
            if pattern_index == 0:
                name, unit, raw_quantity = match.group(2), match.group(3), match.group(4)
            else:
                name, unit, raw_quantity = match.group(1), match.group(2), match.group(3)
            add(name, unit, raw_quantity, match.group(0))
            if len(result) >= 100:
                return result
    return result


def merge_positions(
    deterministic: list[TenderPosition], llm_response: TenderPositionsResponse | None
) -> tuple[list[TenderPosition], list[str]]:
    combined = list(llm_response.products if llm_response else []) + deterministic
    warnings = list(llm_response.warnings if llm_response else [])
    by_name = {position.productQuery.lower(): position for position in deterministic if position.productQuery}
    result: list[TenderPosition] = []
    seen: dict[tuple[str, float | None, str], int] = {}
    for position in combined:
        query = _clean(position.productQuery or position.product)
        match = by_name.get(query.lower())
        quantity = position.quantity if position.quantity is not None else (match.quantity if match else None)
        unit = position.unit or (match.unit if match else "")
        product = _clean(position.product)
        if not product:
            continue
        key = (product.lower().replace("ё", "е"), quantity, unit.lower())
        if key in seen:
            existing_index = seen[key]
            existing = result[existing_index]
            updates: dict[str, Any] = {}
            for field in (
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
            ):
                existing_value = getattr(existing, field)
                candidate_value = getattr(position, field)
                if _missing(existing_value) and not _missing(candidate_value):
                    updates[field] = candidate_value
            if updates:
                result[existing_index] = existing.model_copy(update=updates)
            continue
        seen[key] = len(result)
        update = {"productQuery": query or product, "quantity": quantity, "unit": unit}
        if match:
            for field in (
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
            ):
                if _missing(getattr(position, field)) and not _missing(getattr(match, field)):
                    update[field] = getattr(match, field)
        result.append(position.model_copy(update=update))
        if len(result) >= 100:
            break
    return result, warnings
