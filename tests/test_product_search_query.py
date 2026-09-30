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
    _compact_token,
    _prepend_required_tokens,
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
    assert normalize_search_characteristic(
        SemanticCharacteristic(
            id="c4",
            name="Наличие фрост фильтра",
            original_value="да",
            semantic_role="VARIANT_SELECTOR",
            value_form="TEXT",
            search_token="да",
        )
    ) is None
    assert normalize_search_characteristic(
        SemanticCharacteristic(
            id="c5",
            name="Индикаторы",
            original_value="наличие питания и сигнала",
            semantic_role="OTHER",
            value_form="TEXT",
            search_token="наличие питания и сигнала",
        )
    ) is None


def test_color_temperature_range_is_preserved_instead_of_midpoint() -> None:
    characteristic = SemanticCharacteristic(
        id="c1",
        name="Диапазон СТО, К",
        original_value="3200-7500",
        semantic_role="VARIANT_SELECTOR",
        value_form="ACCEPTABLE_RANGE",
        search_token="3200-7500К",
        numeric=CharacteristicNumeric(min=3200, max=7500, unit="К"),
    )

    normalized = normalize_search_characteristic(characteristic)

    assert normalized is not None
    assert normalized["search_token"] == "3200-7500К"


def test_search_token_sanitizer_drops_quantities_broken_units_and_case_duplicates() -> None:
    assert _compact_token("2 шт") == ""
    assert _compact_token("м2)") == ""
    assert _prepend_required_tokens(
        [],
        ["RS-232/422/485", "rs-232/422/485", "8 шт"],
    ) == ["RS-232/422/485"]



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


def test_enumerated_feature_list_is_not_used_as_a_search_token() -> None:
    characteristic = SemanticCharacteristic(
        id="c1",
        name="Supported functions",
        original_value="VLAN; 802.1Q; Private VLAN; Voice VLAN; MAC; QinQ",
        semantic_role="INTERFACE_OR_STANDARD",
        value_form="ENUMERATED",
        search_token="VLAN; 802.1Q; Private VLAN; Voice VLAN; MAC; QinQ",
    )

    assert normalize_search_characteristic(characteristic) is None


def test_semantic_characteristic_accepts_empty_numeric_from_llm() -> None:
    characteristic = SemanticCharacteristic.model_validate(
        {"id": "c1", "name": "Material", "original_value": "copper", "numeric": None}
    )

    assert characteristic.numeric.model_dump() == {
        "min": None, "max": None, "nominal": None, "unit": None, "values": []
    }


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


def test_query_builder_does_not_repeat_value_already_in_product_name() -> None:
    position = TenderPosition(product="Power cord 220V 10A")
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Power cord 220V 10A",
        category="Electrical",
        identifier_strength="HIGH",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(id="c1", sku_importance="HIGH", usage="SEARCH_PRIMARY"),
            SkuCharacteristicDecision(id="c2", sku_importance="HIGH", usage="SEARCH_PRIMARY"),
        ],
        selected_for_search=["c1", "c2"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {"id": "c1", "search_token": "220V"},
            {"id": "c2", "search_token": "10A"},
        ],
    )

    assert enriched.productQuery == "Power cord 220V 10A"
    assert enriched.searchCharacteristics == []


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
    assert enriched.searchQueries == ["Кабель ПВС 3x0,75"]


def test_cable_identity_bundle_overrides_weak_llm_selection() -> None:
    position = TenderPosition(
        product="Кабель",
        characteristics=[
            ProductCharacteristic(name="Тип (марка)", value="АВВГнг(А) 4х70"),
            ProductCharacteristic(name="Класс гибкости", value="5"),
            ProductCharacteristic(name="Напряжение", value="0,66/1 кВ"),
        ],
    )
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Кабель",
        category="Кабели и провода",
        identifier_strength="LOW",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(id="c1", sku_importance="NONE", usage="IGNORE"),
            SkuCharacteristicDecision(id="c2", sku_importance="HIGH", usage="SEARCH_PRIMARY"),
            SkuCharacteristicDecision(id="c3", sku_importance="HIGH", usage="SEARCH_SECONDARY"),
        ],
        selected_for_search=["c2", "c3"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {"id": "c1", "search_token": "АВВГнг(А) 4x70", "source_value": "АВВГнг(А) 4х70"},
            {"id": "c2", "search_token": "5", "source_value": "5"},
            {"id": "c3", "search_token": "0,66/1кВ", "source_value": "0,66/1 кВ"},
        ],
    )

    assert enriched.productQuery == "Кабель АВВГнг(А) 4x70 0,66/1кВ"
    assert enriched.searchCharacteristics == ["АВВГнг(А) 4x70", "0,66/1кВ"]
    assert " 5" not in enriched.productQuery


def test_llm_identity_bundle_adds_ignored_identifier_to_query() -> None:
    position = TenderPosition(product="Датчик")
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Датчик",
        category="КИП",
        identifier_strength="LOW",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(id="c1", sku_importance="CRITICAL", usage="IGNORE"),
            SkuCharacteristicDecision(id="c2", sku_importance="HIGH", usage="SEARCH_SECONDARY"),
        ],
        selected_for_search=["c2"],
        identity_bundle=["c1"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {"id": "c1", "search_token": "PT1000", "source_value": "PT1000"},
            {"id": "c2", "search_token": "24В", "source_value": "24 В"},
        ],
    )

    assert enriched.productQuery == "Датчик PT1000 24В"


def test_compact_designation_is_preserved_when_llm_treats_it_as_a_range() -> None:
    position = TenderPosition(
        product="Светильник светодиодный",
        characteristics=[
            ProductCharacteristic(name="Технические характеристики", value="SPP-201-0-65-036"),
            ProductCharacteristic(name="Напряжение", value="175-260 В"),
        ],
    )
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Светильник светодиодный",
        category="Электротехника",
        identifier_strength="LOW",
    )
    sku = ProductSkuClassification(
        position_index=1,
        decisions=[
            SkuCharacteristicDecision(id="c1", sku_importance="LOW", usage="IGNORE"),
            SkuCharacteristicDecision(id="c2", sku_importance="HIGH", usage="SEARCH_SECONDARY"),
        ],
        selected_for_search=["c2"],
    )

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [
            {"id": "c1", "search_token": "-201-0", "source_value": "SPP-201-0-65-036"},
            {"id": "c2", "search_token": "175-260В", "source_value": "175-260 В"},
        ],
    )

    assert enriched.productQuery == "Светильник светодиодный SPP-201-0-65-036 175-260В"
    assert enriched.searchCharacteristics == ["SPP-201-0-65-036", "175-260В"]


def test_compact_designation_is_not_repeated_when_already_in_product_name() -> None:
    position = TenderPosition(product="Светильник SPO-7-72-4K-P(4)")
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="Светильник SPO-7-72-4K-P(4)",
        category="Электротехника",
        identifier_strength="HIGH",
    )
    sku = ProductSkuClassification(position_index=1)

    enriched = build_search_query(
        position,
        semantic,
        sku,
        [{"id": "c1", "search_token": "-7-72", "source_value": "SPO-7-72-4K-P(4)"}],
    )

    assert enriched.productQuery == "Светильник SPO-7-72-4K-P(4)"
    assert enriched.searchCharacteristics == []


def test_flattened_table_row_uses_product_kind_and_preserves_named_model() -> None:
    position = TenderPosition(
        product=(
            "\u0422\u0438\u043f \u0438\u0437\u0434\u0435\u043b\u0438\u044f: \u0424\u043e\u043d\u0430\u0440\u044c \u0430\u043a\u043a\u0443\u043c\u0443\u043b\u044f\u0442\u043e\u0440\u043d\u044b\u0439; \u0422\u0438\u043f \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f: \u042d\u043a\u043e\u0442\u043e\u043d-5; "
            "\u041d\u043e\u043c\u0438\u043d\u0430\u043b\u044c\u043d\u043e\u0435 \u043d\u0430\u043f\u0440\u044f\u0436\u0435\u043d\u0438\u0435: 12 \u0412; \u0412\u0440\u0435\u043c\u044f \u0440\u0430\u0431\u043e\u0442\u044b: 6 \u0447"
        )
    )
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="\u0424\u043e\u043d\u0430\u0440\u044c \u0430\u043a\u043a\u0443\u043c\u0443\u043b\u044f\u0442\u043e\u0440\u043d\u044b\u0439",
        category="\u041e\u0441\u0432\u0435\u0449\u0435\u043d\u0438\u0435",
        identifier_strength="LOW",
    )
    enriched = build_search_query(
        position, semantic, ProductSkuClassification(position_index=1), []
    )

    assert enriched.productQuery == "\u0424\u043e\u043d\u0430\u0440\u044c \u0430\u043a\u043a\u0443\u043c\u0443\u043b\u044f\u0442\u043e\u0440\u043d\u044b\u0439 \u042d\u043a\u043e\u0442\u043e\u043d-5"
    assert enriched.searchCharacteristics == ["\u042d\u043a\u043e\u0442\u043e\u043d-5"]
    assert enriched.searchQueries == ["\u0424\u043e\u043d\u0430\u0440\u044c \u0430\u043a\u043a\u0443\u043c\u0443\u043b\u044f\u0442\u043e\u0440\u043d\u044b\u0439 \u042d\u043a\u043e\u0442\u043e\u043d-5"]


def test_flattened_table_row_builds_power_supply_with_compact_type_model() -> None:
    position = TenderPosition(
        product="\u0412\u0438\u0434 /(\u0411\u043b\u043e\u043a): \u043f\u0438\u0442\u0430\u043d\u0438\u044f; \u0422\u0438\u043f: MDR-40; \u0412\u044b\u0445\u043e\u0434\u043d\u043e\u0435 \u043d\u0430\u043f\u0440\u044f\u0436\u0435\u043d\u0438\u0435: 24 \u0412"
    )
    semantic = ProductSemanticClassification(
        position_index=1,
        normalized_product="MDR-40",
        category="\u042d\u043b\u0435\u043a\u0442\u0440\u043e\u0442\u0435\u0445\u043d\u0438\u043a\u0430",
        identifier_strength="LOW",
    )
    enriched = build_search_query(
        position, semantic, ProductSkuClassification(position_index=1), []
    )

    assert enriched.productQuery == "\u0411\u043b\u043e\u043a \u043f\u0438\u0442\u0430\u043d\u0438\u044f MDR-40"
    assert enriched.searchCharacteristics == ["MDR-40"]


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


def test_characteristic_llm_failure_builds_compact_fallback_query() -> None:
    class FailingLlm:
        def classify_product_characteristics(self, items: list[dict]) -> ProductSemanticBatchResponse:
            raise TimeoutError("timeout")

    position = TenderPosition(
        product="Светильник",
        productQuery="Светильник исходный запрос",
        characteristics=[ProductCharacteristic(name="Мощность", value="40 Вт")],
    )

    enriched, warnings, debug = enrich_product_search_queries(FailingLlm(), [position])

    assert enriched[0].productQuery == "Светильник 40Вт"
    assert enriched[0].searchCharacteristics == ["40Вт"]
    assert enriched[0].searchQueries[0] == "Светильник 40Вт"
    assert warnings
    assert debug["enrichedCount"] == 1


def test_projector_fallback_uses_values_and_excludes_binary_presence() -> None:
    class FailingLlm:
        def classify_product_characteristics(self, items: list[dict]) -> ProductSemanticBatchResponse:
            raise TimeoutError("timeout")

    position = TenderPosition(
        product="Прожектор",
        productQuery=(
            "Прожектор; технические характеристики: Источник света: светодиод; "
            "Мощность, Вт: не менее 500"
        ),
        characteristics=[
            ProductCharacteristic(name="Источник света", value="светодиод"),
            ProductCharacteristic(name="Мощность, Вт", value="не менее 500"),
            ProductCharacteristic(name="Наличие фрост фильтра", value="да"),
            ProductCharacteristic(name="Тип цветосмешения CMY", value="линейное"),
            ProductCharacteristic(name="Диапазон СТО, К", value="3200-7500"),
            ProductCharacteristic(
                name="Протоколы управления",
                value="RDM, DMX512, автоматический режим, master-slave",
            ),
        ],
    )

    enriched, warnings, _ = enrich_product_search_queries(FailingLlm(), [position])

    assert warnings
    assert enriched[0].productQuery == "Прожектор 500Вт линейное 3200-7500К"
    assert enriched[0].searchCharacteristics == [
        "500Вт",
        "линейное",
        "3200-7500К",
    ]
    assert enriched[0].searchQueries[0] == enriched[0].productQuery
    assert enriched[0].searchQueries == [
        "Прожектор 500Вт линейное 3200-7500К",
        "Прожектор 500Вт линейное",
        "Прожектор 500Вт",
    ]
    assert "Прожектор" not in enriched[0].searchQueries
    assert "наличие" not in enriched[0].productQuery.casefold()
    assert "да" not in enriched[0].productQuery.casefold()
    assert "автоматический" not in enriched[0].productQuery.casefold()
