from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import fitz

from app.config import Settings

if TYPE_CHECKING:
    from app.services.llm import LlmClient


def extract_pdf_text(
    path: Path, settings: Settings, llm: "LlmClient | None" = None
) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    parts: list[str] = []
    total = 0
    with fitz.open(path) as document:
        for page in document:
            text = page.get_text("text")
            remaining = settings.max_text_chars_per_file - total
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            total += min(len(text), remaining)
    result = "\n".join(parts).strip()
    useful = len(result) >= 500 and any(char.isalnum() for char in result)
    if useful:
        return result, "ok", warnings
    warnings.append("PDF не дал полезный текст; требуется OCR.")
    if llm is None:
        return "", "needs_ocr", warnings
    if path.stat().st_size > settings.pdf_ocr_max_bytes:
        warnings.append("PDF превышает PDF_OCR_MAX_BYTES и не отправлен в OCR.")
        return "", "ocr_size_limit", warnings
    ocr_text = llm.ocr_pdf(path)
    if len(ocr_text.strip()) >= 100:
        return ocr_text.strip(), "ocr_ok", warnings
    warnings.append("OCR не вернул полезный текст.")
    return "", "ocr_failed", warnings
