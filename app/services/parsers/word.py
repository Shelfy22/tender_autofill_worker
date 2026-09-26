from __future__ import annotations

import subprocess
from pathlib import Path

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.config import Settings


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(ord("A") + remainder) + name
        index -= 1
    return name


def _convert(path: Path, target_extension: str, settings: Settings) -> Path:
    output_dir = path.parent / "converted"
    output_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            settings.libreoffice_binary,
            "--headless",
            "--convert-to",
            target_extension,
            "--outdir",
            str(output_dir),
            str(path),
        ],
        capture_output=True,
        timeout=settings.conversion_timeout_seconds,
        check=True,
    )
    converted = output_dir / f"{path.stem}.{target_extension}"
    if not converted.exists():
        raise RuntimeError(f"LibreOffice не создал {converted.name}")
    return converted


def extract_word_text(path: Path, file_type: str, settings: Settings) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    docx_path = path
    if file_type == "doc":
        docx_path = _convert(path, "docx", settings)
        warnings.append("Старый DOC преобразован LibreOffice в DOCX.")
    document = Document(docx_path)
    parts: list[str] = []
    seen_tables: list[object] = []
    table_index = 0

    def append_table(table: Table) -> None:
        nonlocal table_index
        element = table._element
        if any(element is seen for seen in seen_tables):
            return
        seen_tables.append(element)
        table_index += 1
        parts.append(f"Таблица Word {table_index}")
        for row_index, row in enumerate(table.rows, start=1):
            values = [cell.text.replace("\n", "; ").strip() for cell in row.cells]
            if any(values):
                parts.append(
                    f"Строка {row_index}: "
                    + " | ".join(
                        f"{_column_name(index)}: {value}"
                        for index, value in enumerate(values)
                    )
                )
        seen_cells: list[object] = []
        for row in table.rows:
            for cell in row.cells:
                if any(cell._tc is seen for seen in seen_cells):
                    continue
                seen_cells.append(cell._tc)
                for nested_table in cell.tables:
                    append_table(nested_table)

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif isinstance(child, CT_Tbl):
            append_table(Table(child, document))
    text = "\n".join(parts).strip()
    quality = len(text) >= 500 and any(char.isalnum() for char in text)
    if not quality:
        warnings.append(f"Word не дал полезный текст: длина {len(text)}.")
    return (text if quality else ""), ("ok" if quality else "word_quality_failed"), warnings
