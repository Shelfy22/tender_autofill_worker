from pathlib import Path

import fitz
import pytest

from app.config import Settings
from app.services.parsers.pdf import extract_pdf_text
from app.services.products import extract_deterministic_positions


def _cyrillic_font() -> Path | None:
    for path in (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ):
        if path.exists():
            return path
    return None


class _OcrMustNotRun:
    def ocr_pdf(self, path: Path) -> str:
        raise AssertionError(f"OCR must not be called for native text PDF: {path}")


def test_short_meaningful_native_pdf_text_does_not_use_ocr(tmp_path: Path) -> None:
    path = tmp_path / "short-text.pdf"
    document = fitz.open()
    page = document.new_page()
    expected = "Marketing research documentation is not published in the system."
    page.insert_text((72, 72), expected)
    document.save(path)
    document.close()

    settings = Settings(postgres_dsn="postgresql://test:test@localhost/test")
    text, status, warnings = extract_pdf_text(path, settings, _OcrMustNotRun())

    assert expected in text
    assert status == "ok"
    assert warnings == []


def test_pdf_table_rows_are_emitted_as_structured_cells(tmp_path: Path) -> None:
    font_path = _cyrillic_font()
    if font_path is None:
        pytest.skip("Cyrillic PDF font is not available")
    path = tmp_path / "table.pdf"
    document = fitz.open()
    page = document.new_page(width=500, height=300)
    page.insert_font(fontname="testfont", fontfile=str(font_path))
    x0, y0 = 50, 50
    widths = [40, 260, 80]
    heights = [30, 30, 30]
    xs = [x0]
    for width in widths:
        xs.append(xs[-1] + width)
    ys = [y0]
    for height in heights:
        ys.append(ys[-1] + height)
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    rows = [
        ["№", "Наименование", "Кол-во/шт."],
        ["1", "Выключатель автоматический ВА 47-60М 1п 3А С", "1"],
        ["2", "Выключатель автоматический ВА 47-60М3п 25А С", "1"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            page.insert_text(
                (xs[column_index] + 3, ys[row_index] + 18),
                value,
                fontsize=8,
                fontname="testfont",
            )
    document.save(path)
    document.close()

    settings = Settings(postgres_dsn="postgresql://test:test@localhost/test")
    text, status, warnings = extract_pdf_text(path, settings, _OcrMustNotRun())
    positions = extract_deterministic_positions(text)

    assert status == "ok"
    assert warnings == []
    assert "Таблица PDF 1.1" in text
    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("Выключатель автоматический ВА 47-60М 1п 3А С", 1.0, "шт"),
        ("Выключатель автоматический ВА 47-60М3п 25А С", 1.0, "шт"),
    ]


class _TimedOutOcr:
    def ocr_pdf(self, path: Path) -> str:
        raise TimeoutError("OCR timeout")


def test_pdf_ocr_timeout_skips_file_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()

    settings = Settings(postgres_dsn="postgresql://test:test@localhost/test")
    text, status, warnings = extract_pdf_text(path, settings, _TimedOutOcr())

    assert text == ""
    assert status == "ocr_timeout"
    assert any("OCR превысил лимит времени" in warning for warning in warnings)
