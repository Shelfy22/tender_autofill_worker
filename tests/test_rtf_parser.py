from pathlib import Path

from app.config import Settings
from app.services.parsers.common import detect_file_type, parse_file
from app.services.parsers.rtf import extract_rtf_text
from app.services.products import extract_deterministic_positions


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


def test_rtf_table_rows_are_extracted_as_products(tmp_path: Path) -> None:
    header = "\u2116|\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435|\u041a\u043e\u043b-\u0432\u043e/\u0448\u0442.".encode("cp1251")
    row_1 = "1|\u0412\u044b\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0412\u0410 47-60\u041c 1\u043f 3\u0410 \u0421|1".encode("cp1251")
    row_2 = "2|\u0412\u044b\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0412\u0410 47-60\u041c3\u043f 25\u0410 \u0421|1".encode("cp1251")
    filler = ("\u0422\u0435\u043a\u0441\u0442 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430\u0446\u0438\u0438 " * 10).encode("cp1251")
    path = tmp_path / "products.rtf"
    path.write_bytes(
        b"{\\rtf1\\ansi\\ansicpg1251\\uc1{\\fonttbl{\\f0 Arial;}}\\f0 "
        + filler
        + b"\\par \\trowd "
        + header.replace(b"|", b"\\cell ")
        + b"\\cell \\row \\trowd "
        + row_1.replace(b"|", b"\\cell ")
        + b"\\cell \\row \\trowd "
        + row_2.replace(b"|", b"\\cell ")
        + b"\\cell \\row }"
    )

    text, status, warnings = extract_rtf_text(path)
    positions = extract_deterministic_positions(text)

    assert status == "ok"
    assert warnings == []
    assert "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 RTF 1" in text
    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("\u0412\u044b\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0412\u0410 47-60\u041c 1\u043f 3\u0410 \u0421", 1.0, "\u0448\u0442"),
        ("\u0412\u044b\u043a\u043b\u044e\u0447\u0430\u0442\u0435\u043b\u044c \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0412\u0410 47-60\u041c3\u043f 25\u0410 \u0421", 1.0, "\u0448\u0442"),
    ]


def test_common_parser_routes_rtf_without_libreoffice(tmp_path: Path) -> None:
    path = tmp_path / "purchase.rtf"
    path.write_bytes(_sample_rtf())
    settings = Settings(postgres_dsn="postgresql://test:test@localhost/test")

    text, status, warnings = parse_file(path, "rtf", settings)

    assert "Комплект ЗИП должен быть в наличии" in text
    assert status == "ok"
    assert warnings == []
