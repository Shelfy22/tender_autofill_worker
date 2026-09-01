from pathlib import Path

from openpyxl import Workbook

from app.config import Settings
from app.services.parsers.spreadsheets import extract_spreadsheet_text
from app.services.products import extract_deterministic_positions


def test_xlsx_streaming_text_preserves_column_addresses_and_quantity(tmp_path: Path) -> None:
    path = tmp_path / "specification.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Спецификация"
    sheet.append(["№ п/п", "Наименование товара", "Характеристика", "Ед. изм.", "Количество"])
    sheet.append([1, "Кабель силовой", None, "шт", 12])
    workbook.save(path)

    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        max_text_chars_per_file=10_000,
    )
    text, status, warnings = extract_spreadsheet_text(path, "xlsx", settings)

    assert status == "ok"
    assert not warnings
    assert "Лист: Спецификация" in text
    assert "Строка 2: A: 1 | B: Кабель силовой | D: шт | E: 12" in text
    positions = extract_deterministic_positions(text)
    assert [(item.product, item.unit, item.quantity) for item in positions] == [
        ("Кабель силовой", "шт", 12.0)
    ]


def test_low_confidence_xlsx_text_is_not_discarded(tmp_path: Path) -> None:
    path = tmp_path / "short.xlsx"
    workbook = Workbook()
    workbook.active.append(["ABC", 123])
    workbook.save(path)

    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        max_text_chars_per_file=10_000,
    )
    text, status, warnings = extract_spreadsheet_text(path, "xlsx", settings)

    assert "A: ABC | B: 123" in text
    assert status == "spreadsheet_low_confidence"
    assert warnings


def test_xlsx_prices_are_extracted_by_headers_with_source_coordinates(tmp_path: Path) -> None:
    path = tmp_path / "priced_specification.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Спецификация"
    sheet.append(
        [
            "№ п/п",
            "Наименование товара",
            "Ед. изм.",
            "Количество",
            "Цена за единицу, руб.",
            "Стоимость позиции, руб.",
        ]
    )
    sheet.append([1, "Стеллаж Универсал", "шт", 10, "15 707,43", "157 074,30"])
    workbook.save(path)

    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        max_text_chars_per_file=10_000,
    )
    text, status, warnings = extract_spreadsheet_text(path, "xlsx", settings)
    positions = extract_deterministic_positions(
        "--- ДОКУМЕНТ 1 ---\nfileName: priced_specification.xlsx\n" + text
    )

    assert status == "ok"
    assert not warnings
    assert len(positions) == 1
    position = positions[0]
    assert position.documentUnitPriceRub == 15_707.43
    assert position.documentLineTotalRub == 157_074.30
    assert position.documentCurrency == "RUB"
    assert position.documentPriceSource is not None
    assert position.documentPriceSource.fileName == "priced_specification.xlsx"
    assert position.documentPriceSource.sheet == "Спецификация"
    assert position.documentPriceSource.row == 2
    assert position.documentPriceSource.unitPriceColumn == "E"
    assert position.documentPriceSource.lineTotalColumn == "F"
    assert position.documentPriceSource.extractionMethod == "excel_deterministic"
    assert position.sourceReference is not None
    assert position.sourceReference.fileName == "priced_specification.xlsx"
    assert position.sourceReference.sheet == "Спецификация"
    assert position.sourceReference.row == 2
    assert position.sourceReference.productColumn == "B"
    assert position.sourceReference.unitColumn == "C"
    assert position.sourceReference.quantityColumn == "D"
    assert position.sourceReference.extractionMethod == "excel_deterministic"
