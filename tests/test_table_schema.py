from types import SimpleNamespace
from app.models import SpreadsheetTable, SpreadsheetTableSchemaResponse
from app.services.table_schema import (
    apply_table_schema,
    build_table_schema_profile,
    classify_spreadsheet_tables,
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
    )
    tables, warnings, debug = classify_spreadsheet_tables(StubLlm(), [_price_table()], settings)

    assert not warnings
    assert tables[0].headerMap["product"] == "C"
    assert debug["applied"] == 1
