from __future__ import annotations

import re
from typing import Any

from app.models import TenderPosition, TenderPositionsResponse


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|упак\.?"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()


def parse_quantity(value: Any) -> float | None:
    match = re.search(r"\d+(?:[,.]\d+)?", str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else None


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
    seen: set[tuple[str, str, float]] = set()

    def add(name: str, unit: str, raw_quantity: Any, evidence: str) -> None:
        name, unit = _clean(name), _clean(unit)
        quantity = parse_quantity(raw_quantity)
        if quantity is None or re.search(r"наименование\s+товара|кол-?во", name, re.I):
            return
        key = (name.lower().replace("ё", "е"), unit.lower(), quantity)
        if key in seen:
            return
        seen.add(key)
        result.append(
            TenderPosition(
                product=name,
                productQuery=name,
                quantity=quantity,
                unit=unit,
                evidence=_clean(evidence)[:500],
                source="excel_table_deterministic",
            )
        )

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
    seen: set[tuple[str, float | None, str]] = set()
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
            continue
        seen.add(key)
        result.append(position.model_copy(update={"productQuery": query or product, "quantity": quantity, "unit": unit}))
        if len(result) >= 100:
            break
    return result, warnings
