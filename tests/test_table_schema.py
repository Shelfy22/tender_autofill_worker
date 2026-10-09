from types import SimpleNamespace
from app.models import ParsedDocument, SpreadsheetTable, SpreadsheetTableSchemaResponse
from app.services.table_schema import (
    apply_table_schema,
    build_table_schema_profile,
    classify_spreadsheet_tables,
    extract_text_document_tables,
    table_schema_summaries,
)


def _price_table() -> SpreadsheetTable:
    return SpreadsheetTable.model_validate(
        {
            "fileName": "price.xlsx",
            "sheet": "Sheet1",
            "rows": [
                {
                    "row": 1,
                    "cells": {
                        "B": "\u041c\u0430\u0442\u0435\u0440\u0438\u0430\u043b",
                        "C": "\u041a\u0440\u0430\u0442\u043a\u0438\u0439 \u0442\u0435\u043a\u0441\u0442 \u043c\u0430\u0442\u0435\u0440\u0438\u0430\u043b\u0430",
                        "G": "\u0411\u0430\u0437\u0438\u0441\u043d\u0430\u044f \u0415\u0418",
                        "H": "\u0426\u0435\u043d\u0430, \u0440\u0443\u0431. \u0431\u0435\u0437 \u041d\u0414\u0421",
                    },
                },
                {
                    "row": 2,
                    "cells": {
                        "B": "3466190042",
                        "C": "\u041b\u0430\u043c\u043f\u0430 LED E27 10\u0412\u0442",
                        "G": "\u0428\u0422",
                        "H": "80.29",
                    },
                },
            ],
        }
    )


def test_table_schema_profile_is_bounded_and_contains_column_statistics() -> None:
    profile = build_table_schema_profile(_price_table())

    assert profile["rowCount"] == 2
    assert len(profile["sampleRows"]) == 2
    assert next(item for item in profile["columns"] if item["column"] == "H")[
        "numericRatio"
    ] == 0.5


def test_table_schema_profile_clips_long_cells_and_total_payload() -> None:
    table = _price_table()
    table.rows[1].cells["C"] = "x" * 4_000

    profile = build_table_schema_profile(table, max_cell_chars=120, max_chars=1_000)

    assert len(profile["sampleRows"][1]["cells"]["C"]) <= 120
    assert len(str(profile)) <= 1_500


def test_table_schema_overrides_parser_headers_only_when_confident() -> None:
    table = _price_table()
    schema = SpreadsheetTableSchemaResponse(
        tableRole="price_list",
        productColumn="C",
        unitColumn="G",
        unitPriceColumn="H",
        confidence=0.92,
    )

    updated, reason = apply_table_schema(table, schema, min_confidence=0.75)

    assert reason is None
    assert updated.headerMap == {"product": "C", "unit": "G", "unit_price": "H"}


def test_table_schema_rejects_price_as_quantity() -> None:
    table = _price_table()
    schema = SpreadsheetTableSchemaResponse(
        tableRole="purchase_items",
        productColumn="C",
        quantityColumn="H",
        unitPriceColumn="H",
        confidence=0.92,
    )

    updated, reason = apply_table_schema(table, schema, min_confidence=0.75)

    assert updated == table
    assert reason == "price column cannot be quantity column"


def test_table_schema_uses_product_audit_reasoning_llm_method() -> None:
    class StubLlm:
        def classify_spreadsheet_table_schema(self, profile):
            assert profile["fileName"] == "price.xlsx"
            return SpreadsheetTableSchemaResponse(
                tableRole="price_list",
                productColumn="C",
                unitColumn="G",
                unitPriceColumn="H",
                confidence=0.92,
            )

    settings = SimpleNamespace(
        table_schema_audit_enabled=True,
        table_schema_llm_max_tables=1,
        table_schema_min_confidence=0.75,
        table_schema_audit_max_seconds=240,
        table_schema_timeout_seconds=75,
        table_schema_llm_max_attempts=1,
        table_schema_profile_max_cell_chars=320,
        table_schema_profile_max_chars=20_000,
    )
    tables, warnings, debug = classify_spreadsheet_tables(StubLlm(), [_price_table()], settings)

    assert not warnings
    assert tables[0].headerMap["product"] == "C"
    assert debug["applied"] == 1


def test_text_document_tables_keep_exact_document_and_table_scope() -> None:
    document = ParsedDocument(
        documentIndex=1,
        fileName="specification.docx",
        text=(
            "Таблица Word 2\n"
            "Строка 1: A: Наименование | B: Ед. изм. | C: Количество\n"
            "Строка 2: A: Лампа | B: шт | C: 5"
        ),
    )

    tables = extract_text_document_tables([document])

    assert len(tables) == 1
    assert tables[0].fileName == "specification.docx"
    assert tables[0].sheet == "Таблица Word 2"
    assert tables[0].rows[1].cells["A"] == "Лампа"


def test_blocked_table_role_is_retained_for_extraction_guard_and_summary() -> None:
    table = _price_table()
    schema = SpreadsheetTableSchemaResponse(
        tableRole="delivery_schedule",
        confidence=0.95,
    )

    updated, reason = apply_table_schema(table, schema, min_confidence=0.75)

    assert reason is None
    assert updated.tableRole == "delivery_schedule"
    assert table_schema_summaries([updated])["price.xlsx"][0].startswith("Sheet1: role=delivery_schedule")
