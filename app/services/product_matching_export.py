from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


HEADERS = [
    "N",
    "Название товара в тендере",
    "Наименование товара ЭТМ",
    "Код ЭТМ",
    "Ссылка ЭТМ",
    "Производитель",
    "Количество",
    "Медианная цена",
    "Валюта",
    "Сумма",
    "Характеристики из документации",
    "Характеристики выбранного товара ЭТМ",
    "Обоснование (почему товар подобран)",
    "Корректность подбора",
    "Примечание о подборе",
]

MATCHING_NOTE_HINT = "В случае если товар подобран не верно, указать что именно"


def _value(value: Any) -> Any:
    if value is None:
        return ""
    return value


def _export_cell_value(value: Any) -> Any:
    """Return a spreadsheet-safe value without changing the source result."""
    if isinstance(value, str):
        return value.replace(";", ":")
    return value


def _characteristics_label(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        characteristic_value = str(item.get("value") or "").strip()
        label = ": ".join(part for part in (name, characteristic_value) if part)
        if label:
            result.append(label)
    return "\n".join(result)


def _catalog_characteristics_label(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return "\n".join(
        f"{key}: {item}"
        for key, item in value.items()
        if str(key).strip() and item is not None and str(item).strip()
    )


def build_product_matching_workbook(
    product_check: dict[str, Any],
    *,
    selected_catalog_params: list[dict[str, Any]] | None = None,
) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Автоподбор"
    worksheet.append([_export_cell_value(value) for value in HEADERS])

    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill

    details = product_check.get("details")
    if not isinstance(details, list):
        details = []

    first_exported_position = True
    for index, detail in enumerate(details, start=1):
        if not isinstance(detail, dict):
            continue
        result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
        catalog_params = (
            selected_catalog_params[index - 1]
            if selected_catalog_params is not None and index <= len(selected_catalog_params)
            else {}
        )
        article = detail.get("article") or result.get("Артикул") or ""
        link = detail.get("link") or result.get("Ссылка") or ""
        product_id = detail.get("productId") or result.get("ID товара") or article
        worksheet.append(
            [
                _export_cell_value(value)
                for value in [
                    index,
                    _value(detail.get("sourceProduct") or detail.get("productQuery")),
                    _value(result.get("Наименование") or ""),
                    _value(product_id),
                    _value(link),
                    _value(result.get("Производитель") or ""),
                    _value(detail.get("quantity")),
                    _value(detail.get("medianUnitPriceRub")),
                    _value(detail.get("priceCurrency") or result.get("Валюта")),
                    _value(detail.get("positionTotalPriceRub")),
                    _characteristics_label(detail.get("sourceCharacteristics")),
                    _catalog_characteristics_label(catalog_params),
                    _value(result.get("Обоснование") or ""),
                    "",
                    MATCHING_NOTE_HINT if first_exported_position else "",
                ]
            ]
        )
        first_exported_position = False

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    widths = [
        8, 48, 52, 16, 34, 24, 14, 16, 10, 16, 65, 65, 72, 28, 58,
    ]
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width

    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
