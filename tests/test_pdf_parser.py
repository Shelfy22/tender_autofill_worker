from pathlib import Path

import fitz

from app.config import Settings
from app.services.parsers.pdf import extract_pdf_text


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
