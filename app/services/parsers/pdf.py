from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING

import fitz

from app.config import Settings

if TYPE_CHECKING:
    from app.services.llm import LlmClient


def _has_meaningful_text(value: str) -> bool:
    """Accept short native PDF text and reserve OCR for empty/garbled pages."""
    normalized = " ".join(value.split())
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", normalized, re.UNICODE)
    return len(normalized) >= 30 and len(words) >= 4


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(ord("A") + remainder) + name
        index -= 1
    return name


def _pdf_table_text(page: fitz.Page, page_number: int) -> list[str]:
    if not hasattr(page, "find_tables"):
        return []
    try:
        finder = page.find_tables()
    except Exception:
        return []
    parts: list[str] = []
    for table_index, table in enumerate(getattr(finder, "tables", []) or [], start=1):
        try:
            rows = table.extract()
        except Exception:
            continue
        parts.append(f"Таблица PDF {page_number}.{table_index}")
        for row_index, row in enumerate(rows or [], start=1):
            values = [
                str(value or "")
                .replace("\n", " ")
                .replace("\u00ad", "-")
                .replace("\xa0", " ")
                .strip()
                for value in row or []
            ]
            if any(values):
                parts.append(
                    f"Строка {row_index}: "
                    + " | ".join(
                        f"{_column_name(index)}: {value}"
                        for index, value in enumerate(values)
                    )
                )
    return parts


def extract_pdf_text(
    path: Path, settings: Settings, llm: "LlmClient | None" = None
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    parts: list[str] = []
    total = 0

    def append_limited(value: str) -> None:
        nonlocal total
        if not value:
            return
        remaining = settings.max_text_chars_per_file - total
        if remaining <= 0:
            return
        parts.append(value[:remaining])
        total += min(len(value), remaining)

    with fitz.open(path) as document:
        for page_number, page in enumerate(document, start=1):
            append_limited("\n".join(_pdf_table_text(page, page_number)))
            append_limited(page.get_text("text"))
            if total >= settings.max_text_chars_per_file:
                break
    result = "\n".join(parts).strip()
    useful = _has_meaningful_text(result)
    if useful:
        return result, "ok", warnings
    warnings.append("PDF не дал полезный текст; требуется OCR.")
    if llm is None:
        return "", "needs_ocr", warnings
    if path.stat().st_size > settings.pdf_ocr_max_bytes:
        warnings.append("PDF превышает PDF_OCR_MAX_BYTES и не отправлен в OCR.")
        return "", "ocr_size_limit", warnings
    try:
        ocr_text = llm.ocr_pdf(path)
    except TimeoutError as exc:
        warnings.append(f"OCR превысил лимит времени и файл пропущен: {exc}")
        return "", "ocr_timeout", warnings
    except Exception as exc:
        warnings.append(f"OCR не выполнен, файл пропущен: {type(exc).__name__}: {exc}")
        return "", "ocr_failed", warnings
    if len(ocr_text.strip()) >= 100:
        return ocr_text.strip(), "ocr_ok", warnings
    warnings.append("OCR не вернул полезный текст.")
    return "", "ocr_failed", warnings
