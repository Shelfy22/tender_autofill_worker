from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import Settings

if TYPE_CHECKING:
    from app.services.llm import LlmClient


MAGIC_TYPES: list[tuple[bytes, str]] = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf'\x1c", "7z"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
]


def detect_file_type(path: Path, declared_name: str | None = None, mime_type: str | None = None) -> str:
    name = declared_name or path.name
    suffix = Path(name.split("?", 1)[0]).suffix.lower().lstrip(".")
    with path.open("rb") as handle:
        head = handle.read(16)
    magic = next((kind for signature, kind in MAGIC_TYPES if head.startswith(signature)), None)
    if magic == "zip":
        # DOCX/XLSX are ZIP packages; declared extension is decisive after validating ZIP magic.
        if suffix in {"docx", "xlsx"}:
            return suffix
        return "zip"
    if magic == "ole":
        return suffix if suffix in {"doc", "xls"} else "doc"
    if magic:
        return magic
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "") or ""
    return guessed.lstrip(".") or "unknown"


def clip_text(text: str, maximum: int) -> tuple[str, bool]:
    if len(text) <= maximum:
        return text, False
    return text[:maximum], True


def parse_file(path: Path, file_type: str, settings: Settings, llm: "LlmClient | None" = None) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    kind = file_type.lower()
    if kind == "pdf":
        from app.services.parsers.pdf import extract_pdf_text

        text, status, pdf_warnings = extract_pdf_text(path, settings, llm)
        warnings.extend(pdf_warnings)
    elif kind in {"doc", "docx"}:
        from app.services.parsers.word import extract_word_text

        text, status, word_warnings = extract_word_text(path, kind, settings)
        warnings.extend(word_warnings)
    elif kind in {"xls", "xlsx", "csv"}:
        from app.services.parsers.spreadsheets import extract_spreadsheet_text

        text, status, sheet_warnings = extract_spreadsheet_text(path, kind, settings)
        warnings.extend(sheet_warnings)
    else:
        return "", "unsupported", [f"Неподдерживаемый тип файла: {kind}"]
    text, clipped = clip_text(text, settings.max_text_chars_per_file)
    if clipped:
        warnings.append(
            f"Текст {path.name} обрезан до {settings.max_text_chars_per_file} символов."
        )
    return text, status, warnings
