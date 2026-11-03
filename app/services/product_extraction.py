from __future__ import annotations

from app.value_normalization import first_value as _first_value
from billiard.exceptions import SoftTimeLimitExceeded
import json
import re
import unicodedata
from typing import Any
from app.models import DocumentPriceSource, ProductCharacteristic, ProductSourceReference, SpreadsheetRow, SpreadsheetTable, TenderPosition
from app.services.document_roles import COMPOSITE_ROLE, OTHER_ROLE, PRICE_ROLE, classify_document_role, detect_section_role

from app.services.product_common import (
    UNITS,
    _CLASSIFIER_SUFFIX_PATTERN,
    _DELIVERY_SCHEDULE_COLUMN_PATTERN,
    _DELIVERY_SCHEDULE_HEADING_PATTERN,
    _EQUIVALENT_SUFFIX_PATTERN,
    _ONLY_CLASSIFIER_CODE_PATTERN,
    _ONLY_ROW_NUMBER_PATTERN,
    _STRUCTURED_TABLE_MARKER_PATTERN,
    _TABLE_IDENTITY_EXCLUDED_HEADER_PATTERN,
    _TABLE_IDENTITY_HEADER_PATTERN,
    _WORD_TABLE_ROW_PATTERN,
    _clean,
    _excel_column_number,
    _merge_replicated_position_details,
    _normalize_header,
    _parse_structured_cells,
    _position_name_key,
    _word_table_key,
)

def _strip_classifier_suffix(value: str) -> str:
    return _clean(_CLASSIFIER_SUFFIX_PATTERN.sub("", value))

def _normalize_extracted_product_name(value: str) -> str:
    value = _strip_classifier_suffix(_clean(value))
    return _clean(_EQUIVALENT_SUFFIX_PATTERN.sub("", value))

def _compose_table_product_identity(
    name: Any,
    cells: dict[str, str],
    column_labels: dict[str, str],
    product_column: str,
) -> str:
    """Keep a model/mark from the same row as part of a generic product title."""
    product = _clean(name)
    if not product:
        return ""
    product_key = _word_table_key(product)
    identity_values: list[str] = []
    for column, value in sorted(
        cells.items(), key=lambda item: _excel_column_number(item[0])
    ):
        if column == product_column:
            continue
        label = _clean(column_labels.get(column))
        candidate = _clean(value).strip(" ;,.")
        if (
            not candidate
            or not label
            or not _TABLE_IDENTITY_HEADER_PATTERN.search(label)
            or _TABLE_IDENTITY_EXCLUDED_HEADER_PATTERN.search(label)
        ):
            continue
        candidate_key = _word_table_key(candidate)
        if not candidate_key or candidate_key in product_key:
            continue
        identity_values.append(candidate)
        product_key = f"{product_key} {candidate_key}".strip()
    return _clean(" ".join((product, *dict.fromkeys(identity_values))))

def parse_quantity(value: Any) -> float | None:
    match = re.search(r"\d+(?:[,.]\d+)?", str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else None

def _header_role(value: Any) -> str | None:
    header = _normalize_header(value)
    if not header:
        return None
    if re.search(
        r"наименование\s+(?:страны\s+происхождения|участника\s+закупки|"
        r"заказчика|поставщика|производителя)",
        header,
    ):
        return None
    if header == "\u043e\u0431\u044a\u0435\u043a\u0442 \u0437\u0430\u043a\u0443\u043f\u043a\u0438":
        return "product"
    # A price column frequently contains a suffix such as "руб./ед. изм.".
    # Treat a cell as the unit column only when that is its own header, otherwise
    # a secondary price-table header can overwrite the actual unit column.
    unit_header = r"(?:\u0435\u0434\u0438\u043d\u0438\u0446\u0430\s+\u0438\u0437\u043c\u0435\u0440\u0435\u043d\u0438\u044f|\u0435\u0434\s+\u0438\u0437\u043c)(?:\s+(?:\u0442\u043e\u0432\u0430\u0440\u0430|\u043f\u0440\u043e\u0434\u0443\u043a\u0446\u0438\u0438|\u0438\u0437\u0434\u0435\u043b\u0438\u044f))?"
    if re.search(r"\u0435\u0434\s+\u0438\u0437\u043c", header) and not re.fullmatch(
        unit_header,
        header,
    ):
        return None
    characteristic_only = bool(
        re.search(r"показател|параметр|характеристик", header)
        and not re.search(r"товар|продукц|оборудован|материал|издели|мтр", header)
        and not re.fullmatch(
            r"наименование\s+и\s+(?:технические\s+)?характеристики",
            header,
        )
    )
    if characteristic_only:
        return None
    if re.search(r"(?:общая\s+)?стоимость(?:\s+позиции)?|сумма|итого|всего", header):
        return "line_total"
    if re.search(
        r"цена(?:\s+(?:за|одной|1))?\s*(?:единиц[уы]?|ед\b)|"
        r"стоимость\s+(?:за\s+)?(?:единиц[уы]?|ед\b)|^цена(?:\s+руб(?:лей)?)?$",
        header,
    ):
        return "unit_price"
    if re.fullmatch(
        r"цена(?:\s+руб(?:л(?:ей)?)?)?(?:\s+(?:без|с)\s+ндс)?",
        header,
    ):
        return "unit_price"
    if re.fullmatch(
        r"(?:общее\s+)?(?:количество|кол\s+во|кол)"
        r"(?:\s+(?:товара|продукции|изделий|единиц))?",
        header,
    ):
        return "quantity"
    if re.search(r"(?:количество|кол\s+во|кол)\s+(?:шт|штук|ед|м|кг|л)\b", header):
        return "quantity"
    if re.search(r"единица\s+измерения|ед\s+изм", header) or re.fullmatch(
        r"(?:базисная\s+)?е\s*и",
        header,
    ):
        return "unit"
    if re.search(
        r"^(?:наименование|название)(?:\s|$)|^товар$|^предмет\s+закупки$|^(?:краткий\s+текст\s+)?материала?$",
        header,
    ):
        return "product"
    return None

def _is_delivery_schedule_header(cells: dict[str, str]) -> bool:
    roles = {_header_role(value) for value in cells.values()}
    return bool(
        {"product", "quantity"}.issubset(roles)
        and any(
            _DELIVERY_SCHEDULE_COLUMN_PATTERN.search(_clean(value))
            for value in cells.values()
        )
    )

def _unit_from_quantity_header(value: Any) -> str:
    header = _normalize_header(value)
    if not header:
        return ""
    unit_aliases = (
        ("шт", r"шт|штук|штука|штуки"),
        ("ед", r"ед|единиц[а-я]*"),
        ("комплект", r"комплект[а-я]*"),
        ("м", r"м|метр[а-я]*"),
        ("кг", r"кг|килограмм[а-я]*"),
        ("л", r"л|литр[а-я]*"),
    )
    for unit, pattern in unit_aliases:
        if re.search(rf"(?:^|\s)(?:{pattern})(?:\s|$)", header):
            return unit
    return ""

def _unit_from_quantity_value(value: Any) -> str:
    """Read a unit where a quantity cell contains both value and unit."""
    match = re.fullmatch(
        rf"\s*[+-]?\d+(?:[.,]\d+)?\s*({UNITS})\s*",
        _clean(value),
        re.IGNORECASE,
    )
    return _clean(match.group(1)).rstrip(".") if match else ""

def _table_unit(
    cells: dict[str, str],
    header_columns: dict[str, str],
    header_labels: dict[str, str],
) -> tuple[str, str]:
    unit_column = header_columns.get("unit", "")
    unit = cells.get(unit_column, "") if unit_column else ""
    if not unit:
        unit = _unit_from_quantity_header(header_labels.get("quantity", ""))
    return unit, unit_column

def _header_candidates(cells: dict[str, str]) -> list[tuple[str, str, str]]:
    return [
        (role, column, value)
        for column, value in cells.items()
        if (role := _header_role(value)) is not None
    ]

def _is_parameter_value_cell(value: str) -> bool:
    """Return whether a cell looks like a technical-specification entry."""
    text = _clean(value)
    if not text or ":" not in text:
        return False
    name, raw_value = text.split(":", 1)
    return bool(
        re.search(r"[A-Za-z\u0410-\u044f]", name)
        and raw_value.strip()
        and len(name) <= 120
    )

def _header_data_score(
    role: str,
    column: str,
    row_index: int,
    rows: list[SpreadsheetRow],
) -> float:
    """Score a header candidate by whether the following cells fit its role."""
    values = [
        _clean(row.cells.get(column))
        for row in rows[row_index + 1 : row_index + 51]
        if _clean(row.cells.get(column))
    ]
    if not values:
        return 0.0
    if role == "unit":
        matches = sum(bool(re.fullmatch(UNITS, value, re.IGNORECASE)) for value in values)
    elif role == "product":
        # A column headed as a product can still contain technical characteristics.
        if sum(_is_parameter_value_cell(value) for value in values) / len(values) >= 0.6:
            return 0.0
        matches = sum(
            bool(re.search(r"[A-Za-z\u0410-\u044f]", value))
            and _header_role(value) is None
            for value in values
        )
    else:
        matches = sum(
            bool(re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", value.replace(" ", "")))
            for value in values
        )
    return matches / len(values)

def infer_spreadsheet_headers(
    rows: list[SpreadsheetRow],
) -> tuple[list[int], dict[str, str], dict[str, str]]:
    header_rows: list[int] = []
    candidates: dict[str, list[tuple[float, int, str, str]]] = {}
    for row_index, row in enumerate(rows):
        detected_headers = _header_candidates(row.cells)
        core_header_roles = {role for role, _, _ in detected_headers} & {
            "product",
            "unit",
            "quantity",
            "unit_price",
            "line_total",
        }
        if "product" not in core_header_roles and len(core_header_roles) < 2:
            continue
        header_rows.append(row.row)
        for role, column, label in detected_headers:
            score = _header_data_score(role, column, row_index, rows)
            candidates.setdefault(role, []).append((score, row.row, column, label))

    header_map: dict[str, str] = {}
    header_labels: dict[str, str] = {}
    for role, options in candidates.items():
        _, _, column, label = max(
            options,
            key=lambda item: (item[0], -item[1], -_excel_column_number(item[2])),
        )
        header_map[role] = column
        header_labels[role] = label
    return header_rows, header_map, header_labels

def _is_repeated_merged_row(cells: dict[str, str]) -> bool:
    values = [_word_table_key(value) for value in cells.values() if _clean(value)]
    return len(values) >= 3 and len(set(values)) == 1

def _word_table_serial(cells: dict[str, str]) -> str:
    for _, value in sorted(cells.items(), key=lambda item: _excel_column_number(item[0])):
        match = re.fullmatch(r"\s*(\d{1,5})\s*[.)]?\s*", value)
        if match:
            return match.group(1)
    return ""

def _word_table_characteristic_columns(cells: dict[str, str]) -> tuple[str, list[str]]:
    product_column = ""
    characteristic_columns: list[str] = []
    for column, label in cells.items():
        normalized = _word_table_key(label)
        if not normalized:
            continue
        if any(
            marker in normalized
            for marker in (
                "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a",
                "\u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a",
                "\u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d",
                "\u043e\u043f\u0438\u0441\u0430\u043d",
            )
        ):
            characteristic_columns.append(column)
        if (
            not product_column
            and "\u043d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d" in normalized
            and "\u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a" not in normalized
        ):
            product_column = column
        if (
            not product_column
            and "назван" in normalized
            and "характеристик" not in normalized
        ):
            product_column = column
    return product_column, characteristic_columns

def _word_table_characteristics(
    *,
    value: Any,
    fallback_name: str,
    table_label: str,
    section_role: str,
    column: str,
    association_method: str,
    association_confidence: float,
) -> list[ProductCharacteristic]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(
        r"\s*;{2,}\s*|\s*;\s*(?=[^;:]{1,160}:)",
        text,
    )
    if len(parts) == 1 and ";" in text:
        parts = re.split(r"\s*;\s*", text)
    result: list[ProductCharacteristic] = []
    for part in parts:
        raw_part = str(part or "").strip()
        if not raw_part:
            continue
        name, separator, characteristic_value = raw_part.partition(":")
        if not separator:
            tab_parts = re.split(r"\t+", raw_part, maxsplit=1)
            if len(tab_parts) == 2:
                name, characteristic_value = tab_parts
                separator = "\t"
        if not separator or not _clean(characteristic_value):
            name = fallback_name
            characteristic_value = raw_part
        result.append(
            ProductCharacteristic(
                name=_clean(name) or fallback_name,
                value=_clean(characteristic_value),
                evidence=table_label,
                sourceReference={
                    "table": table_label,
                    "column": column,
                    "sectionRole": section_role,
                },
                confidence="high",
                associationConfidence=association_confidence,
                associationMethod=association_method,
                associationStatus="confirmed",
            )
        )
    return result

def _inline_table_characteristics(
    *,
    cells: dict[str, str],
    ignored_columns: set[str],
    table_label: str,
    section_role: str,
    association_method: str,
    association_confidence: float,
) -> list[ProductCharacteristic]:
    """Read named parameter cells from a product row with a merged description."""
    result: list[ProductCharacteristic] = []
    for column, value in sorted(
        cells.items(), key=lambda item: _excel_column_number(item[0])
    ):
        text = _clean(value)
        if (
            column in ignored_columns
            or not text
            or ":" not in text
            or _ONLY_ROW_NUMBER_PATTERN.fullmatch(text)
        ):
            continue
        result.extend(
            _word_table_characteristics(
                value=text,
                fallback_name="Characteristic",
                table_label=table_label,
                section_role=section_role,
                column=column,
                association_method=association_method,
                association_confidence=association_confidence,
            )
        )
    return result

def _enrich_structured_table_positions_with_characteristics(
    text: str,
    positions: list[TenderPosition],
) -> list[TenderPosition]:
    """Attach a companion table's requirements to its numbered product rows."""
    tables: list[
        tuple[str, int, str, list[tuple[int, dict[str, str]]]]
    ] = []
    current_type = ""
    current_number: int | None = None
    current_section_role = OTHER_ROLE
    current_table_role = OTHER_ROLE
    current_rows: list[tuple[int, dict[str, str]]] = []
    for line in text.splitlines():
        detected_role = detect_section_role(line)
        if detected_role != OTHER_ROLE:
            current_section_role = detected_role
        marker = _STRUCTURED_TABLE_MARKER_PATTERN.match(line)
        if marker:
            if current_number is not None:
                tables.append(
                    (current_type, current_number, current_table_role, current_rows)
                )
            current_type = marker.group(1)
            current_number = int(marker.group(2))
            current_table_role = current_section_role
            current_rows = []
            continue
        row = _WORD_TABLE_ROW_PATTERN.match(line)
        if current_number is not None and row:
            cells = _parse_structured_cells(row.group(2))
            if cells:
                current_rows.append((int(row.group(1)), cells))
    if current_number is not None:
        tables.append((current_type, current_number, current_table_role, current_rows))

    requirements_by_key: dict[
        tuple[str, str], tuple[str, str, list[ProductCharacteristic]]
    ] = {}
    requirements_by_name: dict[
        str, list[tuple[str, str, list[ProductCharacteristic]]]
    ] = {}
    requirements_by_row: dict[
        tuple[str, int], list[tuple[str, str, list[ProductCharacteristic]]]
    ] = {}
    position_rows_by_table: dict[str, set[int]] = {}
    for position in positions:
        reference = position.sourceReference
        if reference is None or reference.row is None or not reference.table:
            continue
        position_rows_by_table.setdefault(reference.table, set()).add(reference.row)
    table_metadata = {
        (table_type.casefold(), table_number): (table_role, len(rows))
        for table_type, table_number, table_role, rows in tables
    }
    for table_type, table_number, table_role, rows in tables:
        if len(rows) < 2:
            continue
        companion_table = ""
        previous_metadata = table_metadata.get(
            (table_type.casefold(), table_number - 1)
        )
        if previous_metadata == (table_role, len(rows)):
            companion_table = f"Таблица {table_type} {table_number - 1}"
        header_cells = rows[0][1]
        product_column, characteristic_columns = _word_table_characteristic_columns(
            header_cells
        )
        if not product_column or not characteristic_columns:
            continue
        records: list[dict[str, Any]] = []
        last_record: dict[str, Any] | None = None
        for row_number, cells in rows[1:]:
            product = _clean(cells.get(product_column))
            serial = _word_table_serial(cells)
            table_label = f"Таблица {table_type} {table_number}"
            characteristics: list[ProductCharacteristic] = []
            row_aligned = (
                bool(companion_table)
                and row_number in position_rows_by_table.get(companion_table, set())
            )
            for column in characteristic_columns:
                characteristics.extend(
                    _word_table_characteristics(
                        value=cells.get(column),
                        fallback_name=_clean(header_cells.get(column)),
                        table_label=table_label,
                        section_role=table_role,
                        column=column,
                        association_method=(
                            "same_row" if product or row_aligned else "continuation_row"
                        ),
                        association_confidence=0.98 if product else 0.96 if row_aligned else 0.90,
                    )
                )
            if product:
                last_record = {
                    "product": product,
                    "serial": serial,
                    "row": row_number,
                    "characteristics": characteristics,
                }
                records.append(last_record)
            elif characteristics and row_aligned:
                last_record = {
                    "product": "",
                    "serial": serial,
                    "row": row_number,
                    "characteristics": characteristics,
                }
                records.append(last_record)
            elif (
                last_record is not None
                and characteristics
                and (not serial or not last_record["serial"] or serial == last_record["serial"])
            ):
                last_record["characteristics"].extend(characteristics)

        for record in records:
            product_key = _word_table_key(
                _normalize_extracted_product_name(record["product"])
            )
            characteristics = record["characteristics"]
            requirements = _clean(
                "; ".join(
                    f"{item.name}: {item.value}" if item.name else item.value
                    for item in characteristics
                )
            )
            if not requirements:
                continue
            serial = record["serial"]
            source = (
                f"\u0422\u0430\u0431\u043b\u0438\u0446\u0430 {table_type} {table_number}: "
                f"{requirements}"
            )
            if serial and product_key:
                requirements_by_key[(product_key, serial)] = (
                    requirements,
                    source,
                    characteristics,
                )
            linked_value = (requirements, source, characteristics)
            if product_key:
                requirements_by_name.setdefault(product_key, []).append(linked_value)
            requirements_by_row.setdefault(
                (table_label, record["row"]),
                [],
            ).append(linked_value)
            if companion_table:
                requirements_by_row.setdefault(
                    (companion_table, record["row"]),
                    [],
                ).append(linked_value)

    if not requirements_by_key and not requirements_by_name and not requirements_by_row:
        return positions

    enriched: list[TenderPosition] = []
    for position in positions:
        product_key = _word_table_key(position.product)
        evidence_match = _WORD_TABLE_ROW_PATTERN.search(position.evidence)
        serial = _word_table_serial(
            _parse_structured_cells(evidence_match.group(2)) if evidence_match else {}
        )
        linked = requirements_by_key.get((product_key, serial)) if serial else None
        name_matches = requirements_by_name.get(product_key, [])
        if linked is None and len(name_matches) == 1:
            linked = name_matches[0]
        if linked is None and position.sourceReference is not None:
            row_matches = requirements_by_row.get(
                (
                    position.sourceReference.table,
                    position.sourceReference.row or 0,
                ),
                [],
            )
            if len(row_matches) == 1:
                linked = row_matches[0]
        if linked is None:
            enriched.append(position)
            continue
        requirements, source, characteristics = linked
        combined_requirements = _clean(" ".join((position.requirements, requirements)))
        combined_characteristics = list(position.characteristics)
        seen_characteristics = {
            (_word_table_key(item.name), _word_table_key(item.value))
            for item in combined_characteristics
        }
        for characteristic in characteristics:
            key = (_word_table_key(characteristic.name), _word_table_key(characteristic.value))
            if key not in seen_characteristics:
                combined_characteristics.append(characteristic)
                seen_characteristics.add(key)
        evidence = _clean("\n".join((position.evidence, source)))[:500]
        enriched.append(
            position.model_copy(
                update={
                    "productQuery": _clean(position.product),
                    "requirements": combined_requirements,
                    "characteristics": combined_characteristics,
                    "evidence": evidence,
                }
            )
        )
    return enriched

def _looks_like_structured_row(value: str) -> bool:
    head, separator, tail = value.partition(":")
    return bool(
        separator
        and re.search(r"\d", head)
        and re.search(r"(?:^|\|)\s*[A-Z]{1,3}\s*:", tail)
    )

def _excel_column_name(number: int) -> str:
    name = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name

def _right_aligned_header_columns(
    header_columns: dict[str, str],
    header_width: int,
    cells: dict[str, str],
) -> dict[str, str]:
    row_width = max((_excel_column_number(column) for column in cells), default=0)
    shift = row_width - header_width
    if not header_width or shift >= 0:
        return header_columns
    adjusted: dict[str, str] = {}
    for role, column in header_columns.items():
        number = _excel_column_number(column) + shift
        adjusted[role] = _excel_column_name(number) if number > 0 else column
    return adjusted

def _currency_from_price_cells(*values: Any) -> str | None:
    text = " ".join(str(value or "") for value in values).lower()
    if "₽" in text or re.search(r"\bруб(?:\.|лей|ля)?\b", text):
        return "RUB"
    return None

def _drop_unresolved_generic_table_positions(
    positions: list[TenderPosition],
) -> list[TenderPosition]:
    """Discard blank offer-form rows when a richer table in the file names the goods."""
    richer_prefixes: dict[str, set[str]] = {}
    generic_indexes: list[tuple[int, str, str]] = []
    for index, position in enumerate(positions):
        reference = position.sourceReference
        if reference is None or not reference.table:
            continue
        product_key = _word_table_key(position.product)
        words = product_key.split()
        if not words:
            continue
        file_key = _word_table_key(reference.fileName) or "__single_document__"
        if len(words) == 1 and not _clean(position.model) and not _clean(position.article):
            generic_indexes.append((index, file_key, product_key))
            continue
        for generic_index, generic_file, generic_key in list(generic_indexes):
            if generic_file == file_key and product_key.startswith(f"{generic_key} "):
                richer_prefixes.setdefault(file_key, set()).add(generic_key)
        for generic_key in list(richer_prefixes.get(file_key, set())):
            if product_key.startswith(f"{generic_key} "):
                richer_prefixes.setdefault(file_key, set()).add(generic_key)

    # The first pass may encounter generic forms after the detailed table.
    for position in positions:
        reference = position.sourceReference
        if reference is None or not reference.table:
            continue
        file_key = _word_table_key(reference.fileName) or "__single_document__"
        product_key = _word_table_key(position.product)
        for _, generic_file, generic_key in generic_indexes:
            if generic_file == file_key and product_key.startswith(f"{generic_key} "):
                richer_prefixes.setdefault(file_key, set()).add(generic_key)

    skipped = {
        index
        for index, file_key, product_key in generic_indexes
        if product_key in richer_prefixes.get(file_key, set())
    }
    return [position for index, position in enumerate(positions) if index not in skipped]

def _consolidate_repeated_table_characteristic_rows(
    positions: list[TenderPosition],
) -> list[TenderPosition]:
    """Keep one purchase position when a Word/PDF table uses one row per parameter."""
    result: list[TenderPosition] = []
    indexes: dict[tuple[str, str, str, str, float | None, str], int] = {}
    for position in positions:
        reference = position.sourceReference
        table_scope = _clean(
            reference.table or reference.sheet if reference is not None else ""
        )
        position_number = _clean(
            reference.positionNumber if reference is not None else ""
        )
        if (
            position.source != "excel_table_deterministic"
            or reference is None
            or not table_scope
            or not position_number
        ):
            result.append(position)
            continue
        key = (
            _word_table_key(reference.fileName) or "__single_document__",
            _word_table_key(table_scope),
            _word_table_key(position_number),
            _position_name_key(position),
            position.quantity,
            _word_table_key(position.unit),
        )
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(result)
            result.append(position)
            continue
        result[existing_index] = _merge_replicated_position_details(
            result[existing_index], position
        )
    return result

def extract_deterministic_positions(
    text: str,
    spreadsheet_tables: list[SpreadsheetTable] | None = None,
    max_positions: int = 5_000,
    text_table_schemas: list[SpreadsheetTable] | None = None,
) -> list[TenderPosition]:
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
    structured_spreadsheet_rows: set[str] = set()
    structured_text_table_rows: set[str] = set()

    def add(
        name: str,
        unit: str,
        raw_quantity: Any,
        evidence: str,
        *,
        candidate_id: str = "",
        document_unit_price: Any = None,
        document_line_total: Any = None,
        document_currency: str | None = None,
        document_price_source: DocumentPriceSource | None = None,
        source_reference: ProductSourceReference | None = None,
        source_cells: dict[str, str] | None = None,
        characteristics: list[ProductCharacteristic] | None = None,
        requirements: str = "",
    ) -> None:
        name, unit = _normalize_extracted_product_name(name), _clean(unit)
        quantity = parse_quantity(raw_quantity)
        price_only_row = (
            quantity is None
            and (document_unit_price not in {None, ""} or document_line_total not in {None, ""})
        )
        if (
            (quantity is None and not price_only_row)
            or _ONLY_ROW_NUMBER_PATTERN.fullmatch(name)
            or _ONLY_CLASSIFIER_CODE_PATTERN.fullmatch(name)
            or re.search(r"наименование\s+товара|кол-?во", name, re.I)
        ):
            return
        result.append(
            TenderPosition(
                candidateId=candidate_id,
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
                sourceReference=source_reference,
                sourceCells=dict(source_cells or {}),
                characteristics=list(characteristics or []),
                requirements=_clean(requirements),
            )
        )

    def table_characteristics(
        *,
        table: SpreadsheetTable,
        row: SpreadsheetRow,
        cells: dict[str, str],
        column_labels: dict[str, str],
        ignored_columns: set[str],
        association_method: str,
        association_confidence: float,
    ) -> list[ProductCharacteristic]:
        allowed_columns = set(table.characteristicColumns)
        source_role = classify_document_role(table.fileName, table.sheet)
        # A calculation table from an NMC/NMCk price justification contains
        # procurement identity, quantity, and money only.  It must not turn
        # row numbers, formulas, averages, or calculated prices into product
        # requirements merely because its schema was classified as
        # ``purchase_items`` instead of ``price_list``.
        if (
            source_role == PRICE_ROLE
            and table.tableRole != "technical_specification"
            and not allowed_columns
        ):
            return []
        # A price justification may supply a product title and price, but its
        # remaining statistical/calculation fields are not product specs. Only
        # an explicitly classified technical column may be carried forward.
        if table.tableRole == "price_list" and not allowed_columns:
            return []
        items: list[ProductCharacteristic] = []
        for column, value in cells.items():
            label = _clean(column_labels.get(column))
            normalized_label = _normalize_header(label)
            characteristic_value = _clean(value)
            if (
                column in ignored_columns
                or (allowed_columns and column not in allowed_columns)
                or not characteristic_value
                or normalized_label in {"№", "no", "n", "номер", "п п", "пп"}
                or re.search(
                    r"(?:^код\b|окпд|ктру|тн\s+вэд|цена|стоимост|сумма|итого|"
                    r"руб(?:ль|лей)?\b|предложени[ея]\s+поставщик)",
                    normalized_label,
                )
            ):
                continue
            items.append(
                ProductCharacteristic(
                    name=label or f"Колонка {column}",
                    value=characteristic_value,
                    evidence=f"{table.fileName}; {table.sheet}; строка {row.row}",
                    sourceReference={
                        "fileName": table.fileName,
                        "sheet": table.sheet,
                        "row": row.row,
                        "column": column,
                    },
                    confidence="high" if label else "medium",
                    associationConfidence=association_confidence,
                    associationMethod=association_method,
                    associationStatus="confirmed",
                )
            )
        return items

    for raw_table in spreadsheet_tables or []:
        try:
            table = (
                raw_table
                if isinstance(raw_table, SpreadsheetTable)
                else SpreadsheetTable.model_validate(raw_table)
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            continue
        if table.tableRole in {
            "delivery_schedule",
            "offer_form",
            "contract_template",
            "other",
            "ambiguous",
        }:
            continue
        if any(_is_delivery_schedule_header(row.cells) for row in table.rows):
            continue
        header_columns = dict(table.headerMap)
        header_labels = dict(table.headerLabels)
        header_rows = set(table.headerRows)
        column_labels: dict[str, str] = {}
        for header_row in table.rows:
            if header_row.row not in header_rows:
                continue
            for column, value in header_row.cells.items():
                label = _clean(value)
                if label:
                    current = column_labels.get(column, "")
                    if label not in current.split(" / "):
                        column_labels[column] = " / ".join(filter(None, (current, label)))
        use_precomputed_headers = "product" in header_columns and bool(
            {"quantity", "unit_price", "line_total"} & set(header_columns)
        )
        last_result_index: int | None = None
        for row in table.rows:
            cells = row.cells
            if use_precomputed_headers:
                if row.row in header_rows:
                    continue
            else:
                detected_headers = {
                    role: (column, value)
                    for column, value in cells.items()
                    if (role := _header_role(value)) is not None
                }
                core_header_roles = set(detected_headers) & {
                    "product",
                    "unit",
                    "quantity",
                    "unit_price",
                    "line_total",
                }
                is_header_row = (
                    "product" in detected_headers
                    or len(core_header_roles) >= 2
                )
                if is_header_row:
                    for column, label in cells.items():
                        cleaned_label = _clean(label)
                        if cleaned_label:
                            current = column_labels.get(column, "")
                            if cleaned_label not in current.split(" / "):
                                column_labels[column] = " / ".join(
                                    filter(None, (current, cleaned_label))
                                )
                    for role, (column, label) in detected_headers.items():
                        header_columns[role] = column
                        header_labels[role] = label
                    continue
            if "product" not in header_columns or not (
                {"quantity", "unit_price", "line_total"} & set(header_columns)
            ):
                continue
            product_column = header_columns["product"]
            quantity_column = header_columns.get("quantity", "")
            ignored_columns = {
                column
                for column in header_columns.values()
                if column
            }
            name = cells.get(product_column, "")
            name = _compose_table_product_identity(
                name,
                cells,
                column_labels,
                product_column,
            )
            characteristics = table_characteristics(
                table=table,
                row=row,
                cells=cells,
                column_labels=column_labels,
                ignored_columns=ignored_columns,
                association_method="same_row" if name else "continuation_row",
                association_confidence=0.98 if name else 0.90,
            )
            if not name:
                if characteristics and last_result_index is not None:
                    existing = result[last_result_index]
                    combined_characteristics = list(existing.characteristics) + characteristics
                    continuation_requirements = "; ".join(
                        f"{item.name}: {item.value}" if item.name else item.value
                        for item in characteristics
                    )
                    combined_requirements = _clean(
                        "; ".join(
                            filter(None, (existing.requirements, continuation_requirements))
                        )
                    )
                    continuation_evidence = (
                        f"Строка {row.row}: "
                        + " | ".join(
                            f"{column}: {value}" for column, value in cells.items()
                        )
                    )
                    result[last_result_index] = existing.model_copy(
                        update={
                            "characteristics": combined_characteristics,
                            "requirements": combined_requirements,
                            "evidence": _clean(
                                " | ".join(
                                    filter(None, (existing.evidence, continuation_evidence))
                                )
                            )[:1200],
                        }
                    )
                    structured_spreadsheet_rows.add(_clean(continuation_evidence))
                continue
            unit, unit_column = _table_unit(cells, header_columns, header_labels)
            raw_quantity = cells.get(quantity_column) if quantity_column else None
            if not unit:
                unit = _unit_from_quantity_value(raw_quantity)
            unit_price_column = header_columns.get("unit_price", "")
            line_total_column = header_columns.get("line_total", "")
            raw_unit_price = cells.get(unit_price_column) if unit_price_column else None
            raw_line_total = cells.get(line_total_column) if line_total_column else None
            has_document_price = (
                raw_unit_price not in {None, ""}
                or raw_line_total not in {None, ""}
            )
            if not unit or (raw_quantity is None and not has_document_price):
                continue
            evidence = (
                f"Строка {row.row}: "
                + " | ".join(
                    f"{column}: {value}"
                    for column, value in cells.items()
                )
            )
            structured_spreadsheet_rows.add(_clean(evidence))
            price_source = (
                DocumentPriceSource(
                    fileName=table.fileName,
                    sheet=table.sheet,
                    row=row.row,
                    unitPriceColumn=unit_price_column,
                    lineTotalColumn=line_total_column,
                    unitPriceHeader=header_labels.get("unit_price", ""),
                    lineTotalHeader=header_labels.get("line_total", ""),
                    extractionMethod="excel_deterministic",
                )
                if has_document_price
                else None
            )
            requirements = "; ".join(
                f"{item.name}: {item.value}" if item.name else item.value
                for item in characteristics
            )
            before_count = len(result)
            add(
                name,
                unit,
                raw_quantity,
                evidence,
                candidate_id=f"xlsx:{table.fileName}:{table.sheet}:{row.row}",
                document_unit_price=raw_unit_price,
                document_line_total=raw_line_total,
                document_currency=_currency_from_price_cells(
                    header_labels.get("unit_price"),
                    header_labels.get("line_total"),
                    raw_unit_price,
                    raw_line_total,
                ),
                document_price_source=price_source,
                source_reference=ProductSourceReference(
                    fileName=table.fileName,
                    sheet=table.sheet,
                    sectionRole=classify_document_role(table.fileName, table.sheet),
                    row=row.row,
                    productColumn=product_column,
                    quantityColumn=quantity_column,
                    unitColumn=unit_column,
                    productHeader=header_labels.get("product", ""),
                    quantityHeader=header_labels.get("quantity", ""),
                    unitHeader=header_labels.get("unit", ""),
                    positionNumber=_word_table_serial(cells),
                    extractionMethod="excel_deterministic",
                ),
                source_cells=cells,
                characteristics=characteristics,
                requirements=requirements,
            )
            if len(result) > before_count:
                last_result_index = len(result) - 1
            if len(result) >= max_positions:
                return result

    # Header-aware extraction for text emitted by the XLS/XLSX/CSV parser.
    # Column addresses make this deterministic even when cells between columns are empty.
    current_file = ""
    current_sheet = ""
    header_columns: dict[str, str] = {}
    header_labels: dict[str, str] = {}
    column_labels: dict[str, str] = {}
    header_width = 0
    last_text_table_result_index: int | None = None
    pdf_inherited_headers_pending = False
    current_section_role = OTHER_ROLE
    delivery_schedule_pending = False
    skip_current_text_table = False
    text_schema_by_key = {
        (table.fileName, table.sheet): table
        for table in text_table_schemas or []
    }
    for line in text.splitlines():
        if line.startswith("--- ДОКУМЕНТ "):
            current_file = ""
            current_sheet = ""
            header_columns = {}
            header_labels = {}
            column_labels = {}
            header_width = 0
            current_section_role = OTHER_ROLE
            delivery_schedule_pending = False
            skip_current_text_table = False
            continue
        detected_section_role = detect_section_role(line)
        if detected_section_role != OTHER_ROLE:
            current_section_role = detected_section_role
        if _DELIVERY_SCHEDULE_HEADING_PATTERN.search(line):
            delivery_schedule_pending = True
        if line.startswith(("Таблица Word ", "Таблица PDF ", "Таблица RTF ")):
            inherit_pdf_headers = (
                bool(re.search(r"\bPDF\s+\d", current_sheet, re.IGNORECASE))
                and bool(re.search(r"\bPDF\s+\d", line, re.IGNORECASE))
                and {"product", "quantity"}.issubset(header_columns)
            )
            current_sheet = line.strip()
            pdf_inherited_headers_pending = inherit_pdf_headers
            skip_current_text_table = delivery_schedule_pending
            delivery_schedule_pending = False
            if not inherit_pdf_headers:
                header_columns = {}
                header_labels = {}
                column_labels = {}
                header_width = 0
                last_text_table_result_index = None
            continue
        if line.lower().startswith("filename:"):
            current_file = line.split(":", 1)[1].strip()
            file_role = classify_document_role(current_file)
            if file_role not in {OTHER_ROLE, COMPOSITE_ROLE}:
                current_section_role = file_role
            continue
        if line.lower().startswith("documentkind:"):
            document_kind = line.split(":", 1)[1].strip()
            kind_role = {
                "technical": "technical_specification",
                "specification": "technical_specification",
                "price_justification": "price_justification",
                "contract": "contract",
            }.get(document_kind.casefold(), OTHER_ROLE)
            if kind_role != OTHER_ROLE:
                current_section_role = kind_role
            continue
        if line.startswith("Лист:"):
            current_sheet = line.split(":", 1)[1].strip()
            header_columns = {}
            header_labels = {}
            column_labels = {}
            header_width = 0
            last_text_table_result_index = None
            delivery_schedule_pending = False
            skip_current_text_table = False
            continue
        row_match = re.match(r"^Строка\s+(\d+)\s*:\s*(.+)$", line, re.I)
        if not row_match:
            continue
        row_number = int(row_match.group(1))
        cells = _parse_structured_cells(row_match.group(2))
        if not cells:
            continue
        table_schema = text_schema_by_key.get((current_file, current_sheet))
        if table_schema and table_schema.tableRole in {
            "delivery_schedule",
            "offer_form",
            "contract_template",
            "other",
            "ambiguous",
        }:
            skip_current_text_table = True
        schema_headers_locked = bool(table_schema and table_schema.headerMap)
        if schema_headers_locked and table_schema:
            header_columns = dict(table_schema.headerMap)
            header_labels = dict(table_schema.headerLabels)
        if re.search(r"\b(?:Word|PDF|RTF)\s+\d", current_sheet, re.IGNORECASE):
            structured_text_table_rows.add(_clean(line))
        if _is_delivery_schedule_header(cells):
            skip_current_text_table = True
        if skip_current_text_table:
            continue
        if _is_repeated_merged_row(cells):
            continue
        detected_headers = {
            role: (column, value)
            for column, value in cells.items()
            if (role := _header_role(value)) is not None
        }
        core_header_roles = set(detected_headers) & {
            "product",
            "unit",
            "quantity",
            "unit_price",
            "line_total",
        }
        is_header_row = "product" in detected_headers or len(core_header_roles) >= 2
        if is_header_row:
            if schema_headers_locked:
                continue
            if pdf_inherited_headers_pending:
                header_columns = {}
                header_labels = {}
                column_labels = {}
                header_width = 0
                pdf_inherited_headers_pending = False
            header_width = max(
                header_width,
                max((_excel_column_number(column) for column in cells), default=0),
            )
            for column, label in cells.items():
                cleaned_label = _clean(label)
                if cleaned_label:
                    current = column_labels.get(column, "")
                    if cleaned_label not in current.split(" / "):
                        column_labels[column] = " / ".join(
                            filter(None, (current, cleaned_label))
                        )
            for role, (column, label) in detected_headers.items():
                header_columns[role] = column
                header_labels[role] = label
            continue
        if not {"product", "quantity"}.issubset(header_columns):
            continue
        active_header_columns = header_columns
        product_column = active_header_columns["product"]
        quantity_column = active_header_columns["quantity"]
        unit, unit_column = _table_unit(cells, active_header_columns, header_labels)
        name = cells.get(product_column, "")
        raw_quantity = cells.get(quantity_column)
        if not unit:
            unit = _unit_from_quantity_value(raw_quantity)
        if not name or not unit or raw_quantity is None:
            adjusted_columns = _right_aligned_header_columns(
                header_columns,
                header_width,
                cells,
            )
            adjusted_unit, adjusted_unit_column = _table_unit(
                cells,
                adjusted_columns,
                header_labels,
            )
            if not adjusted_unit:
                adjusted_unit = _unit_from_quantity_value(
                    cells.get(adjusted_columns["quantity"])
                )
            adjusted_product_column = adjusted_columns["product"]
            adjusted_quantity_column = adjusted_columns["quantity"]
            adjusted_name = cells.get(adjusted_product_column, "")
            adjusted_quantity = cells.get(adjusted_quantity_column)
            if (
                adjusted_name
                and adjusted_quantity is not None
                and parse_quantity(adjusted_quantity) is not None
                and re.fullmatch(UNITS, adjusted_unit, re.IGNORECASE)
            ):
                active_header_columns = adjusted_columns
                product_column = adjusted_product_column
                quantity_column = adjusted_quantity_column
                unit = adjusted_unit
                unit_column = adjusted_unit_column
                name = adjusted_name
                raw_quantity = adjusted_quantity
        if not name or not unit or raw_quantity is None:
            structured_spreadsheet_rows.add(_clean(line))
            serial = _word_table_serial(cells)
            if last_text_table_result_index is not None and not serial:
                ignored_columns = {
                    column
                    for role, column in active_header_columns.items()
                    if role in {"product", "unit", "quantity", "unit_price", "line_total"}
                    and column
                }
                continuation = _inline_table_characteristics(
                    cells=cells,
                    ignored_columns=ignored_columns,
                    table_label=current_sheet,
                    section_role=current_section_role,
                    association_method="continuation_row",
                    association_confidence=0.90,
                )
                if continuation:
                    existing = result[last_text_table_result_index]
                    continuation_requirements = "; ".join(
                        f"{item.name}: {item.value}" for item in continuation
                    )
                    result[last_text_table_result_index] = existing.model_copy(
                        update={
                            "characteristics": list(existing.characteristics)
                            + continuation,
                            "requirements": _clean(
                                "; ".join(
                                    filter(
                                        None,
                                        (
                                            existing.requirements,
                                            continuation_requirements,
                                        ),
                                    )
                                )
                            ),
                        }
                    )
            continue
        name = _compose_table_product_identity(
            name,
            cells,
            column_labels,
            product_column,
        )
        if _clean(line) in structured_spreadsheet_rows:
            continue
        ignored_columns = {
            column
            for role, column in active_header_columns.items()
            if role in {"product", "unit", "quantity", "unit_price", "line_total"}
            and column
        }
        characteristics = _inline_table_characteristics(
            cells=cells,
            ignored_columns=ignored_columns,
            table_label=current_sheet,
            section_role=current_section_role,
            association_method="same_row",
            association_confidence=0.98,
        )
        unit_price_column = active_header_columns.get("unit_price", "")
        line_total_column = active_header_columns.get("line_total", "")
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
        requirements = "; ".join(
            f"{item.name}: {item.value}" for item in characteristics
        )
        before_count = len(result)
        structured_spreadsheet_rows.add(_clean(line))
        add(
            name,
            unit,
            raw_quantity,
            line,
            candidate_id=(
                f"table:{current_file}:{current_sheet}:{row_number}:{product_column}"
            ),
            document_unit_price=raw_unit_price,
            document_line_total=raw_line_total,
            document_currency=_currency_from_price_cells(
                header_labels.get("unit_price"),
                header_labels.get("line_total"),
                raw_unit_price,
                raw_line_total,
            ),
            document_price_source=price_source,
            source_reference=ProductSourceReference(
                fileName=current_file,
                sheet=current_sheet,
                row=row_number,
                productColumn=product_column,
                quantityColumn=quantity_column,
                unitColumn=unit_column,
                productHeader=header_labels.get("product", ""),
                quantityHeader=header_labels.get("quantity", ""),
                unitHeader=header_labels.get("unit", ""),
                table=current_sheet if current_sheet.startswith("Таблица ") else "",
                positionNumber=_word_table_serial(cells),
                sectionRole=current_section_role,
                extractionMethod="excel_deterministic",
            ),
            characteristics=characteristics,
            requirements=requirements,
        )
        if len(result) > before_count:
            last_text_table_result_index = len(result) - 1
        if len(result) >= max_positions:
            return result

    # Structured row emitted by the spreadsheet parser:
    # "Строка 2: A: 1 | B: Кабель | D: шт | E: 10".
    for row_match in re.finditer(r"^Строка\s+\d+\s*:\s*(.+)$", text, re.I | re.M):
        if _clean(row_match.group(0)) in (
            structured_spreadsheet_rows | structured_text_table_rows
        ):
            continue
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
            if len(result) >= max_positions:
                return result

    fallback_lines: list[str] = []
    pdf_page_has_structured_table = False
    has_pdf_structured_tables = any(
        line.startswith("--- PDF PAGE ") and line.endswith(" STRUCTURED TABLES ---")
        for line in text.splitlines()
    )
    skip_pdf_raw_text = False
    for line in text.splitlines():
        if line.startswith("--- PDF PAGE ") and line.endswith(" BEGIN ---"):
            pdf_page_has_structured_table = False
            skip_pdf_raw_text = False
            continue
        if line.startswith("--- PDF PAGE ") and line.endswith(" STRUCTURED TABLES ---"):
            pdf_page_has_structured_table = True
            continue
        if line.startswith("--- PDF PAGE ") and line.endswith(" RAW TEXT ---"):
            skip_pdf_raw_text = has_pdf_structured_tables or pdf_page_has_structured_table
            continue
        if skip_pdf_raw_text or _looks_like_structured_row(line):
            continue
        fallback_lines.append(line)
    fallback_text = "\n".join(fallback_lines)
    fallback_normalized = _clean(fallback_text)
    for pattern_index, pattern in enumerate(patterns):
        for match in pattern.finditer(fallback_normalized if pattern_index == 0 else fallback_text):
            if pattern_index == 0:
                name, unit, raw_quantity = match.group(2), match.group(3), match.group(4)
            else:
                name, unit, raw_quantity = match.group(1), match.group(2), match.group(3)
            add(name, unit, raw_quantity, match.group(0))
            if len(result) >= max_positions:
                return result
    return _drop_unresolved_generic_table_positions(
        _consolidate_repeated_table_characteristic_rows(
            _enrich_structured_table_positions_with_characteristics(text, result)
        )
    )


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
            if not key[0]:
                continue
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
                    sourceReference=ProductSourceReference(
                        extractionMethod="seldon_structured"
                    ),
                )
            )
            if len(result) >= 100:
                return result
    return result
