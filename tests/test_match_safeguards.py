from __future__ import annotations

from app.models import (
    ProductSemanticClassification,
    ProductSkuClassification,
    SkuCharacteristicDecision,
    SpreadsheetRow,
    TenderPosition,
)
from app.services.catalog import _catalog_category_conflict
from app.services.product_search_query import build_search_query
from app.services.product_validation import _deterministic_non_product_role
from app.services.products import _header_data_score


def test_technical_characteristic_column_is_not_scored_as_product() -> None:
    rows = [
        SpreadsheetRow(row=1, cells={"A": "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435"}),
        SpreadsheetRow(row=2, cells={"A": "\u041c\u043e\u0449\u043d\u043e\u0441\u0442\u044c: 500 \u0412\u0442"}),
        SpreadsheetRow(row=3, cells={"A": "\u041d\u0430\u043f\u0440\u044f\u0436\u0435\u043d\u0438\u0435: 220 \u0412"}),
    ]

    assert _header_data_score("product", "A", 0, rows) == 0.0


def test_obvious_fragments_are_rejected_before_catalog_search() -> None:
    assert _deterministic_non_product_role("800x600x20 \u043c\u043c") == "characteristic"
    assert _deterministic_non_product_role("\u041c\u043e\u0449\u043d\u043e\u0441\u0442\u044c: 500 \u0412\u0442") == "characteristic"
    assert _deterministic_non_product_role("\u0442\u043e\u0447\u043d\u043e") == "header"


def test_strong_model_does_not_add_numeric_characteristics_to_search() -> None:
    position = TenderPosition(product="Transformer ABC-100")
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Transformer ABC-100",
        category="Electrical",
        identifier_strength="HIGH",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(
                id="c1", sku_importance="CRITICAL", usage="SEARCH_PRIMARY"
            )
        ],
        selected_for_search=["c1"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [{"id": "c1", "search_token": "400A", "source_value": "400 A"}],
    )

    assert enriched.productQuery == "Transformer ABC-100"
    assert enriched.searchCharacteristics == []


def test_catalog_rejects_different_basic_product_class() -> None:
    conflict = _catalog_category_conflict(
        TenderPosition(product="\u041f\u0440\u043e\u0436\u0435\u043a\u0442\u043e\u0440 500 \u0412\u0442"),
        {"name": "\u0421\u0432\u0435\u0442\u0438\u043b\u044c\u043d\u0438\u043a 500 \u0412\u0442", "params": {}},
    )

    assert conflict is not None
    assert "projector" in conflict
    assert "luminaire" in conflict
