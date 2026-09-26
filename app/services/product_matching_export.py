from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


HEADERS = [
    "N",
    "Название товара тендера",
    "Количество",
    "Ед. изм.",
    "Артикул",
    "Ссылка",
    "Код ETM",
    "Наименование ETM",
    "Производитель",
    "Медианная цена",
    "Валюта",
    "Сумма позиции",
    "Соответствие",
    "Обоснование",
    "Источник",
    "Position key",
    "Характеристики из документов",
    "Итоговый поисковый запрос",
    "Выбранные search_token",
    "Категория поиска",
    "Варианты Qdrant-запроса",
    "Конфликты характеристик",
]


def _value(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _source_label(detail: dict[str, Any]) -> str:
    reference = detail.get("sourceReference")
    if not isinstance(reference, dict):
        return ""
    parts = [
        str(reference.get("fileName") or "").strip(),
        str(reference.get("sheet") or "").strip(),
        f"row {reference.get('row')}" if reference.get("row") else "",
    ]
    return " / ".join(part for part in parts if part)


def _characteristics_label(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        characteristic_value = str(item.get("value") or "").strip()
        method = str(item.get("associationMethod") or "").strip()
        confidence = item.get("associationConfidence")
        label = ": ".join(part for part in (name, characteristic_value) if part)
        association = ", ".join(
            part
            for part in (
                method,
                f"{float(confidence):.2f}" if isinstance(confidence, (int, float)) else "",
            )
            if part
        )
        if label:
            result.append(f"{label} [{association}]" if association else label)
    return "\n".join(result)


def _list_label(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "\n".join(str(item).strip() for item in value if str(item).strip())


def _conflicts_label(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("normalizedName") or "").strip()
        values = item.get("values")
        rendered_values = []
        if isinstance(values, list):
            rendered_values = [
                str(entry.get("value") or "").strip()
                for entry in values
                if isinstance(entry, dict) and str(entry.get("value") or "").strip()
            ]
        result.append(f"{name}: {' <> '.join(rendered_values)}")
    return "\n".join(result)


def build_product_matching_workbook(product_check: dict[str, Any]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Автоподбор"
    worksheet.append(HEADERS)

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill

    details = product_check.get("details")
    if not isinstance(details, list):
        details = []

    for index, detail in enumerate(details, start=1):
        if not isinstance(detail, dict):
            continue
        result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
        article = detail.get("article") or result.get("Артикул") or ""
        link = detail.get("link") or result.get("Ссылка") or ""
        product_id = detail.get("productId") or result.get("ID товара") or article
        worksheet.append(
            [
                index,
                _value(detail.get("sourceProduct") or detail.get("productQuery")),
                _value(detail.get("quantity")),
                _value(detail.get("unit")),
                _value(article),
                _value(link),
                _value(product_id),
                _value(result.get("Наименование") or ""),
                _value(result.get("Производитель") or ""),
                _value(detail.get("medianUnitPriceRub")),
                _value(detail.get("priceCurrency") or result.get("Валюта")),
                _value(detail.get("positionTotalPriceRub")),
                _value(result.get("Соответствие") or ""),
                _value(result.get("Обоснование") or ""),
                _source_label(detail),
                _value(detail.get("positionKey")),
                _characteristics_label(detail.get("sourceCharacteristics")),
                _value(detail.get("productQuery")),
                _list_label(detail.get("searchCharacteristics")),
                _value(
                    detail.get("searchCategoryCode")
                    or detail.get("searchCategory")
                ),
                _list_label(detail.get("searchQueries")),
                _conflicts_label(detail.get("characteristicConflicts")),
            ]
        )

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    widths = [
        8, 48, 14, 12, 16, 34, 16, 52, 24, 16, 10, 16, 22, 72, 40,
        24, 65, 65, 36, 28, 65, 60,
    ]
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width

    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
