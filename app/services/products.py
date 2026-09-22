from __future__ import annotations

import json
import re
from typing import Any

from app.models import (
    DocumentPriceSource,
    ProductSourceReference,
    SpreadsheetRow,
    SpreadsheetTable,
    TenderPosition,
    TenderPositionsResponse,
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


UNITS = r"штука|штук|шт\.?|комплект|компл\.?|набор|ед\.?|метр|м|кг|л|упак\.?"


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("undefined", " ")).strip()


def _strip_classifier_suffix(value: str) -> str:
    return _clean(_CLASSIFIER_SUFFIX_PATTERN.sub("", value))


def _normalize_extracted_product_name(value: str) -> str:
    value = _strip_classifier_suffix(_clean(value))
    return _clean(_EQUIVALENT_SUFFIX_PATTERN.sub("", value))


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
        r"(?:общее\s+)?(?:количество|кол\s+во|кол)"
        r"(?:\s+(?:товара|продукции|изделий|единиц))?",
        header,
    ):
        return "quantity"
    if re.search(r"(?:количество|кол\s+во|кол)\s+(?:шт|штук|ед|м|кг|л)\b", header):
        return "quantity"
    if re.search(r"единица\s+измерения|ед\s+изм", header):
        return "unit"
    if re.search(
        r"^(?:наименование|название)(?:\s|$)|^товар$|^предмет\s+закупки$",
        header,
    ):
        return "product"
    return None


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
    return product_column, characteristic_columns


def _enrich_structured_table_positions_with_characteristics(
    text: str,
    positions: list[TenderPosition],
) -> list[TenderPosition]:
    """Attach a companion table's requirements to its numbered product rows."""
    tables: list[tuple[str, int, list[dict[str, str]]]] = []
    current_type = ""
    current_number: int | None = None
    current_rows: list[dict[str, str]] = []
    for line in text.splitlines():
        marker = _STRUCTURED_TABLE_MARKER_PATTERN.match(line)
        if marker:
            if current_number is not None:
                tables.append((current_type, current_number, current_rows))
            current_type = marker.group(1)
            current_number = int(marker.group(2))
            current_rows = []
            continue
        row = _WORD_TABLE_ROW_PATTERN.match(line)
        if current_number is not None and row:
            cells = _parse_structured_cells(row.group(2))
            if cells:
                current_rows.append(cells)
    if current_number is not None:
        tables.append((current_type, current_number, current_rows))

    requirements_by_key: dict[tuple[str, str], tuple[str, str]] = {}
    requirements_by_name: dict[str, list[tuple[str, str]]] = {}
    for table_type, table_number, rows in tables:
        if len(rows) < 2:
            continue
        product_column, characteristic_columns = _word_table_characteristic_columns(rows[0])
        if not product_column or not characteristic_columns:
            continue
        for cells in rows[1:]:
            product = _clean(cells.get(product_column))
            requirements = _clean(
                " ".join(cells.get(column, "") for column in characteristic_columns)
            )
            serial = _word_table_serial(cells)
            product_key = _word_table_key(product)
            if not product_key or not requirements:
                continue
            source = (
                f"\u0422\u0430\u0431\u043b\u0438\u0446\u0430 {table_type} {table_number}: "
                f"{requirements}"
            )
            if serial:
                requirements_by_key[(product_key, serial)] = (requirements, source)
            requirements_by_name.setdefault(product_key, []).append((requirements, source))

    if not requirements_by_key and not requirements_by_name:
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
        if linked is None:
            enriched.append(position)
            continue
        requirements, source = linked
        combined_requirements = _clean(" ".join((position.requirements, requirements)))
        query = _clean(position.productQuery or position.product)
        if requirements.casefold() not in query.casefold():
            query = _clean(
                f"{query}; \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438: {requirements}"
            )
        evidence = _clean("\n".join((position.evidence, source)))[:500]
        enriched.append(
            position.model_copy(
                update={
                    "productQuery": query,
                    "requirements": combined_requirements,
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


def extract_deterministic_positions(
    text: str,
    spreadsheet_tables: list[SpreadsheetTable] | None = None,
    max_positions: int = 5_000,
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
    ) -> None:
        name, unit = _normalize_extracted_product_name(name), _clean(unit)
        quantity = parse_quantity(raw_quantity)
        if (
            quantity is None
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
            )
        )

    for raw_table in spreadsheet_tables or []:
        try:
            table = (
                raw_table
                if isinstance(raw_table, SpreadsheetTable)
                else SpreadsheetTable.model_validate(raw_table)
            )
        except Exception:
            continue
        header_columns = dict(table.headerMap)
        header_labels = dict(table.headerLabels)
        header_rows = set(table.headerRows)
        use_precomputed_headers = {"product", "quantity"}.issubset(header_columns)
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
                    for role, (column, label) in detected_headers.items():
                        header_columns[role] = column
                        header_labels[role] = label
                    continue
            if not {"product", "quantity"}.issubset(header_columns):
                continue
            product_column = header_columns["product"]
            quantity_column = header_columns["quantity"]
            unit, unit_column = _table_unit(cells, header_columns, header_labels)
            name = cells.get(product_column, "")
            raw_quantity = cells.get(quantity_column)
            if not name or not unit or raw_quantity is None:
                continue
            unit_price_column = header_columns.get("unit_price", "")
            line_total_column = header_columns.get("line_total", "")
            raw_unit_price = cells.get(unit_price_column) if unit_price_column else None
            raw_line_total = cells.get(line_total_column) if line_total_column else None
            has_document_price = (
                raw_unit_price not in {None, ""}
                or raw_line_total not in {None, ""}
            )
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
                    row=row.row,
                    productColumn=product_column,
                    quantityColumn=quantity_column,
                    unitColumn=unit_column,
                    productHeader=header_labels.get("product", ""),
                    quantityHeader=header_labels.get("quantity", ""),
                    unitHeader=header_labels.get("unit", ""),
                    extractionMethod="excel_deterministic",
                ),
                source_cells=cells,
            )
            if len(result) >= max_positions:
                return result

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
        if line.startswith(("Таблица Word ", "Таблица PDF ", "Таблица RTF ")):
            current_sheet = line.strip()
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
        core_header_roles = set(detected_headers) & {
            "product",
            "unit",
            "quantity",
            "unit_price",
            "line_total",
        }
        is_header_row = "product" in detected_headers or len(core_header_roles) >= 2
        if is_header_row:
            for role, (column, label) in detected_headers.items():
                header_columns[role] = column
                header_labels[role] = label
            continue
        if not {"product", "quantity"}.issubset(header_columns):
            continue
        product_column = header_columns["product"]
        quantity_column = header_columns["quantity"]
        unit, unit_column = _table_unit(cells, header_columns, header_labels)
        name = cells.get(product_column, "")
        raw_quantity = cells.get(quantity_column)
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
                extractionMethod="excel_deterministic",
            ),
        )
        if len(result) >= max_positions:
            return result

    # Structured row emitted by the spreadsheet parser:
    # "Строка 2: A: 1 | B: Кабель | D: шт | E: 10".
    for row_match in re.finditer(r"^Строка\s+\d+\s*:\s*(.+)$", text, re.I | re.M):
        if _clean(row_match.group(0)) in structured_spreadsheet_rows:
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

    fallback_text = "\n".join(
        line
        for line in text.splitlines()
        if not _looks_like_structured_row(line)
    )
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
    return _enrich_structured_table_positions_with_characteristics(text, result)


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
    value = _clean(position.productQuery or position.product).lower().replace("ё", "е")
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
    if position.candidateId:
        return f"candidate:{position.candidateId}"
    reference = position.sourceReference
    if reference is None or reference.row is None:
        return ""
    return ":".join(
        (
            "source",
            reference.fileName,
            reference.sheet,
            str(reference.row),
            reference.productColumn,
        )
    )


def _is_nmck_source_name(value: Any) -> bool:
    return "\u043d\u043c\u0446" in _word_table_key(value)


def _cross_document_position_key(position: TenderPosition) -> tuple[str, str, float | None, str] | None:
    reference = position.sourceReference
    if reference is None or not _clean(reference.fileName):
        return None
    product = _position_name_key(position)
    if not product:
        return None
    return (
        product,
        _word_table_key(position.requirements),
        position.quantity,
        _word_table_key(position.unit),
    )


def _deduplicate_cross_document_positions(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str]]:
    """Keep every same-file row, but retain one document's copy of replicated rows."""
    grouped: dict[tuple[str, str, float | None, str], list[tuple[int, TenderPosition]]] = {}
    for index, position in enumerate(positions):
        key = _cross_document_position_key(position)
        if key is not None:
            grouped.setdefault(key, []).append((index, position))

    skipped_indexes: set[int] = set()
    warnings: list[str] = []
    for group in grouped.values():
        by_file: dict[str, list[tuple[int, TenderPosition]]] = {}
        for index, position in group:
            file_name = _clean(position.sourceReference.fileName if position.sourceReference else "")
            by_file.setdefault(file_name, []).append((index, position))
        if len(by_file) < 2:
            continue
        canonical_file = min(
            by_file,
            key=lambda name: (not _is_nmck_source_name(name), min(index for index, _ in by_file[name])),
        )
        for file_name, copies in by_file.items():
            if file_name == canonical_file:
                continue
            skipped_indexes.update(index for index, _ in copies)
            warnings.append(
                "Skipped replicated product rows from document "
                f"{file_name}; retained {canonical_file}."
            )
    return (
        [position for index, position in enumerate(positions) if index not in skipped_indexes],
        list(dict.fromkeys(warnings)),
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
    llm_product_ids = {id(position) for position in llm_products}
    combined = llm_products + seldon + deterministic
    warnings = list(llm_response.warnings if llm_response else []) + breakdown_warnings
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
    result, cross_document_warnings = _deduplicate_cross_document_positions(result)
    return result, warnings + cross_document_warnings
