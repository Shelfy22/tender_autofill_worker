from __future__ import annotations

import json
import re
from typing import Any

from app.models import (
    DocumentPriceSource,
    ProductCharacteristic,
    ProductSourceReference,
    SpreadsheetRow,
    SpreadsheetTable,
    TenderPosition,
    TenderPositionsResponse,
)
from app.services.document_roles import (
    COMPOSITE_ROLE,
    OTHER_ROLE,
    TECHNICAL_ROLE,
    classify_document_role,
    detect_section_role,
    source_role_priority,
)


_ONLY_ROW_NUMBER_PATTERN = re.compile(
    r"^\s*[+-]?\d+(?:[.,]\d+)?\s*[.)-]?\s*$"
)
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
_CLASSIFIER_SUFFIX_PATTERN = re.compile(
    r"\s*(?:код\s+)?(?:окпд2?|ктру|тн\s+вэд)\s*:?\s*\d{2}(?:[.\s-]\d{1,3}){2,}\s*$",
    re.IGNORECASE,
)
_EQUIVALENT_SUFFIX_PATTERN = re.compile(
    r"\s*[([]?\s*(?:или\s+)?(?:аналог|эквивалент)\s*[)\]]?\s*$",
    re.IGNORECASE,
)
_CONDITION_POSITION_PATTERN = re.compile(
    r"^\s*(?:"
    r"аналоги?\s+рассматрива(?:ются|ется)(?:\s*[.!;:]?\s*допуск(?:\s+по)?\s+[а-яё\s]+\s*[±+\-]?\s*\d+(?:[,.]\d+)?\s*%)?|"
    r"эквиваленты?\s+(?:допуска(?:ются|ется)|разрешены?)|"
    r"аналог\s+допуска(?:ется|ются)|без\s+аналогов|"
    r"допуск(?:\s+по)?\s+(?:габарит[а-яё]*|толщин[а-яё]*|размер[а-яё]*)\s*[±+\-]?\s*\d+(?:[,.]\d+)?\s*%|"
    r"согласно\s+(?:техническому\s+задани[юя]|тз)|"
    r"(?:не\s+менее|не\s+более|не\s+хуже|до|от)\s+\d+(?:[,.]\d+)?\s*(?:месяц[а-яё]*|мес\.?|дн(?:ей|я)?|сут(?:ок|ки)?|лет|год[а-яё]*|%)"
    r")\s*[.!;:]*\s*$",
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
_DELIVERY_SCHEDULE_HEADING_PATTERN = re.compile(
    r"(?:\u0433\u0440\u0430\u0444\u0438\u043a|\u043f\u043b\u0430\u043d)\s+(?:\u043f\u043e\u0441\u0442\u0430\u0432\u043a|\u043e\u0442\u0433\u0440\u0443\u0437\u043a|\u0434\u043e\u0441\u0442\u0430\u0432\u043a)",
    re.IGNORECASE,
)
_DELIVERY_SCHEDULE_COLUMN_PATTERN = re.compile(
    r"(?:\u0441\u0440\u043e\u043a|\u0434\u0430\u0442\u0430|\u043f\u0435\u0440\u0438\u043e\u0434|\u043c\u0435\u0441\u044f\u0446)\s+(?:\u043f\u043e\u0441\u0442\u0430\u0432\u043a|\u043e\u0442\u0433\u0440\u0443\u0437\u043a|\u0434\u043e\u0441\u0442\u0430\u0432\u043a)",
    re.IGNORECASE,
)


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|уп\.?|упак\.?"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()


def _strip_classifier_suffix(value: str) -> str:
    return _clean(_CLASSIFIER_SUFFIX_PATTERN.sub("", value))


def _normalize_extracted_product_name(value: str) -> str:
    value = _strip_classifier_suffix(_clean(value))
    return _clean(_EQUIVALENT_SUFFIX_PATTERN.sub("", value))


_TABLE_IDENTITY_HEADER_PATTERN = re.compile(
    r"(?:\bмарка\b|\bмодель\b|\bартикул\b|\bобозначени|\bтип\b)",
    re.IGNORECASE,
)
_TABLE_IDENTITY_EXCLUDED_HEADER_PATTERN = re.compile(
    r"(?:предложени.*участник|страна|техническ.*регламент|стандарт)",
    re.IGNORECASE,
)


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
        or _CONDITION_POSITION_PATTERN.fullmatch(product)
        or _ADDRESS_OR_RECIPIENT_PATTERN.search(product)
        or _is_tender_subject_or_survey_title(position)
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
    text = re.sub(r"\bколи-\s*чество\b", "количество", text, flags=re.IGNORECASE)
    text = re.sub(r"\bдиа-\s*метр\b", "диаметр", text, flags=re.IGNORECASE)
    return re.sub(r"[^a-zа-я0-9№]+", " ", text).strip()


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


def _parse_structured_cells(value: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for raw_part in value.split("|"):
        match = re.match(r"^\s*([A-Z]{1,3}):\s*(.*?)\s*$", raw_part)
        if match and match.group(2):
            cells[match.group(1)] = match.group(2)
    return cells


_STRUCTURED_TABLE_MARKER_PATTERN = re.compile(
    r"^\s*\u0422\u0430\u0431\u043b\u0438\u0446\u0430\s+(Word|PDF|RTF)\s+(\d+)\s*$",
    re.IGNORECASE,
)
_WORD_TABLE_ROW_PATTERN = re.compile(
    r"^\s*\u0421\u0442\u0440\u043e\u043a\u0430\s+(\d+)\s*:\s*(.+)$",
    re.IGNORECASE,
)


def _word_table_key(value: Any) -> str:
    return re.sub(
        r"[^a-z\u0410-\u044f0-9]+",
        " ",
        _clean(value).casefold().replace("\u0451", "\u0435"),
    ).strip()


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


def _excel_column_number(value: str) -> int:
    number = 0
    for character in value.upper():
        if not "A" <= character <= "Z":
            return 0
        number = number * 26 + ord(character) - ord("A") + 1
    return number


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
        key = (name.lower().replace("ё", "е"), unit.lower(), quantity)
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
        items: list[ProductCharacteristic] = []
        for column, value in cells.items():
            label = _clean(column_labels.get(column))
            normalized_label = _normalize_header(label)
            characteristic_value = _clean(value)
            if (
                column in ignored_columns
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


def _position_name_key(position: TenderPosition) -> str:
    value = _clean(position.product).lower().replace("ё", "е")
    description_match = _PRODUCT_DESCRIPTION_SEPARATOR_PATTERN.match(value)
    if description_match:
        value = _clean(description_match.group(1))
    value = re.sub(
        r"\s*[([]?\s*(?:или\s+)?(?:аналог|эквивалент)\s*[)\]]?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()


def _position_source_key(position: TenderPosition) -> str:
    reference = position.sourceReference
    if reference is not None and reference.row is not None:
        table_scope = reference.sheet or reference.table
        if reference.fileName and table_scope:
            return ":".join(
                (
                    "source",
                    _word_table_key(reference.fileName),
                    _word_table_key(table_scope),
                    str(reference.row),
                )
            )
        if reference.fileName and reference.productColumn:
            return ":".join(
                (
                    "source",
                    _word_table_key(reference.fileName),
                    str(reference.row),
                    _word_table_key(reference.productColumn),
                )
            )
    if position.candidateId:
        return f"candidate:{position.candidateId}"
    return ""


def _position_source_role(position: TenderPosition) -> str:
    reference = position.sourceReference
    if reference is None:
        return OTHER_ROLE
    if reference.sectionRole and reference.sectionRole != OTHER_ROLE:
        return reference.sectionRole
    return classify_document_role(reference.fileName, reference.sheet, reference.table)


def _position_unit_key(value: Any) -> str:
    unit = _word_table_key(value)
    if re.fullmatch(r"\u0448\u0442(?:\u0443\u043a\u0430|\u0443\u043a\u0438|\u0443\u043a)?", unit):
        return "\u0448\u0442"
    return unit


def _same_replicated_product(left: TenderPosition, right: TenderPosition) -> bool:
    left_key = _position_name_key(left)
    right_key = _position_name_key(right)
    return bool(left_key and right_key) and (
        left_key == right_key
        or left_key.startswith(f"{right_key} ")
        or right_key.startswith(f"{left_key} ")
    )


_GENERIC_NAME_WORDS = {
    "или", "эквивалент", "поставка", "товар", "товары", "для", "с",
    "и", "в", "на", "по", "из", "со",
}


def _product_name_stems(position: TenderPosition) -> set[str]:
    words = re.findall(r"[a-zа-яё0-9]+", _clean(position.product).casefold())
    return {
        word[:5]
        for word in words
        if len(word) >= 3 and word not in _GENERIC_NAME_WORDS and not word.isdigit()
    }


def _same_source_row_product_variant(left: TenderPosition, right: TenderPosition) -> bool:
    left_reference = left.sourceReference
    right_reference = right.sourceReference
    if (
        left_reference is None
        or right_reference is None
        or left_reference.row is None
        or right_reference.row is None
        or _word_table_key(left_reference.fileName) != _word_table_key(right_reference.fileName)
        or left_reference.row != right_reference.row
        or left.quantity != right.quantity
        or _position_unit_key(left.unit) != _position_unit_key(right.unit)
    ):
        return False
    if _same_replicated_product(left, right):
        return True
    return len(_product_name_stems(left) & _product_name_stems(right)) >= 2


def _is_generic_product_copy(position: TenderPosition) -> bool:
    name = _clean(position.product).casefold()
    stems = _product_name_stems(position)
    return "поставка" in name or len(stems) <= 1


def _generic_copy_matches_detail(generic: TenderPosition, detailed: TenderPosition) -> bool:
    if generic.quantity != detailed.quantity or _position_unit_key(generic.unit) != _position_unit_key(detailed.unit):
        return False
    generic_stems = _product_name_stems(generic)
    detailed_stems = _product_name_stems(detailed)
    if not generic_stems or not detailed_stems:
        return False
    shared = generic_stems & detailed_stems
    return len(shared) >= min(2, len(generic_stems))


_POWER_TRANSFORMER_SERIES_PATTERN = re.compile(r"\bтмгф?\b", re.IGNORECASE)
_POWER_TRANSFORMER_CAPACITY_PATTERN = re.compile(
    r"(?:\bтмгф?\s*[- ]*|\b)(\d{2,5})(?=\s*(?:ква|кв\s*а|/))",
    re.IGNORECASE,
)


def _power_transformer_identity(position: TenderPosition) -> tuple[str, str] | None:
    """Return the stable series/capacity pair used in replicated transformer tables."""
    text = " ".join(
        (
            position.product,
            position.productQuery or "",
            *(item.value for item in position.characteristics),
        )
    )
    series = _POWER_TRANSFORMER_SERIES_PATTERN.search(text)
    capacity = _POWER_TRANSFORMER_CAPACITY_PATTERN.search(text)
    if series is None or capacity is None:
        return None
    return series.group(0).casefold(), capacity.group(1)


def _cross_document_position_key(position: TenderPosition) -> tuple[str, str, float | None, str] | None:
    reference = position.sourceReference
    if reference is None or not _clean(reference.fileName):
        return None
    product = _position_name_key(position)
    if not product:
        return None
    strong_identity = (
        _word_table_key(position.article)
        or _word_table_key(position.model)
        or (
            f"{_word_table_key(position.lotNumber)}:{_word_table_key(position.positionNumber)}"
            if position.lotNumber and position.positionNumber
            else ""
        )
    )
    return (product, strong_identity, position.quantity, _position_unit_key(position.unit))


def _source_reference_key(reference: ProductSourceReference | None) -> tuple[str, str, int | None, str]:
    if reference is None:
        return ("", "", None, "")
    return (
        _word_table_key(reference.fileName),
        _word_table_key(reference.sheet or reference.table),
        reference.row,
        _word_table_key(reference.productColumn),
    )


def _all_source_references(position: TenderPosition) -> list[ProductSourceReference]:
    references = list(position.sourceReferences)
    if position.sourceReference is not None:
        references.append(position.sourceReference)
    result: list[ProductSourceReference] = []
    seen: set[tuple[str, str, int | None, str]] = set()
    for reference in references:
        key = _source_reference_key(reference)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(reference)
    return result


def _product_identity_specificity(position: TenderPosition) -> tuple[int, int, int, int]:
    """Prefer a concrete model/article over a generic table label."""
    title = _clean(position.product)
    model_or_article = int(bool(_clean(position.model) or _clean(position.article)))
    model_tokens = len(re.findall(r"(?=.*[a-zа-яё])(?=.*\d)[a-zа-яё0-9./_-]{3,}", title.casefold()))
    return (
        model_or_article,
        model_tokens,
        len(_product_name_stems(position)),
        len(title),
    )


def _preferred_product_identity(
    canonical: TenderPosition,
    duplicate: TenderPosition,
) -> TenderPosition:
    # A document LLM often repeats a structured row as a verbose phrase. It
    # must not replace the table's product identity merely because it is longer.
    if duplicate.source == "llm" and canonical.source != "llm":
        return canonical
    # A price-list row is already a valid canonical name unless the other
    # structured table explicitly supplies a model or article.
    if (
        _position_source_role(canonical) == "price_justification"
        and not (_clean(duplicate.model) or _clean(duplicate.article))
    ):
        return canonical
    if _product_identity_specificity(duplicate) <= _product_identity_specificity(canonical):
        return canonical
    updates: dict[str, Any] = {"product": duplicate.product}
    for field in ("productQuery", "model", "article", "brand", "sourceReference", "sourceCells"):
        value = getattr(duplicate, field)
        if not _missing(value):
            updates[field] = value
    return canonical.model_copy(update=updates)


def _merge_replicated_position_details(
    canonical: TenderPosition,
    duplicate: TenderPosition,
) -> TenderPosition:
    canonical = _preferred_product_identity(canonical, duplicate)
    updates: dict[str, Any] = {}
    characteristics = list(canonical.characteristics)
    seen_characteristics = {
        (_word_table_key(item.name), _word_table_key(item.value))
        for item in characteristics
    }
    for characteristic in duplicate.characteristics:
        key = (
            _word_table_key(characteristic.name),
            _word_table_key(characteristic.value),
        )
        if key not in seen_characteristics:
            characteristics.append(characteristic)
            seen_characteristics.add(key)
    if characteristics != canonical.characteristics:
        updates["characteristics"] = characteristics

    duplicate_requirements = _clean(duplicate.requirements)
    canonical_requirements = _clean(canonical.requirements)
    if duplicate_requirements and duplicate_requirements not in canonical_requirements:
        updates["requirements"] = _clean(
            "; ".join(filter(None, (canonical_requirements, duplicate_requirements)))
        )

    duplicate_evidence = _clean(duplicate.evidence)
    canonical_evidence = _clean(canonical.evidence)
    if duplicate_evidence and duplicate_evidence not in canonical_evidence:
        updates["evidence"] = _clean(
            " | ".join(filter(None, (canonical_evidence, duplicate_evidence)))
        )[:1200]

    source_references = _all_source_references(canonical) + _all_source_references(duplicate)
    deduplicated_references: list[ProductSourceReference] = []
    seen_references: set[tuple[str, str, int | None, str]] = set()
    for reference in source_references:
        key = _source_reference_key(reference)
        if key in seen_references:
            continue
        seen_references.add(key)
        deduplicated_references.append(reference)
    if deduplicated_references != canonical.sourceReferences:
        updates["sourceReferences"] = deduplicated_references

    # Price is authoritative in an NMC/price-justification table, regardless
    # of which source donated the most concrete product title.
    if _position_source_role(duplicate) == "price_justification":
        for field in (
            "documentUnitPriceRub",
            "documentLineTotalRub",
            "documentCurrency",
            "documentPriceEvidence",
            "documentPriceSource",
        ):
            value = getattr(duplicate, field)
            if not _missing(value):
                updates[field] = value
    return canonical.model_copy(update=updates) if updates else canonical


def _aligned_section_duplicate_pairs(
    positions: list[TenderPosition],
) -> list[tuple[int, int]]:
    by_scope: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        reference = position.sourceReference
        if reference is None:
            continue
        file_key = _word_table_key(reference.fileName)
        role = _position_source_role(position)
        if not file_key or role not in {"price_justification", "technical_specification"}:
            continue
        by_scope.setdefault((file_key, role), []).append((index, position))

    options: list[tuple[float, float, tuple[str, str], tuple[str, str]]] = []
    price_scopes = [scope for scope in by_scope if scope[1] == "price_justification"]
    technical_scopes = [
        scope for scope in by_scope if scope[1] == "technical_specification"
    ]
    for price_scope in price_scopes:
        price_rows = by_scope[price_scope]
        for technical_scope in technical_scopes:
            technical_rows = by_scope[technical_scope]
            if len(price_rows) != len(technical_rows) or len(price_rows) < 4:
                continue
            aligned_identity = 0
            aligned_grid = 0
            for (_, price), (_, technical) in zip(price_rows, technical_rows):
                same_quantity = price.quantity == technical.quantity
                same_unit = _position_unit_key(price.unit) == _position_unit_key(
                    technical.unit
                )
                if same_quantity and same_unit:
                    aligned_grid += 1
                    if _position_name_key(price) == _position_name_key(technical):
                        aligned_identity += 1
            identity_ratio = aligned_identity / len(price_rows)
            grid_ratio = aligned_grid / len(price_rows)
            if identity_ratio >= 0.75 and grid_ratio >= 0.90:
                options.append(
                    (identity_ratio, grid_ratio, price_scope, technical_scope)
                )

    pairs: list[tuple[int, int]] = []
    used_price_scopes: set[tuple[str, str]] = set()
    used_technical_scopes: set[tuple[str, str]] = set()
    for _, _, price_scope, technical_scope in sorted(options, reverse=True):
        if (
            price_scope in used_price_scopes
            or technical_scope in used_technical_scopes
        ):
            continue
        used_price_scopes.add(price_scope)
        used_technical_scopes.add(technical_scope)
        pairs.extend(
            (price_entry[0], technical_entry[0])
            for price_entry, technical_entry in zip(
                by_scope[price_scope],
                by_scope[technical_scope],
            )
        )
    return pairs


def _aligned_replicated_table_pairs(
    positions: list[TenderPosition],
    skipped_indexes: set[int],
) -> list[tuple[int, int]]:
    """Pair full table copies across documents without collapsing rows in one table."""
    by_scope: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes:
            continue
        reference = position.sourceReference
        if reference is None:
            continue
        file_name = _word_table_key(reference.fileName) or "__single_document__"
        table_scope = _word_table_key(reference.sheet or reference.table)
        if not table_scope:
            continue
        by_scope.setdefault((file_name, table_scope), []).append((index, position))

    scopes = list(by_scope)
    options: list[tuple[float, float, tuple[str, str], tuple[str, str]]] = []
    for left_offset, left_scope in enumerate(scopes):
        left_rows = by_scope[left_scope]
        is_identified_single_row = (
            len(left_rows) == 1
            and bool(re.search(r"\d", _position_name_key(left_rows[0][1])))
        )
        if len(left_rows) < 2 and not is_identified_single_row:
            continue
        for right_scope in scopes[left_offset + 1 :]:
            right_rows = by_scope[right_scope]
            if len(left_rows) != len(right_rows):
                continue
            aligned_grid = 0
            strong_identity = 0
            for (_, left), (_, right) in zip(left_rows, right_rows):
                if left.quantity != right.quantity or _position_unit_key(left.unit) != _position_unit_key(right.unit):
                    continue
                aligned_grid += 1
                left_identity = _power_transformer_identity(left)
                right_identity = _power_transformer_identity(right)
                left_stems = _product_name_stems(left)
                right_stems = _product_name_stems(right)
                exact_model = bool(
                    _clean(left.model or left.article)
                    and _word_table_key(left.model or left.article)
                    == _word_table_key(right.model or right.article)
                )
                if (
                    _same_replicated_product(left, right)
                    or exact_model
                    or (left_identity is not None and left_identity == right_identity)
                    or len(left_stems & right_stems) >= 2
                ):
                    strong_identity += 1
            grid_ratio = aligned_grid / len(left_rows)
            identity_ratio = strong_identity / len(left_rows)
            # A two-row table must match both rows. Longer tables allow one
            # noisy row, but never rely on quantity/unit alone.
            required_identity = 1.0 if len(left_rows) <= 3 else 0.80
            if grid_ratio >= 0.90 and identity_ratio >= required_identity:
                options.append((identity_ratio, grid_ratio, left_scope, right_scope))

    pairs: list[tuple[int, int]] = []
    used_scopes: set[tuple[str, str]] = set()
    for _, _, left_scope, right_scope in sorted(options, reverse=True):
        if left_scope in used_scopes or right_scope in used_scopes:
            continue
        left_rows = by_scope[left_scope]
        right_rows = by_scope[right_scope]
        left_priority = min(
            source_role_priority(_position_source_role(position))
            for _, position in left_rows
        )
        right_priority = min(
            source_role_priority(_position_source_role(position))
            for _, position in right_rows
        )
        left_first_index = left_rows[0][0]
        right_first_index = right_rows[0][0]
        if (left_priority, left_first_index) <= (right_priority, right_first_index):
            canonical_rows, duplicate_rows = left_rows, right_rows
        else:
            canonical_rows, duplicate_rows = right_rows, left_rows
        pairs.extend(
            (canonical[0], duplicate[0])
            for canonical, duplicate in zip(canonical_rows, duplicate_rows)
        )
        used_scopes.update((left_scope, right_scope))
    return pairs


def _deduplicate_cross_document_positions(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Keep every same-file row, but retain one document's copy of replicated rows."""
    skipped_indexes: set[int] = set()
    warnings: list[str] = []
    aligned_pairs = _aligned_section_duplicate_pairs(positions)
    for canonical_index, duplicate_index in aligned_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index],
            positions[duplicate_index],
        )
        skipped_indexes.add(duplicate_index)
    if aligned_pairs:
        warnings.append(
            "Paired replicated price-justification and technical-specification "
            "rows by aligned position order; retained the price list and merged "
            "technical characteristics."
        )

    table_pairs = _aligned_replicated_table_pairs(positions, skipped_indexes)
    for canonical_index, duplicate_index in table_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index],
            positions[duplicate_index],
        )
        skipped_indexes.add(duplicate_index)
    if table_pairs:
        warnings.append(
            "Merged replicated table copies after aligned row order, quantity, "
            "unit, and stable product identities were confirmed."
        )

    # Some tenders repeat only two or three technical positions. The generic
    # aligned-table safeguard deliberately leaves such short lists alone.
    # For power transformers, however, series plus nominal capacity is a
    # stable identity: it lets us merge copies from price, technical, and
    # contract tables without collapsing different transformer executions.
    transformer_groups: dict[tuple[str, str, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        identity = _power_transformer_identity(position)
        reference = position.sourceReference
        scope = _word_table_key(reference.sheet or reference.table)
        if identity is None or not scope:
            continue
        transformer_groups.setdefault(
            (*identity, position.quantity, _position_unit_key(position.unit)), []
        ).append((index, position))

    transformer_duplicates = 0
    for group in transformer_groups.values():
        scopes = {
            (_word_table_key(position.sourceReference.fileName), _word_table_key(position.sourceReference.sheet or position.sourceReference.table))
            for _, position in group
            if position.sourceReference is not None
        }
        # A repeated item within one source table remains a separate tender row.
        if len(group) < 2 or len(scopes) < 2 or len(scopes) != len(group):
            continue
        canonical_index, canonical = min(
            group,
            key=lambda entry: (
                source_role_priority(_position_source_role(entry[1])),
                entry[0],
            ),
        )
        for duplicate_index, duplicate in group:
            if duplicate_index == canonical_index:
                continue
            canonical = _merge_replicated_position_details(canonical, duplicate)
            positions[canonical_index] = canonical
            skipped_indexes.add(duplicate_index)
            transformer_duplicates += 1
    if transformer_duplicates:
        warnings.append(
            "Merged replicated power-transformer rows by stable series and nominal capacity."
        )

    same_row_groups: dict[tuple[str, int, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        reference = position.sourceReference
        if reference.row is None:
            continue
        same_row_groups.setdefault(
            (
                _word_table_key(reference.fileName),
                reference.row,
                position.quantity,
                _position_unit_key(position.unit),
            ),
            [],
        ).append((index, position))

    same_row_duplicates = 0
    for group in same_row_groups.values():
        if len(group) < 2:
            continue
        canonical_index, canonical = max(
            group,
            key=lambda entry: (
                len(_clean(entry[1].product)),
                len(entry[1].characteristics),
                -entry[0],
            ),
        )
        for duplicate_index, duplicate in group:
            if duplicate_index == canonical_index or not _same_source_row_product_variant(canonical, duplicate):
                continue
            canonical = _merge_replicated_position_details(canonical, duplicate)
            positions[canonical_index] = canonical
            skipped_indexes.add(duplicate_index)
            same_row_duplicates += 1
    if same_row_duplicates:
        warnings.append(
            "Merged repeated product variants extracted from the same source row."
        )

    source_groups: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes or position.sourceReference is None:
            continue
        reference = position.sourceReference
        scope = _word_table_key(reference.sheet or reference.table) or "__document_text__"
        source_groups.setdefault((_word_table_key(reference.fileName), scope), []).append(
            (index, position)
        )

    generic_table_pairs: list[tuple[int, int]] = []
    scopes = list(source_groups)
    consumed_generic_scopes: set[tuple[str, str]] = set()
    for generic_scope in scopes:
        generic_rows = source_groups[generic_scope]
        if generic_scope in consumed_generic_scopes or not generic_rows:
            continue
        if not all(_is_generic_product_copy(position) for _, position in generic_rows):
            continue
        matches: list[tuple[int, tuple[str, str], list[tuple[int, TenderPosition]]]] = []
        for detailed_scope in scopes:
            if detailed_scope == generic_scope:
                continue
            detailed_rows = source_groups[detailed_scope]
            if len(detailed_rows) != len(generic_rows) or not detailed_rows:
                continue
            if all(
                _generic_copy_matches_detail(generic, detailed)
                for (_, generic), (_, detailed) in zip(generic_rows, detailed_rows)
            ):
                specificity = sum(
                    len(_clean(position.product)) + len(position.characteristics) * 80
                    for _, position in detailed_rows
                )
                matches.append((specificity, detailed_scope, detailed_rows))
        if not matches:
            continue
        _, _, detailed_rows = max(matches, key=lambda item: item[0])
        generic_table_pairs.extend(
            (detailed_index, generic_index)
            for (generic_index, _), (detailed_index, _) in zip(generic_rows, detailed_rows)
        )
        consumed_generic_scopes.add(generic_scope)

    for canonical_index, duplicate_index in generic_table_pairs:
        positions[canonical_index] = _merge_replicated_position_details(
            positions[canonical_index], positions[duplicate_index]
        )
        skipped_indexes.add(duplicate_index)
    if generic_table_pairs:
        warnings.append(
            "Merged a generic product-table copy into the matching detailed table."
        )

    grouped: dict[tuple[str, str, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if index in skipped_indexes:
            continue
        key = _cross_document_position_key(position)
        if key is not None:
            grouped.setdefault(key, []).append((index, position))

    for group in grouped.values():
        by_file: dict[str, list[tuple[int, TenderPosition]]] = {}
        for index, position in group:
            file_name = _clean(position.sourceReference.fileName if position.sourceReference else "")
            source_scope = f"{file_name}::{_position_source_role(position)}"
            by_file.setdefault(source_scope, []).append((index, position))
        if len(by_file) < 2:
            continue
        canonical_file = min(
            by_file,
            key=lambda name: (
                min(
                    source_role_priority(_position_source_role(position))
                    for _, position in by_file[name]
                ),
                min(index for index, _ in by_file[name]),
            ),
        )
        canonical_copies = by_file[canonical_file]
        for file_name, copies in by_file.items():
            if file_name == canonical_file:
                continue
            for copy_offset, (_, duplicate) in enumerate(copies):
                if not canonical_copies:
                    break
                canonical_index, canonical = canonical_copies[
                    min(copy_offset, len(canonical_copies) - 1)
                ]
                updated = _merge_replicated_position_details(canonical, duplicate)
                if updated != canonical:
                    group_entry = (canonical_index, updated)
                    canonical_copies[
                        min(copy_offset, len(canonical_copies) - 1)
                    ] = group_entry
                    positions[canonical_index] = updated
            skipped_indexes.update(index for index, _ in copies)
            warnings.append(
                "Skipped replicated product rows from document "
                f"{file_name}; retained {canonical_file}."
            )
    return (
        [position for index, position in enumerate(positions) if index not in skipped_indexes],
        list(dict.fromkeys(warnings)),
    )


_OKPD_PLACEHOLDER_PRODUCT_PATTERN = re.compile(
    r"^\s*\S+(?:\s+\S+){0,4}\s*\(\s*\u043e\u043a\u043f\u0434\s+\d{2}(?:\.\d{1,3}){1,4}\s*\)\s*$",
    re.IGNORECASE,
)
_SURVEY_SHEET_FILE_PATTERN = re.compile(
    r"(?:\b\u043e\u043f\u0440\u043e\u0441\u043d\w*\s+\u043b\u0438\u0441\u0442\b|"
    r"(?:^|\W)\u043e\u043b\s*(?:\u2116|n|#)?\s*\d+)",
    re.IGNORECASE,
)
_TENDER_SUBJECT_PRODUCT_PATTERN = re.compile(
    r"^\s*(?:\u043a\u0442\u043f|\u0441\u0442\u043f|\u043c\u0442\u043f)"
    r"(?:\s*,\s*(?:\u043a\u0442\u043f|\u0441\u0442\u043f|\u043c\u0442\u043f)){1,}"
    r"\s+\u0431\u0435\u0437\s+\u0441\u0438\u043b\u043e\u0432\w*\s+\u0442\u0440\u0430\u043d\u0441\u0444\u043e\u0440\u043c\u0430\u0442\u043e\u0440\w*\s*$",
    re.IGNORECASE,
)


def _is_tender_subject_or_survey_title(position: TenderPosition) -> bool:
    """Reject tender-level labels and LLM guesses from an OL document title."""
    product = _clean(position.product)
    if _TENDER_SUBJECT_PRODUCT_PATTERN.fullmatch(product):
        return True

    reference = position.sourceReference
    return bool(
        position.source == "llm"
        and reference is not None
        and _SURVEY_SHEET_FILE_PATTERN.search(_clean(reference.fileName))
        and not _clean(reference.productColumn)
        and not _clean(position.model)
        and not _clean(position.article)
        and not position.sourceCells
    )


def _is_blank_okpd_template_position(position: TenderPosition) -> bool:
    """Recognize an LLM label inferred from a blank offer-form product cell."""
    reference = position.sourceReference
    return bool(
        position.source == "llm"
        and reference is not None
        and reference.row is not None
        and _position_source_role(position) in {OTHER_ROLE, COMPOSITE_ROLE}
        and _OKPD_PLACEHOLDER_PRODUCT_PATTERN.fullmatch(_clean(position.product))
        and not _clean(position.model)
        and not _clean(position.article)
        and not position.characteristics
        and not _clean(position.requirements)
        and not position.sourceCells
    )


def _contiguous_template_groups(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    grouped: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        if not _is_blank_okpd_template_position(position):
            continue
        reference = position.sourceReference
        if reference is None:
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.table) or "__document__",
        )
        grouped.setdefault(key, []).append((index, position))

    result: list[list[tuple[int, TenderPosition]]] = []
    for entries in grouped.values():
        current: list[tuple[int, TenderPosition]] = []
        previous_row: int | None = None
        for entry in entries:
            row = entry[1].sourceReference.row if entry[1].sourceReference else None
            if current and (row is None or previous_row is None or row != previous_row + 1):
                result.append(current)
                current = []
            current.append(entry)
            previous_row = row
        if current:
            result.append(current)
    return result


def _technical_position_groups(
    positions: list[TenderPosition],
) -> list[list[TenderPosition]]:
    grouped: dict[tuple[str, str], list[TenderPosition]] = {}
    for position in positions:
        reference = position.sourceReference
        if (
            reference is None
            or reference.row is None
            or _position_source_role(position) != TECHNICAL_ROLE
            or _is_blank_okpd_template_position(position)
        ):
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.table) or "__document__",
        )
        grouped.setdefault(key, []).append(position)
    return [
        sorted(group, key=lambda position: position.sourceReference.row or 0)
        for group in grouped.values()
    ]


def _drop_blank_okpd_template_positions(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Drop an aligned blank offer form when a detailed technical table exists."""
    skipped_indexes: set[int] = set()
    technical_groups = _technical_position_groups(positions)
    for template_group in _contiguous_template_groups(positions):
        if len(template_group) < 4:
            continue
        template_grid = [
            (position.quantity, _position_unit_key(position.unit))
            for _, position in template_group
        ]
        matching_group = next(
            (
                technical_group
                for technical_group in technical_groups
                if len(technical_group) == len(template_group)
                and [
                    (position.quantity, _position_unit_key(position.unit))
                    for position in technical_group
                ]
                == template_grid
            ),
            None,
        )
        if matching_group is not None:
            skipped_indexes.update(index for index, _ in template_group)

    if not skipped_indexes:
        return positions, []
    return (
        [position for index, position in enumerate(positions) if index not in skipped_indexes],
        [
            "Skipped a blank offer-form table inferred only from OKPD codes after "
            "an aligned technical specification confirmed the detailed product rows."
        ],
    )


def _apparel_family_key(position: TenderPosition) -> str:
    """Recognize a clothing family without treating its size grid as new goods."""
    value = _word_table_key(position.productQuery or position.product)
    if "\u043a\u043e\u0441\u0442\u044e\u043c" in value:
        product_type = "suit"
    elif "\u043a\u0443\u0440\u0442\u043a" in value:
        product_type = "jacket"
    else:
        return ""
    if "\u043c\u0443\u0436" in value:
        gender = "male"
    elif "\u0436\u0435\u043d" in value:
        gender = "female"
    else:
        return ""
    return f"{product_type}:{gender}:winter" if "\u0437\u0438\u043c" in value else ""


def _drop_unconfirmed_llm_quantity_breakdowns(
    llm_products: list[TenderPosition],
    source_backed_positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Discard an LLM-only clothing size breakdown when a table already has its total."""
    backed_by_family: dict[str, list[TenderPosition]] = {}
    for position in source_backed_positions:
        if not _position_source_key(position) or position.quantity is None:
            continue
        family = _apparel_family_key(position)
        if family:
            backed_by_family.setdefault(family, []).append(position)

    unconfirmed_by_family: dict[str, list[TenderPosition]] = {}
    for position in llm_products:
        if _position_source_key(position) or position.quantity is None:
            continue
        family = _apparel_family_key(position)
        if family:
            unconfirmed_by_family.setdefault(family, []).append(position)

    removed_ids: set[int] = set()
    warnings: list[str] = []
    for family, candidates in unconfirmed_by_family.items():
        total = sum(float(position.quantity or 0) for position in candidates)
        parent = next(
            (
                position
                for position in backed_by_family.get(family, [])
                if abs(float(position.quantity or 0) - total) < 1e-9
            ),
            None,
        )
        if parent is None or len(candidates) < 2:
            continue
        removed_ids.update(id(position) for position in candidates)
        warnings.append(
            "Skipped unreferenced LLM size breakdown of a source-backed position: "
            f"{parent.product[:160]} ({parent.quantity:g} = {total:g})."
        )
    return (
        [position for position in llm_products if id(position) not in removed_ids],
        warnings,
    )


def _position_table_identity(position: TenderPosition) -> tuple[str, float | None, str]:
    return (
        _position_name_key(position),
        position.quantity,
        _position_unit_key(position.unit),
    )


def _source_backed_table_groups(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    grouped: dict[tuple[str, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        reference = position.sourceReference
        if (
            reference is None
            or reference.row is None
            or not _clean(reference.fileName)
            or not (reference.sheet or reference.table)
        ):
            continue
        key = (
            _word_table_key(reference.fileName),
            _word_table_key(reference.sheet or reference.table),
        )
        grouped.setdefault(key, []).append((index, position))
    return [
        sorted(group, key=lambda entry: entry[1].sourceReference.row or 0)
        for group in grouped.values()
    ]


def _unreferenced_llm_runs(
    positions: list[TenderPosition],
) -> list[list[tuple[int, TenderPosition]]]:
    runs: list[list[tuple[int, TenderPosition]]] = []
    current: list[tuple[int, TenderPosition]] = []
    for index, position in enumerate(positions):
        if _position_source_key(position):
            if current:
                runs.append(current)
                current = []
            continue
        current.append((index, position))
    if current:
        runs.append(current)
    return runs


def _drop_unreferenced_llm_table_copies(
    deterministic: list[TenderPosition],
    llm_products: list[TenderPosition],
) -> tuple[list[TenderPosition], list[TenderPosition], list[str]]:
    """Remove a complete, source-less LLM copy of a structured table.

    The LLM can emit a valid copy of a table after unrelated document-level
    candidates. Whole-list alignment then cannot recognize it. Requiring a
    complete table, source-row ordering, and a four-row minimum avoids merging
    genuine repeated purchase positions inside the table.
    """
    removed_indexes: set[int] = set()
    warnings: list[str] = []
    for source_group in _source_backed_table_groups(deterministic):
        if len(source_group) < 4:
            continue
        source_identities = [
            _position_table_identity(position) for _, position in source_group
        ]
        if not all(identity[0] for identity in source_identities):
            continue
        for llm_run in _unreferenced_llm_runs(llm_products):
            if len(llm_run) < len(source_group):
                continue
            llm_identities = [
                _position_table_identity(position) for _, position in llm_run
            ]
            for start in range(len(llm_run) - len(source_group) + 1):
                end = start + len(source_group)
                if llm_identities[start:end] != source_identities:
                    continue
                for (source_index, source_position), (_, llm_position) in zip(
                    source_group, llm_run[start:end]
                ):
                    deterministic[source_index] = _merge_replicated_position_details(
                        source_position,
                        llm_position,
                    )
                removed_indexes.update(index for index, _ in llm_run[start:end])
                reference = source_group[0][1].sourceReference
                warnings.append(
                    "Skipped an unreferenced LLM copy of structured table rows; "
                    f"retained {reference.fileName if reference else 'source-backed'} rows."
                )
                break

    if not removed_indexes:
        return deterministic, llm_products, []
    return (
        deterministic,
        [
            position
            for index, position in enumerate(llm_products)
            if index not in removed_indexes
        ],
        list(dict.fromkeys(warnings)),
    )


def _merge_aligned_llm_table_copy(
    deterministic: list[TenderPosition],
    llm_products: list[TenderPosition],
) -> tuple[list[TenderPosition], list[TenderPosition], list[str]]:
    if len(deterministic) != len(llm_products) or len(deterministic) < 4:
        return deterministic, llm_products, []

    aligned_identity = 0
    aligned_grid = 0
    for source_position, llm_position in zip(deterministic, llm_products):
        same_quantity = source_position.quantity == llm_position.quantity
        same_unit = _word_table_key(source_position.unit) == _word_table_key(
            llm_position.unit
        )
        if same_quantity and same_unit:
            aligned_grid += 1
            if _position_name_key(source_position) == _position_name_key(llm_position):
                aligned_identity += 1
    identity_ratio = aligned_identity / len(deterministic)
    grid_ratio = aligned_grid / len(deterministic)
    if identity_ratio < 0.75 or grid_ratio < 0.90:
        return deterministic, llm_products, []

    merged = [
        _merge_replicated_position_details(source_position, llm_position)
        for source_position, llm_position in zip(deterministic, llm_products)
    ]
    return (
        merged,
        [],
        [
            "Skipped an aligned LLM copy of deterministic table rows; "
            "retained deterministic product identities and merged details."
        ],
    )


def merge_positions(
    deterministic: list[TenderPosition],
    llm_response: TenderPositionsResponse | None,
    seldon: list[TenderPosition] | None = None,
    max_positions: int = 5_000,
) -> tuple[list[TenderPosition], list[str]]:
    seldon = list(seldon or [])
    llm_products = list(llm_response.products if llm_response else [])
    llm_products, breakdown_warnings = _drop_unconfirmed_llm_quantity_breakdowns(
        llm_products,
        deterministic + seldon + llm_products,
    )
    deterministic, llm_products, unreferenced_copy_warnings = (
        _drop_unreferenced_llm_table_copies(deterministic, llm_products)
    )
    deterministic, llm_products, aligned_copy_warnings = (
        _merge_aligned_llm_table_copy(deterministic, llm_products)
    )
    llm_product_ids = {id(position) for position in llm_products}
    combined = llm_products + seldon + deterministic
    warnings = (
        list(llm_response.warnings if llm_response else [])
        + breakdown_warnings
        + unreferenced_copy_warnings
        + aligned_copy_warnings
    )
    seldon_by_source = {
        key: position
        for position in seldon
        if (key := _position_source_key(position))
    }
    excel_by_source = {
        key: position
        for position in deterministic
        if (key := _position_source_key(position))
    }
    if seldon and not any(position.quantity is not None for position in seldon):
        warnings.append(
            "Товарные позиции найдены в структурированных данных Seldon, но количество в них отсутствует."
        )
    result: list[TenderPosition] = []
    seen: dict[str, int] = {}
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
        source_key = _position_source_key(position)
        seldon_match = seldon_by_source.get(source_key)
        excel_match = excel_by_source.get(source_key)
        if (
            id(raw_position) in llm_product_ids
            and position.quantity is None
            and (deterministic or seldon)
            and seldon_match is None
            and excel_match is None
        ):
            warnings.append(
                "Skipped LLM product without quantity that was not confirmed by structured product rows: "
                f"{_clean(position.product)[:200]}"
            )
            continue
        if (
            id(raw_position) in llm_product_ids
            and (deterministic or seldon)
            and not source_key
            and not _clean(position.unit)
            and len(_position_name_key(position).split()) <= 2
            and not _clean(position.model)
            and not _clean(position.article)
            and not position.characteristics
        ):
            warnings.append(
                "Skipped unreferenced LLM category without a unit when structured "
                f"product rows are available: {_clean(position.product)[:200]}"
            )
            continue
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
        # Equal labels remain separate purchase positions unless they point to
        # the same source row. This preserves the coverage denominator.
        resolved_key = source_key or f"occurrence:{id(raw_position)}"
        existing_index = seen.get(resolved_key)
        if existing_index is not None:
            existing = result[existing_index]
            deterministic_replaces_llm = (
                position.source == "excel_table_deterministic"
                and existing.source != "excel_table_deterministic"
            )
            merged_existing = _merge_replicated_position_details(
                position if deterministic_replaces_llm else existing,
                existing if deterministic_replaces_llm else position,
            )
            if merged_existing != existing:
                result[existing_index] = merged_existing
                existing = merged_existing
            updates: dict[str, Any] = {}
            if existing.quantity is None and quantity is not None:
                updates["quantity"] = quantity
            if not existing.unit and unit:
                updates["unit"] = unit
            for field in (
                "requirements",
                "characteristics",
                "positionKey",
                "lotNumber",
                "positionNumber",
                "model",
                "documentUnitPriceRub",
                "documentLineTotalRub",
                "documentCurrency",
                "documentPriceEvidence",
                "documentPriceSource",
                "sourceReference",
                "sourceCells",
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
            seen[resolved_key] = existing_index
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
        if len(result) >= max_positions:
            break
    result, blank_template_warnings = _drop_blank_okpd_template_positions(result)
    result, cross_document_warnings = _deduplicate_cross_document_positions(result)
    return result, warnings + blank_template_warnings + cross_document_warnings
