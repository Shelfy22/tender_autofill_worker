from __future__ import annotations

import json
import re
from typing import Any

from app.models import DocumentPriceSource, TenderPosition, TenderPositionsResponse


_ONLY_ROW_NUMBER_PATTERN = re.compile(r"^\s*\d{1,4}\s*[.)-]?\s*$")
_ONLY_CLASSIFIER_CODE_PATTERN = re.compile(
    r"^\s*\d{2}(?:[.\s-]\d{1,3}){2,}(?:\s*[.)-]?)?\s*$"
)
_ONLY_AUXILIARY_CODE_PATTERN = re.compile(
    r"^\s*(?:ол|ol)\s*[-–—]?\s*\d{1,4}\s*$",
    re.IGNORECASE,
)
_EXAMPLE_POSITION_PATTERN = re.compile(
    r"^\s*(?:пример(?:\s+заполнения)?|example)\s*[.:;–—-]?\s*$",
    re.IGNORECASE,
)
_SERVICE_POSITION_PATTERN = re.compile(
    r"^\s*(?:национальн[а-яё]*\s+режим|"
    r"(?:ограничени[ея]|запрет[а-яё]*)\s+(?:не\s+)?(?:установлен[а-яё]*|предоставля[а-яё]*)|"
    r"товар\s+(?:не\s+)?(?:отсутствует|включ[её]н)\s+в\s+реестр|"
    r"код\s+(?:окпд|окпд2|ктру|тн\s+вэд)|"
    r"единиц[аы]\s+измерени[яй]|количеств[оа]|итого|всего)\b",
    re.IGNORECASE,
)
_ADDRESS_OR_RECIPIENT_PATTERN = re.compile(
    r"(?:\b(?:место|адрес)\s+(?:поставки|доставки)\b|"
    r"\b(?:грузополучатель|получатель)\b|"
    r"\b\d{6}\b.{0,160}(?:\bг\.|\bгород\b|\bул\.|\bулица\b|\bд\.|\bдом\b)|"
    r"\b(?:филиал|предприятие)\b.{0,160}(?:\bг\.|\bгород\b|\bул\.|\bулица\b))",
    re.IGNORECASE,
)
_PRODUCT_DESCRIPTION_SEPARATOR_PATTERN = re.compile(
    r"^(.{3,300}?):\s*((?:назначение|технические\s+характеристики|"
    r"характеристики|описание)\s*:?.*)$",
    re.IGNORECASE | re.DOTALL,
)
_TENDER_LEVEL_PRICE_PATTERN = re.compile(
    r"\b(?:начальн[а-яё]*\s+(?:максимальн[а-яё]*\s+)?цен[аы]|нмцк?|нмц)\b",
    re.IGNORECASE,
)
_POSITION_PRICE_PATTERN = re.compile(
    r"\b(?:цен[аы]\s+(?:за\s+)?(?:единиц[уы]|1\s*(?:шт|ед))|"
    r"стоимост[ьи]\s+(?:единиц[уы]|позиц[а-яё]*|строк[а-яё]*)|"
    r"сумм[аы]\s+(?:строк[а-яё]*|позиц[а-яё]*))\b",
    re.IGNORECASE,
)


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|упак\.?"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _is_noise_position(position: TenderPosition) -> bool:
    product = _clean(position.product)
    return bool(
        not product
        or _ONLY_ROW_NUMBER_PATTERN.fullmatch(product)
        or _ONLY_CLASSIFIER_CODE_PATTERN.fullmatch(product)
        or _ONLY_AUXILIARY_CODE_PATTERN.fullmatch(product)
        or _EXAMPLE_POSITION_PATTERN.fullmatch(product)
        or _SERVICE_POSITION_PATTERN.search(product)
        or _ADDRESS_OR_RECIPIENT_PATTERN.search(product)
    )


def _normalize_product_description(position: TenderPosition) -> TenderPosition:
    """Keep the product title as identity and move an inline specification to requirements."""
    product = _clean(position.product)
    match = _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN.match(product)
    if not match:
        return position
    title = _clean(match.group(1))
    description = _clean(match.group(2))
    if not title or not description:
        return position
    requirements = _clean(" ".join(filter(None, (position.requirements, description))))
    query = _clean(position.productQuery)
    return position.model_copy(
        update={
            "product": title,
            "productQuery": title if not query or query == product else query,
            "requirements": requirements,
        }
    )


def _clear_tender_level_price(position: TenderPosition) -> TenderPosition:
    """Do not treat NMCK/initial tender price as a product-position price."""
    evidence = _clean(position.documentPriceEvidence or position.evidence)
    if (
        position.documentPriceSource is None
        and _TENDER_LEVEL_PRICE_PATTERN.search(evidence)
        and not _POSITION_PRICE_PATTERN.search(evidence)
    ):
        return position.model_copy(
            update={
                "documentUnitPriceRub": None,
                "documentLineTotalRub": None,
                "documentCurrency": None,
                "documentPriceEvidence": "",
            }
        )
    return position


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


def _excel_column_number(value: str) -> int:
    number = 0
    for character in value.upper():
        if not "A" <= character <= "Z":
            return 0
        number = number * 26 + ord(character) - ord("A") + 1
    return number


def _plain_number(value: Any) -> float | None:
    text = str(value or "").strip().replace("\xa0", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[,.]\d+)?", text):
        return None
    return float(text.replace(",", "."))


def _quantity_from_structured_evidence(position: TenderPosition) -> float | None:
    """Recover quantity from the spreadsheet cell immediately after the unit cell."""
    expected_unit = _clean(position.unit).casefold().replace("ё", "е")
    for evidence in (position.evidence, position.documentPriceEvidence):
        for row in str(evidence or "").splitlines():
            row_match = re.search(r"(?:^|\b)Строка\s+\d+\s*:\s*(.+)$", row, re.IGNORECASE)
            if not row_match:
                continue
            cells = _parse_structured_cells(row_match.group(1))
            ordered = sorted(cells.items(), key=lambda item: _excel_column_number(item[0]))
            for index, (_, cell_value) in enumerate(ordered):
                normalized_cell = _clean(cell_value).casefold().replace("ё", "е")
                is_unit = bool(re.fullmatch(UNITS, cell_value, re.IGNORECASE))
                if expected_unit and normalized_cell == expected_unit:
                    is_unit = True
                if not is_unit or index + 1 >= len(ordered):
                    continue
                current_column = _excel_column_number(ordered[index][0])
                next_column = _excel_column_number(ordered[index + 1][0])
                if next_column != current_column + 1:
                    continue
                quantity = _plain_number(ordered[index + 1][1])
                if quantity is not None:
                    return quantity
    return None


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


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _nested_text(value: Any, *keys: str) -> str:
    if isinstance(value, dict):
        return _clean(_first_value(value, *keys))
    return _clean(value)


def extract_seldon_positions(purchase: dict[str, Any]) -> list[TenderPosition]:
    """Extract product rows from the structured purchase without relying on an LLM."""
    lots = _first_value(purchase, "lotsList", "lots", "lotList") or []
    if isinstance(lots, dict):
        lots = [lots]
    if not isinstance(lots, list):
        lots = []

    containers: list[dict[str, Any]] = [purchase]
    containers.extend(lot for lot in lots if isinstance(lot, dict))
    result: list[TenderPosition] = []
    seen: set[tuple[str, float | None, str]] = set()

    for container in containers:
        products = _first_value(
            container,
            "productsList",
            "products",
            "productList",
            "positions",
            "items",
        ) or []
        if isinstance(products, dict):
            products = [products]
        if not isinstance(products, list):
            continue

        for raw_product in products:
            if not isinstance(raw_product, dict):
                continue
            nested_product = raw_product.get("product")
            source = (
                {**nested_product, **raw_product}
                if isinstance(nested_product, dict)
                else raw_product
            )
            name = _clean(
                _first_value(
                    source,
                    "name",
                    "productName",
                    "positionName",
                    "subject",
                    "title",
                    "fullName",
                )
            )
            if not name and isinstance(nested_product, str):
                name = _clean(nested_product)
            if not name:
                continue

            quantity = parse_quantity(
                _first_value(
                    source,
                    "quantity",
                    "amount",
                    "count",
                    "qty",
                    "volume",
                    "productQuantity",
                )
            )
            unit = _nested_text(
                _first_value(
                    source,
                    "unit",
                    "okei",
                    "measureUnit",
                    "unitName",
                    "measure",
                ),
                "name",
                "shortName",
                "symbol",
                "code",
            )
            requirements = _clean(
                _first_value(
                    source,
                    "requirements",
                    "characteristics",
                    "specification",
                    "description",
                )
            )
            key = (
                re.sub(r"[^a-zа-я0-9]+", " ", name.lower().replace("ё", "е")).strip(),
                quantity,
                unit.lower(),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            evidence = json.dumps(raw_product, ensure_ascii=False, default=str)[:500]
            result.append(
                TenderPosition(
                    product=name,
                    productQuery=name,
                    quantity=quantity,
                    unit=unit,
                    evidence=evidence,
                    requirements=requirements,
                    source="seldon_structured",
                )
            )
            if len(result) >= 100:
                return result
    return result


def _position_name_key(position: TenderPosition) -> str:
    value = _clean(position.productQuery or position.product).lower().replace("ё", "е")
    description_match = _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN.match(value)
    if description_match:
        value = _clean(description_match.group(1))
    return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()


def merge_positions(
    deterministic: list[TenderPosition],
    llm_response: TenderPositionsResponse | None,
    seldon: list[TenderPosition] | None = None,
) -> tuple[list[TenderPosition], list[str]]:
    seldon = list(seldon or [])
    combined = list(llm_response.products if llm_response else []) + seldon + deterministic
    warnings = list(llm_response.warnings if llm_response else [])
    seldon_by_name = {_position_name_key(position): position for position in seldon}
    excel_by_name = {_position_name_key(position): position for position in deterministic}
    if seldon and not any(position.quantity is not None for position in seldon):
        warnings.append(
            "Товарные позиции найдены в структурированных данных Seldon, но количество в них отсутствует."
        )
    result: list[TenderPosition] = []
    seen: dict[tuple[str, float | None, str], int] = {}
    for raw_position in combined:
        position = _normalize_product_description(_clear_tender_level_price(raw_position))
        if _is_noise_position(position):
            product_text = _clean(position.product)
            if _ADDRESS_OR_RECIPIENT_PATTERN.search(product_text):
                warnings.append(
                    "Пропущен адрес/получатель, ошибочно извлечённый как товар: "
                    f"{product_text[:200]}"
                )
            else:
                warnings.append(
                    "Пропущена служебная строка, ошибочно извлечённая как товар: "
                    f"{product_text[:200]}"
                )
            continue
        structured_quantity = _quantity_from_structured_evidence(position)
        if structured_quantity is not None and position.quantity != structured_quantity:
            warnings.append(
                "Количество позиции исправлено по соседним ячейкам Excel: "
                f"{_clean(position.product)[:200]} — {structured_quantity:g}."
            )
            position = position.model_copy(update={"quantity": structured_quantity})
        query = _clean(position.productQuery or position.product)
        name_key = _position_name_key(position)
        seldon_match = seldon_by_name.get(name_key)
        excel_match = excel_by_name.get(name_key)
        quantity = (
            seldon_match.quantity
            if seldon_match is not None and seldon_match.quantity is not None
            else excel_match.quantity
            if excel_match is not None and excel_match.quantity is not None
            else position.quantity
        )
        unit = (
            seldon_match.unit
            if seldon_match is not None and seldon_match.unit
            else excel_match.unit
            if excel_match is not None and excel_match.unit
            else position.unit
        )
        product = _clean(position.product)
        if not product:
            continue
        resolved_key = (name_key, quantity, unit.lower())
        if resolved_key in seen:
            existing_index = seen[resolved_key]
            existing = result[existing_index]
            updates: dict[str, Any] = {}
            if existing.quantity is None and quantity is not None:
                updates["quantity"] = quantity
            if not existing.unit and unit:
                updates["unit"] = unit
            for field in (
                "requirements",
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
            candidate_evidence = _clean(position.evidence)
            existing_evidence = _clean(existing.evidence)
            if candidate_evidence and candidate_evidence not in existing_evidence:
                updates["evidence"] = _clean(
                    " | ".join(filter(None, (existing_evidence, candidate_evidence)))
                )[:1200]
            if updates:
                result[existing_index] = existing.model_copy(update=updates)
            if candidate_evidence or existing_evidence:
                warnings.append(
                    "Объединена повторно извлечённая товарная позиция из нескольких документов: "
                    f"{product[:200]}"
                )
            continue
        seen[resolved_key] = len(result)
        update = {"productQuery": query or product, "quantity": quantity, "unit": unit}
        if excel_match:
            for field in (
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
            ):
                excel_value = getattr(excel_match, field)
                if not _missing(excel_value):
                    update[field] = excel_value
        result.append(position.model_copy(update=update))
        if len(result) >= 100:
            break
    return result, warnings
