from __future__ import annotations

from app.models import (
    CharacteristicNumeric,
    ProductCharacteristic,
    ProductSemanticBatchResponse,
    ProductSemanticClassification,
    ProductSkuBatchResponse,
    ProductSkuClassification,
    SemanticCharacteristic,
    SkuCharacteristicDecision,
    TenderPosition,
)
from app.services.product_search_query import (
    build_search_query,
    enrich_product_search_queries,
    normalize_search_characteristic,
)


def semantic_characteristic(
    characteristic_id: str,
    *,
    role: str = "PERFORMANCE",
    form: str = "EXACT",
    token: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    nominal: float | None = None,
    unit: str | None = None,
    encoded: bool = False,
) -> SemanticCharacteristic:
    return SemanticCharacteristic.model_validate(
        {
            "id": characteristic_id,
            "name": "Параметр",
            "original_value": token,
            "semantic_role": role,
            "value_form": form,
            "search_token": token,
            "already_encoded_in_name": encoded,
            "numeric": {
                "min": minimum,
                "max": maximum,
                "nominal": nominal,
                "unit": unit,
            },
        }
    )


def test_deterministic_numeric_normalization_uses_search_safe_values() -> None:
    acceptable = normalize_search_characteristic(
        semantic_characteristic(
            "c1", form="ACCEPTABLE_RANGE", token="25-32 кВт", minimum=25, maximum=32, unit="кВт"
        )
    )
    minimum = normalize_search_characteristic(
        semantic_characteristic("c2", form="MINIMUM", token=">= 200 м3/ч", minimum=200, unit="м3/ч")
    )
    maximum = normalize_search_characteristic(
        semantic_characteristic("c3", form="MAXIMUM", token="<= 50 м", maximum=50, unit="м")
    )
    tolerance = normalize_search_characteristic(
        semantic_characteristic("c4", form="TOLERANCE", token="200±10 мм", nominal=200, unit="мм")
    )
    interface = normalize_search_characteristic(
        semantic_characteristic("c5", role="INTERFACE_OR_STANDARD", form="INTERFACE_RANGE", token="4-20 мА")
    )

    assert acceptable and acceptable["search_token"] == "28,5кВт"
    assert minimum and minimum["search_token"] == "200м3/ч"
    assert maximum and maximum["search_token"] == "50м"
    assert tolerance and tolerance["search_token"] == "200мм"
    assert interface and interface["search_token"] == "4-20мА"


def test_non_search_and_already_encoded_characteristics_are_removed() -> None:
    assert normalize_search_characteristic(
        semantic_characteristic("c1", role="TEMPORAL", token="12 месяцев")
    ) is None
    assert normalize_search_characteristic(
        semantic_characteristic("c2", role="QUANTITY", token="10 шт")
    ) is None
    assert normalize_search_characteristic(
        semantic_characteristic("c3", token="220В", encoded=True)
    ) is None


def test_hallucinated_search_token_is_rejected() -> None:
    characteristic = SemanticCharacteristic(
        id="c1",
        name="Напряжение",
        original_value="220 В",
        semantic_role="PERFORMANCE",
        value_form="EXACT",
        search_token="230 В",
    )

    assert normalize_search_characteristic(characteristic) is None

    wrong_unit = characteristic.model_copy(update={"search_token": "220 Гц"})
    assert normalize_search_characteristic(wrong_unit) is None


def test_query_builder_uses_only_values_without_parameter_labels() -> None:
    position = TenderPosition(product="БУРС-1В", productQuery="БУРС-1В; Напряжение питания: 220 В")
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="БУРС-1В",
        category="Электротехника",
        identifier_strength="MEDIUM",
        characteristics=[],
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(id="c1", sku_importance="HIGH", usage="SEARCH_PRIMARY"),
            SkuCharacteristicDecision(id="c2", sku_importance="HIGH", usage="SEARCH_SECONDARY"),
        ],
        selected_for_search=["c1", "c2"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {"id": "c1", "search_token": "220В"},
            {"id": "c2", "search_token": "50Гц"},
        ],
    )

    assert enriched.productQuery == "БУРС-1В 220В 50Гц"
    assert enriched.searchCharacteristics == ["220В", "50Гц"]
    assert "Напряжение" not in enriched.productQuery


def test_query_builder_composes_cable_designation_and_query_variants() -> None:
    position = TenderPosition(
        product="Кабель ПВС",
        characteristics=[
            ProductCharacteristic(
                name="Количество жил",
                normalizedName="cable.cores",
                value="3",
            ),
            ProductCharacteristic(
                name="Сечение",
                normalizedName="cable.cross_section",
                value="0,75 мм2",
            ),
        ],
    )
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Кабель ПВС",
        category="Кабели и провода",
        identifier_strength="MEDIUM",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(
                id="c1", sku_importance="CRITICAL", usage="SEARCH_PRIMARY"
            ),
            SkuCharacteristicDecision(
                id="c2", sku_importance="CRITICAL", usage="SEARCH_PRIMARY"
            ),
        ],
        selected_for_search=["c1", "c2"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {
                "id": "c1",
                "name": "Количество жил",
                "search_token": "3",
                "source_value": "3",
            },
            {
                "id": "c2",
                "name": "Сечение",
                "search_token": "0,75мм2",
                "source_value": "0,75 мм2",
            },
        ],
    )

    assert enriched.productQuery == "Кабель ПВС 3x0,75"
    assert enriched.searchCharacteristics == ["3x0,75"]
    assert enriched.searchCategoryCode == "electrical_lighting"
    assert enriched.searchQueries == ["Кабель ПВС", "Кабель ПВС 3x0,75"]


class SearchQueryLlm:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def classify_product_characteristics(self, items: list[dict]) -> ProductSemanticBatchResponse:
        self.calls.append("semantic")
        item = items[0]
        return ProductSemanticBatchResponse(
            results=[
                ProductSemanticClassification(
                    position_index=item["position_index"],
                    normalized_product=item["product"],
                    category="Электротехника",
                    identifier_strength="MEDIUM",
                    characteristics=[
                        semantic_characteristic("c1", token="220 В"),
                        semantic_characteristic("c2", token="50 Гц"),
                    ],
                )
            ]
        )

    def classify_sku_importance(self, items: list[dict]) -> ProductSkuBatchResponse:
        self.calls.append("sku")
        return ProductSkuBatchResponse(
            results=[
                ProductSkuClassification(
                    position_index=items[0]["position_index"],
                    decisions=[
                        SkuCharacteristicDecision(
                            id=item["id"],
                            sku_importance="HIGH",
                            usage="SEARCH_SECONDARY",
                        )
                        for item in items[0]["characteristics"]
                    ],
                    selected_for_search=["c1", "c2"],
                )
            ]
        )


def test_two_llm_stages_enrich_query_and_keep_structured_characteristics() -> None:
    llm = SearchQueryLlm()
    position = TenderPosition(
        product="Светильник ионный",
        characteristics=[
            ProductCharacteristic(name="Напряжение", value="220 В"),
            ProductCharacteristic(name="Частота", value="50 Гц"),
        ],
    )

    enriched, warnings, debug = enrich_product_search_queries(llm, [position])

    assert not warnings
    assert llm.calls == ["semantic", "sku"]
    assert enriched[0].productQuery == "Светильник ионный 220В 50Гц"
    assert len(enriched[0].characteristics) == 2
    assert debug["enrichedCount"] == 1


def test_characteristic_llm_failure_keeps_original_query() -> None:
    class FailingLlm:
        def classify_product_characteristics(self, items: list[dict]) -> ProductSemanticBatchResponse:
            raise TimeoutError("timeout")

    position = TenderPosition(
        product="Светильник",
        productQuery="Светильник исходный запрос",
        characteristics=[ProductCharacteristic(name="Мощность", value="40 Вт")],
    )

    enriched, warnings, debug = enrich_product_search_queries(FailingLlm(), [position])

    assert enriched[0].productQuery == "Светильник исходный запрос"
    assert warnings
    assert debug["enrichedCount"] == 0
