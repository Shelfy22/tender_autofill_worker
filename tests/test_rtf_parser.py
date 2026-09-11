from pathlib import Path

from app.config import Settings
from app.services.parsers.common import detect_file_type, parse_file
from app.services.parsers.rtf import extract_rtf_text


def _sample_rtf() -> bytes:
    heading = "ЗАКУПОЧНАЯ ДОКУМЕНТАЦИЯ".encode("cp1251")
    body = (
        "Комплект ЗИП должен быть в наличии и поставляться вместе с товаром. "
        "Требования к участнику и характеристики продукции приведены в таблице."
    ).encode("cp1251")
    table_heading = "Наименование".encode("cp1251")
    return (
        b"{\\rtf1\\ansi\\ansicpg1251\\uc1"
        b"{\\fonttbl{\\f0 Arial;}}\\f0 "
        + heading
        + b"\\par "
        + body
        + b"\\par \\trowd "
        + table_heading
        + b"\\cell 10\\cell \\row }"
    )


def test_extract_rtf_text_preserves_cyrillic_and_table_cells(tmp_path: Path) -> None:
    path = tmp_path / "purchase.rtf"
    path.write_bytes(_sample_rtf())

    text, status, warnings = extract_rtf_text(path)

    assert "ЗАКУПОЧНАЯ ДОКУМЕНТАЦИЯ" in text
    assert "Комплект ЗИП должен быть в наличии" in text
    assert "Наименование | 10" in text
    assert "Arial" not in text
    assert status == "ok"
    assert warnings == []


def test_rtf_magic_overrides_wrong_declared_extension(tmp_path: Path) -> None:
    path = tmp_path / "download.bin"
    path.write_bytes(_sample_rtf())

    assert detect_file_type(path, "document.dat") == "rtf"


def test_common_parser_routes_rtf_without_libreoffice(tmp_path: Path) -> None:
    path = tmp_path / "purchase.rtf"
    path.write_bytes(_sample_rtf())
    settings = Settings(postgres_dsn="postgresql://test:test@localhost/test")

    text, status, warnings = parse_file(path, "rtf", settings)

    assert "Комплект ЗИП должен быть в наличии" in text
    assert status == "ok"
    assert warnings == []
