from app.models import (
    DocumentPriceSource,
    ProductMatchItem,
    TenderPosition,
    TenderPositionsResponse,
)
from app.services.products import (
    extract_deterministic_positions,
    extract_seldon_positions,
    merge_positions,
    parse_quantity,
)


def test_quantity_parsing() -> None:
    assert parse_quantity("16 шт.") == 16
    assert parse_quantity("2,5") == 2.5
    assert parse_quantity(None) is None


def test_numeric_decimal_row_is_not_a_product() -> None:
    response = TenderPositionsResponse(
        products=[
            TenderPosition(product="10.0", quantity=1, unit="шт"),
            TenderPosition(product="Кабель", quantity=1, unit="шт"),
        ]
    )

    merged, warnings = merge_positions([], response)

    assert [item.product for item in merged] == ["Кабель"]
    assert any("служебная строка" in warning for warning in warnings)


def test_equivalent_suffix_and_missing_quantity_are_merged() -> None:
    response = TenderPositionsResponse(
        products=[
            TenderPosition(product="Кабель или аналог", quantity=None, unit="шт"),
            TenderPosition(product="Кабель", quantity=12, unit="шт"),
        ]
    )

    merged, _ = merge_positions([], response)

    assert len(merged) == 1
    assert merged[0].quantity == 12


def test_excel_like_position_extraction_preserves_quantity() -> None:
    positions = extract_deterministic_positions(
        "№ п/п Наименование товара Ед. изм. Кол-во 1 Моноблок штука 16 тип моноблок"
    )
    assert len(positions) == 1
    assert positions[0].product == "Моноблок"
    assert positions[0].quantity == 16
    assert positions[0].unit.lower() == "штука"


def test_characteristic_labels_do_not_replace_excel_header_columns() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "Лист: Спецификация",
                "Строка 1: A: Наименование товара | B: Ед. изм. | C: Количество",
                "Строка 2: A: Выключатель автоматический | B: шт | C: 10 | "
                "D: Количество полюсов | E: Наименование показателя",
            )
        )
    )

    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("Выключатель автоматический", 10.0, "шт")
    ]


def test_invalid_structured_table_falls_back_to_text_extraction() -> None:
    text = chr(10).join(
        (
            "Лист: Спецификация",
            "Строка 1: A: Наименование товара | B: Ед. изм. | C: Количество",
            "Строка 2: A: Кабель силовой | B: м | C: 25",
        )
    )
    invalid_tables = [
        {
            "sheet": "Повреждённый лист",
            "rows": [{"row": 0, "cells": "not-an-object"}],
        }
    ]

    positions = extract_deterministic_positions(
        text,
        invalid_tables,  # type: ignore[arg-type]
    )

    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("Кабель силовой", 25.0, "м")
    ]


def test_malformed_llm_source_cells_are_normalized_without_failure() -> None:
    position = TenderPosition.model_validate(
        {
            "product": "Кабель силовой",
            "sourceCells": ["B: Кабель", "C: м", "D: 25"],
        }
    )

    assert position.sourceCells == {}


def test_merge_preserves_deterministic_document_price_on_llm_position() -> None:
    deterministic = [
        TenderPosition(
            product="Кабель",
            productQuery="Кабель",
            quantity=5,
            unit="шт",
            documentUnitPriceRub=100,
            documentLineTotalRub=500,
            documentCurrency="RUB",
            documentPriceSource=DocumentPriceSource(
                fileName="spec.xlsx",
                sheet="Лист1",
                row=2,
                unitPriceColumn="E",
                lineTotalColumn="F",
                extractionMethod="excel_deterministic",
            ),
        )
    ]
    llm = TenderPositionsResponse(
        products=[TenderPosition(product="Кабель", productQuery="Кабель", quantity=5, unit="шт")]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert not warnings
    assert len(merged) == 1
    assert merged[0].documentUnitPriceRub == 100
    assert merged[0].documentLineTotalRub == 500
    assert merged[0].documentPriceSource is not None
    assert merged[0].documentPriceSource.extractionMethod == "excel_deterministic"


def test_seldon_structured_quantity_has_priority_over_excel_and_llm() -> None:
    seldon = extract_seldon_positions(
        {
            "lotsList": [
                {
                    "productsList": [
                        {
                            "name": "Люк чугунный круглый",
                            "quantity": "200",
                            "okei": {"name": "шт"},
                        }
                    ]
                }
            ]
        }
    )
    excel = [
        TenderPosition(
            product="Люк чугунный круглый",
            productQuery="Люк чугунный круглый",
            quantity=70,
            unit="комплект",
            source="excel_table_deterministic",
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product="Люк чугунный круглый",
                productQuery="Люк чугунный круглый",
                quantity=1,
                unit="ед",
            )
        ]
    )

    merged, _ = merge_positions(excel, llm, seldon)

    assert len(merged) == 1
    assert merged[0].quantity == 200
    assert merged[0].unit == "шт"

def test_llm_source_reference_null_strings_are_normalized() -> None:
    position = TenderPosition.model_validate(
        {
            "product": "Cable",
            "sourceReference": {
                "fileName": None,
                "sheet": None,
                "row": 1,
                "productColumn": None,
                "quantityColumn": None,
                "unitColumn": None,
                "productHeader": None,
                "quantityHeader": None,
                "unitHeader": None,
                "extractionMethod": "llm",
            },
        }
    )

    assert position.sourceReference is not None
    assert position.sourceReference.sheet == ""
    assert position.sourceReference.productColumn == ""

def test_empty_llm_document_price_source_is_normalized_to_null() -> None:
    position = TenderPosition.model_validate(
        {
            "product": "Таль электрическая",
            "documentPriceSource": {
                "row": None,
                "sheet": "",
                "fileName": "",
                "lineTotalColumn": "",
                "unitPriceColumn": "",
                "extractionMethod": "llm",
            },
        }
    )

    assert position.documentPriceSource is None


def test_excel_extraction_method_alias_is_normalized() -> None:
    position = TenderPosition.model_validate(
        {
            "product": "?????????????",
            "documentPriceSource": {
                "fileName": "????????????.xlsx",
                "sheet": "????1",
                "row": 37,
                "extractionMethod": "excel",
            },
        }
    )

    assert position.documentPriceSource is not None
    assert position.documentPriceSource.extractionMethod == "excel_deterministic"


def test_product_match_item_discards_non_numeric_diagnostic_price() -> None:
    item = ProductMatchItem.model_validate(
        {
            "positionIndex": 37,
            "product": "Кабель силовой",
            "productQuery": "Кабель силовой",
            "documentLineTotalRub": "Кабель силовой ВВГнг(A) 1х95/25 - 10",
            "match": {},
        }
    )

    assert item.documentLineTotalRub is None


def test_merge_filters_row_numbers_classifier_codes_and_service_phrases() -> None:
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(product="1"),
            TenderPosition(product="28.14.11.121"),
            TenderPosition(product="Национальный режим не предоставляется – ограничение"),
            TenderPosition(product="Клапан регулирующий", quantity=2, unit="шт"),
        ]
    )

    merged, warnings = merge_positions([], llm)

    assert [item.product for item in merged] == ["Клапан регулирующий"]
    assert len([item for item in warnings if "служебная строка" in item]) == 3


def test_merge_filters_auxiliary_ol_codes_and_example_rows() -> None:
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(product="ОЛ-5"),
            TenderPosition(product="OL-7"),
            TenderPosition(product="Пример"),
            TenderPosition(product="Клапан регулирующий", quantity=1, unit="шт"),
        ]
    )

    merged, warnings = merge_positions([], llm)

    assert [item.product for item in merged] == ["Клапан регулирующий"]
    assert len([item for item in warnings if "служебная строка" in item]) == 3


def test_excel_adjacent_quantity_overrides_price_copied_by_llm() -> None:
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product="Клапан регулирующий",
                quantity=33636.4004,
                unit="шт",
                evidence=(
                    "Строка 12: A: 1 | B: Клапан регулирующий | "
                    "D: шт | E: 1 | O: 33636.4004"
                ),
            )
        ]
    )

    merged, warnings = merge_positions([], llm)

    assert merged[0].quantity == 1
    assert any("исправлено по соседним ячейкам Excel" in item for item in warnings)


def test_merge_filters_delivery_address_and_deduplicates_tz_and_specification() -> None:
    deterministic = [
        TenderPosition(
            product=(
                'производственное предприятие "Предприятие тепловых сетей" филиала '
                '«Самарский», 443082, г. Самара, ул. 1-й переулок, д. 55'
            ),
            quantity=1,
            unit="шт",
            evidence="Строка 8, колонка K — адрес поставки",
            source="excel_table_deterministic",
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product=(
                    "ВЕНТИЛЯТОР ОСЕВОЙ ВОГД 4.0 (или эквивалент): "
                    "Назначение: проветривание колодцев. Технические характеристики: "
                    "гидравлический привод"
                ),
                quantity=1,
                unit="шт",
                evidence="ТЗ, строка 10",
            ),
            TenderPosition(
                product=(
                    "СТАНЦИЯ ГИДРАВЛИЧЕСКАЯ для подключения до 4-ех инструментов "
                    "одновременно с электростартером (или эквивалент): "
                    "Назначение: питание четырёх гидравлических инструментов"
                ),
                quantity=2,
                unit="шт",
                evidence="ТЗ, строка 11",
            ),
            TenderPosition(
                product="ВЕНТИЛЯТОР ОСЕВОЙ ВОГД 4.0 (или эквивалент)",
                quantity=1,
                unit="шт",
                evidence="Спецификация, строка 8",
            ),
            TenderPosition(
                product=(
                    "СТАНЦИЯ ГИДРАВЛИЧЕСКАЯ для подключения до 4-ех инструментов "
                    "одновременно с электростартером (или эквивалент)"
                ),
                quantity=2,
                unit="шт",
                evidence="Спецификация, строка 9",
            ),
        ]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert [item.product for item in merged] == [
        "ВЕНТИЛЯТОР ОСЕВОЙ ВОГД 4.0 (или эквивалент)",
        (
            "СТАНЦИЯ ГИДРАВЛИЧЕСКАЯ для подключения до 4-ех инструментов "
            "одновременно с электростартером (или эквивалент)"
        ),
    ]
    assert [item.quantity for item in merged] == [1, 2]
    assert "Назначение: проветривание колодцев" in merged[0].requirements
    assert "ТЗ, строка 10" in merged[0].evidence
    assert "Спецификация, строка 8" in merged[0].evidence
    assert any("адрес" in warning.lower() for warning in warnings)
    assert len([warning for warning in warnings if "повторно извлечённая" in warning]) == 2


def test_address_word_inside_real_product_name_is_not_filtered() -> None:
    llm = TenderPositionsResponse(
        products=[TenderPosition(product="Извещатель пожарный адресный", quantity=5, unit="шт")]
    )

    merged, _ = merge_positions([], llm)

    assert [item.product for item in merged] == ["Извещатель пожарный адресный"]


def test_initial_tender_price_is_not_kept_as_position_price() -> None:
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product="Трансформатор",
                quantity=2,
                documentLineTotalRub=1_100_000,
                documentCurrency="RUB",
                documentPriceEvidence=(
                    "Начальная максимальная цена договора: 1 100 000 рублей."
                ),
            )
        ]
    )

    merged, _ = merge_positions([], llm)

    assert merged[0].documentUnitPriceRub is None
    assert merged[0].documentLineTotalRub is None
    assert merged[0].documentPriceEvidence == ""
