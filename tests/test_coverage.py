from app.models import ProductMatch, ProductMatchItem
from app.services.coverage import summarize_product_coverage


def item(
    index: int,
    correspondence: str,
    *,
    quantity: float | None = 1,
    price: float | None = 500_000,
    analogs_allowed: bool | None = None,
    evidence: bool = True,
    document_unit_price: float | None = None,
    document_line_total: float | None = None,
) -> ProductMatchItem:
    return ProductMatchItem(
        positionIndex=index,
        product=f"Товар {index}",
        productQuery=f"Товар {index}",
        quantity=quantity,
        analogsAllowed=analogs_allowed,
        documentUnitPriceRub=document_unit_price,
        documentLineTotalRub=document_line_total,
        documentCurrency="RUB" if document_unit_price or document_line_total else None,
        match=ProductMatch.model_validate(
            {
                "Артикул": f"A-{index}" if evidence else None,
                "Ссылка": None,
                "Наименование": f"Каталог {index}",
                "Производитель": "ETM",
                "Медианная цена": price,
                "Валюта": "RUB",
                "Соответствие": correspondence,
            }
        ),
    )


def test_supplied_is_full_match_or_allowed_analog() -> None:
    result = summarize_product_coverage(
        [
            item(1, "Полное соответствие"),
            item(2, "Аналог", analogs_allowed=True),
            item(3, "Аналог", analogs_allowed=False),
            item(4, "Полное соответствие", evidence=False),
        ]
    )
    assert result["suppliedCount"] == 2
    assert result["fullMatchCount"] == 1
    assert result["analogMatchCount"] == 1
    assert result["coveragePercent"] == 50.0
    assert result["hardReject"] is True


def test_coverage_must_be_strictly_greater_than_50() -> None:
    exactly_half = summarize_product_coverage(
        [item(1, "Полное соответствие"), item(2, "Товар не найден")]
    )
    above_half = summarize_product_coverage(
        [
            item(1, "Полное соответствие"),
            item(2, "Аналог", analogs_allowed=True),
            item(3, "Товар не найден"),
        ]
    )
    assert exactly_half["coverageApproved"] is False
    assert exactly_half["hardReject"] is True
    assert above_half["coveragePercent"] == 66.67
    assert above_half["coverageApproved"] is True
    assert above_half["hardReject"] is False


def test_position_total_is_median_unit_price_times_quantity() -> None:
    result = summarize_product_coverage(
        [
            item(1, "Полное соответствие", quantity=3, price=200_000),
            item(2, "Полное соответствие", quantity=2, price=250_000),
        ]
    )
    assert result["details"][0]["positionTotalPriceRub"] == 600_000
    assert result["details"][1]["positionTotalPriceRub"] == 500_000
    assert result["quantityAdjustedTotalRub"] == 1_100_000
    assert result["priceEvaluationComplete"] is True
    assert result["supplyValueHardReject"] is False


def test_price_threshold_and_incomplete_evaluation() -> None:
    below = summarize_product_coverage(
        [item(1, "Полное соответствие", quantity=1, price=999_999)]
    )
    equal = summarize_product_coverage(
        [item(1, "Полное соответствие", quantity=1, price=1_000_000)]
    )
    incomplete = summarize_product_coverage(
        [item(1, "Полное соответствие", quantity=None, price=10)]
    )
    assert below["supplyValueHardReject"] is True
    assert equal["supplyValueHardReject"] is False
    assert incomplete["priceEvaluationComplete"] is False
    assert incomplete["supplyValueThresholdApplicable"] is False
    assert incomplete["supplyValueHardReject"] is False


def test_russian_formatted_median_price_is_parsed() -> None:
    match = ProductMatch.model_validate(
        {"Медианная цена": "1 251 000,50 руб.", "Соответствие": "Товар не найден"}
    )
    assert match.median_price == 1_251_000.50


def test_legacy_catalog_price_aliases_are_supported() -> None:
    for field in ("Медианная цена, руб.", "Цена", "medianPrice", "price"):
        match = ProductMatch.model_validate(
            {field: "15 707,43 руб.", "Соответствие": "Товар не найден"}
        )
        assert match.median_price == 15_707.43


def test_product_check_keeps_qdrant_price_audit_fields() -> None:
    matched_item = item(1, "Полное соответствие", quantity=2, price=15_707.43)
    matched_item.match.qdrant_point_id = "476338"
    matched_item.match.product_id = "476338"
    matched_item.match.price_source_field = "payload.metadata.price"
    matched_item.match.price_aggregation = "selected_candidate"

    result = summarize_product_coverage([matched_item])
    detail = result["details"][0]
    assert detail["selectedPointId"] == "476338"
    assert detail["productId"] == "476338"
    assert detail["priceSourceField"] == "payload.metadata.price"
    assert detail["priceAggregation"] == "selected_candidate"


def test_document_price_is_diagnostic_and_does_not_change_qdrant_threshold() -> None:
    result = summarize_product_coverage(
        [
            item(
                1,
                "Полное соответствие",
                quantity=2,
                price=600_000,
                document_unit_price=100_000,
                document_line_total=200_000,
            )
        ]
    )

    detail = result["details"][0]
    assert detail["documentUnitPriceRub"] == 100_000
    assert detail["documentLineTotalRub"] == 200_000
    assert detail["documentLineTotalConsistent"] is True
    assert detail["documentVsCatalogDifferenceRub"] == 500_000
    assert detail["documentVsCatalogDifferencePercent"] == 500.0
    assert detail["documentPriceUsedForSupplyValue"] is False
    assert result["quantityAdjustedTotalRub"] == 1_200_000
    assert result["supplyValueHardReject"] is False
    assert result["priceBasis"] == "qdrant_selected_product"
    assert result["documentPriceDiagnostics"]["usedForSupplyValueThreshold"] is False


def test_document_price_does_not_complete_missing_qdrant_price() -> None:
    result = summarize_product_coverage(
        [
            item(
                1,
                "Полное соответствие",
                quantity=2,
                price=None,
                document_unit_price=700_000,
                document_line_total=1_400_000,
            )
        ]
    )

    assert result["priceEvaluationComplete"] is False
    assert result["supplyValueThresholdApplicable"] is False
    assert result["supplyValueHardReject"] is False
    assert result["quantityAdjustedTotalRub"] == 0


def test_document_unit_price_can_be_derived_for_diagnostics_from_line_total() -> None:
    result = summarize_product_coverage(
        [item(1, "Полное соответствие", quantity=4, price=300, document_line_total=1_000)]
    )

    detail = result["details"][0]
    assert detail["documentUnitPriceRub"] is None
    assert detail["documentCalculatedUnitPriceRub"] == 250
    assert detail["documentUnitPriceDerivedFromLineTotal"] is True
    assert detail["documentVsCatalogDifferencePercent"] == 20.0
