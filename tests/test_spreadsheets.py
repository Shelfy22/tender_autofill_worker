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
