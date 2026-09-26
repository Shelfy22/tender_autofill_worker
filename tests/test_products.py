from app.models import (
    DocumentPriceSource,
    ProductCharacteristic,
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


def test_equivalent_suffix_and_missing_quantity_remain_separate_positions() -> None:
    response = TenderPositionsResponse(
        products=[
            TenderPosition(product="Кабель или аналог", quantity=None, unit="шт"),
            TenderPosition(product="Кабель", quantity=12, unit="шт"),
        ]
    )

    merged, _ = merge_positions([], response)

    assert [item.quantity for item in merged] == [None, 12]


def test_llm_category_without_quantity_is_skipped_when_structured_rows_exist() -> None:
    deterministic = [
        TenderPosition(
            product="Cable APvBShp 4x25",
            productQuery="Cable APvBShp 4x25",
            quantity=559,
            unit="m",
            source="excel_table_deterministic",
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product="Power cables with aluminum conductors up to 1 kV",
                quantity=None,
                unit="",
            )
        ]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert [item.product for item in merged] == ["Cable APvBShp 4x25"]
    assert any("without quantity" in warning for warning in warnings)


def test_merge_filters_tender_conditions_misread_as_products() -> None:
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(product="Согласно Техническому заданию", quantity=26, unit="шт"),
            TenderPosition(product="не менее 12 месяцев", quantity=26, unit="шт"),
            TenderPosition(product="Аналоги рассматриваются. Допуск габаритов ±5%", quantity=12, unit="шт"),
            TenderPosition(product="Аналоги рассматриваются. Допуск по толщине ±15%", quantity=10, unit="шт"),
            TenderPosition(product="Кабель АПвБШп 4х25 мм2", quantity=559, unit="м"),
        ]
    )

    merged, warnings = merge_positions([], llm)

    assert [item.product for item in merged] == ["Кабель АПвБШп 4х25 мм2"]
    assert len([warning for warning in warnings if "служебная строка" in warning]) == 4


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


def test_word_table_quantity_unit_header_extracts_positions() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "Строка 1: A: № | B: Наименование | C: Кол-во/шт. | D: Поставщик №1",
                "Строка 2: A: 1 | B: Выключатель автоматический модульный ВА 47-60М 1п 3А С | C: 1 | D: 157,69",
                "Строка 3: A: 2 | B: Выключатель автоматический модульный ВА 47-60М3п 25А С | C: 1 | D: 547,81",
            )
        )
    )

    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("Выключатель автоматический модульный ВА 47-60М 1п 3А С", 1.0, "шт"),
        ("Выключатель автоматический модульный ВА 47-60М3п 25А С", 1.0, "шт"),
    ]


def test_word_rows_with_same_name_remain_separate_positions() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "Таблица Word 1",
                "Строка 1: A: № | B: Наименование товара/ Код ОКПД2 | C: Кол-во, шт.",
                "Строка 2: A: 1 | B: Выключатель автоматический ВА 47-60М 1п 3А С или эквивалент Код ОКПД2: 27.12.23.190 | C: 1",
                "Таблица Word 2",
                "Строка 1: A: № | B: Наименование | C: Кол-во/шт. | D: Цена за ед., руб. | E: Сумма, руб.",
                "Строка 2: A: 1 | B: Выключатель автоматический ВА 47-60М 1п 3А С | C: 1 | D: 274,01 | E: 274,01",
            )
        )
    )

    assert len(positions) == 2
    assert positions[0].product == "Выключатель автоматический ВА 47-60М 1п 3А С"
    assert positions[0].quantity == 1
    assert positions[0].unit == "шт"
    assert positions[1].documentUnitPriceRub == 274.01


def test_word_companion_characteristics_enrich_catalog_query() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 Word 1",
                "\u0421\u0442\u0440\u043e\u043a\u0430 1: A: \u2116 \u043f/\u043f | B: \u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 | C: \u0415\u0434. \u0438\u0437\u043c. | D: \u041a\u043e\u043b-\u0432\u043e",
                "\u0421\u0442\u0440\u043e\u043a\u0430 2: A: 1 | B: \u042d\u043b\u0435\u043a\u0442\u0440\u043e\u0434\u044b \u0441\u0432\u0430\u0440\u043e\u0447\u043d\u044b\u0435 \u0442\u0438\u043f 1 | C: \u043a\u0433 | D: 10",
                "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 Word 2",
                "\u0421\u0442\u0440\u043e\u043a\u0430 1: A: \u2116 \u043f/\u043f | B: \u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 | C: \u0422\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u0445\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438",
                "\u0421\u0442\u0440\u043e\u043a\u0430 2: A: 1 | B: \u042d\u043b\u0435\u043a\u0442\u0440\u043e\u0434\u044b \u0441\u0432\u0430\u0440\u043e\u0447\u043d\u044b\u0435 \u0442\u0438\u043f 1 | C: \u041c\u0430\u0440\u043a\u0430: \u042d42; \u0434\u0438\u0430\u043c\u0435\u0442\u0440: 3,0 \u043c\u043c; \u043f\u043e\u043a\u0440\u044b\u0442\u0438\u0435: \u0440\u0443\u0442\u0438\u043b\u043e\u0432\u043e\u0435",
            )
        )
    )

    assert len(positions) == 1
    assert "\u041c\u0430\u0440\u043a\u0430: \u042d42" in positions[0].requirements
    assert "\u0434\u0438\u0430\u043c\u0435\u0442\u0440: 3,0 \u043c\u043c" in positions[0].productQuery
    assert "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 Word 2" in positions[0].evidence


def test_word_rows_align_from_right_and_split_companion_characteristics() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "Таблица Word 1",
                "Строка 1: A: № п/п | B: ОКПД-2 | C: Ограничения | "
                "D: Основание | E: Наименование продукции | "
                "F: Предложение эквивалентного товара | G: Кол-во | H: Ед. изм.",
                "Строка 2: A: 1 | B: 2 | C: 3 | D: 4 | E: 5 | F: 6 | G: 7",
                "Строка 3: A:  | B: 27.33.13.110 | C: х | "
                "D: Вилка промышленная 32А 380В IP44 или эквивалент | "
                "E: допускается | F: 10 | G: шт.",
                "Таблица Word 2",
                "Строка 1: A: № п/п | B: Наименование продукции | "
                "C: Технические характеристики",
                "Строка 2: A: 1 | B: 2 | C: 4",
                "Строка 3: A:  | "
                "B: Вилка промышленная 32А 380В IP44 или эквивалент | "
                "C: Номинальный ток: 32 А; Номинальное напряжение: 380 В; "
                "Степень защиты: IP44",
            )
        )
    )

    assert len(positions) == 1
    assert positions[0].product == "Вилка промышленная 32А 380В IP44"
    assert positions[0].quantity == 10
    assert positions[0].unit == "шт."
    assert [(item.name, item.value) for item in positions[0].characteristics] == [
        ("Номинальный ток", "32 А"),
        ("Номинальное напряжение", "380 В"),
        ("Степень защиты", "IP44"),
    ]


def test_word_companion_blank_name_uses_matching_source_row() -> None:
    positions = extract_deterministic_positions(
        "\n".join(
            (
                "Таблица Word 1",
                "Строка 1: A: Наименование | B: Кол-во | C: Ед. изм.",
                "Строка 2: A: Автомат 50А | B: 1 | C: шт",
                "Строка 3: A: Автомат 63А | B: 1 | C: шт",
                "Таблица Word 2",
                "Строка 1: A: Наименование | B: Технические характеристики",
                "Строка 2: A: Автомат 50А | B: Номинальный ток: 50 А",
                "Строка 3: A:  | B: Номинальный ток: 63 А",
            )
        )
    )

    assert len(positions) == 2
    assert positions[0].characteristics[0].value == "50 А"
    assert positions[1].characteristics[0].value == "63 А"
    assert positions[1].characteristics[0].associationMethod == "same_row"


def test_merge_drops_unreferenced_llm_clothing_size_breakdown() -> None:
    deterministic = [
        TenderPosition(
            product=(
                "\u041a\u043e\u0441\u0442\u044e\u043c \u043c\u0443\u0436\u0441\u043a\u043e\u0439 \u0437\u0438\u043c\u043d\u0438\u0439, "
                "\u0442\u0435\u043c\u043d\u043e-\u0441\u0438\u043d\u0438\u0439"
            ),
            quantity=102,
            sourceReference={
                "fileName": "spec.doc",
                "row": 2,
                "productColumn": "A",
                "extractionMethod": "llm",
            },
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product=(
                    "\u041a\u043e\u0441\u0442\u044e\u043c\u044b \u0437\u0438\u043c\u043d\u0438\u0435 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 "
                    "\u0440\u0430\u0437\u043c\u0435\u0440 44-62"
                ),
                quantity=95,
            ),
            TenderPosition(
                product=(
                    "\u041a\u043e\u0441\u0442\u044e\u043c\u044b \u0437\u0438\u043c\u043d\u0438\u0435 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 "
                    "\u0440\u0430\u0437\u043c\u0435\u0440 64-70"
                ),
                quantity=7,
            ),
        ]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert [(position.product, position.quantity) for position in merged] == [
        (deterministic[0].product, 102)
    ]
    assert any("size breakdown" in warning for warning in warnings)


def test_merge_keeps_same_file_rows_and_prefers_nmck_copy_across_files() -> None:
    contract_positions = [
        TenderPosition(
            product="\u0421\u0432\u0435\u0442\u0438\u043b\u044c\u043d\u0438\u043a \u043d\u0430\u0441\u0442\u0435\u043d\u043d\u044b\u0439",
            quantity=1,
            unit="\u0448\u0442",
            sourceReference={
                "fileName": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430.docx",
                "row": 2,
                "productColumn": "A",
            },
        ),
        TenderPosition(
            product="\u0421\u0432\u0435\u0442\u0438\u043b\u044c\u043d\u0438\u043a \u043d\u0430\u0441\u0442\u0435\u043d\u043d\u044b\u0439",
            quantity=1,
            unit="\u0448\u0442",
            sourceReference={
                "fileName": "\u041f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440\u0430.docx",
                "row": 3,
                "productColumn": "A",
            },
        ),
    ]
    nmck_positions = [
        TenderPosition(
            product=position.product,
            quantity=position.quantity,
            unit=position.unit,
            sourceReference={
                    "fileName": "\u041e\u0431\u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435 \u041d\u041c\u0426\u0414.xlsx",
                    "row": row,
                    "productColumn": "B",
            },
        )
        for row, position in enumerate(contract_positions, start=2)
    ]

    merged, warnings = merge_positions(contract_positions + nmck_positions, None)

    assert len(merged) == 2
    assert all(
        position.sourceReference is not None
        and "\u041d\u041c\u0426" in position.sourceReference.fileName
        for position in merged
    )
    assert any("replicated product rows" in warning for warning in warnings)


def test_merge_uses_price_list_and_adds_characteristics_from_technical_section() -> None:
    technical = TenderPosition(
        product="Светильник промышленный",
        quantity=2,
        unit="шт",
        requirements="Мощность: 40 Вт",
        characteristics=[
            ProductCharacteristic(
                name="Мощность",
                value="40 Вт",
                associationConfidence=0.98,
                associationMethod="same_row",
                associationStatus="confirmed",
            )
        ],
        sourceReference={
            "fileName": "Извещение.doc",
            "row": 20,
            "sectionRole": "technical_specification",
        },
    )
    price = TenderPosition(
        product="Светильник промышленный",
        quantity=2,
        unit="шт",
        sourceReference={
            "fileName": "Извещение.doc",
            "row": 80,
            "sectionRole": "price_justification",
        },
    )

    merged, warnings = merge_positions([technical, price], None)

    assert len(merged) == 1
    assert merged[0].sourceReference is not None
    assert merged[0].sourceReference.sectionRole == "price_justification"
    assert [item.value for item in merged[0].characteristics] == ["40 Вт"]
    assert any("replicated product rows" in warning for warning in warnings)


def test_repeated_names_are_paired_between_price_and_technical_sections() -> None:
    price_positions = [
        TenderPosition(
            product="Лампа настенная",
            quantity=1,
            unit="шт",
            sourceReference={
                "fileName": "Извещение.doc",
                "row": row,
                "sectionRole": "price_justification",
            },
        )
        for row in (80, 81)
    ]
    technical_positions = [
        TenderPosition(
            product="Лампа настенная",
            quantity=1,
            unit="шт",
            characteristics=[
                ProductCharacteristic(
                    name="Мощность",
                    value=value,
                    associationMethod="same_row",
                )
            ],
            sourceReference={
                "fileName": "Извещение.doc",
                "row": row,
                "sectionRole": "technical_specification",
            },
        )
        for row, value in ((20, "20 Вт"), (21, "40 Вт"))
    ]

    merged, _ = merge_positions(technical_positions + price_positions, None)

    assert len(merged) == 2
    assert [[item.value for item in position.characteristics] for position in merged] == [
        ["20 Вт"],
        ["40 Вт"],
    ]


def test_aligned_sections_merge_a_small_number_of_different_product_labels() -> None:
    price_names = [
        "Лампа A",
        "Лампа B",
        "Светильник ЭРА 80 Вт",
        "Лампа D",
        "Лампа E",
    ]
    technical_names = [
        "Лампа A",
        "Лампа B",
        "Светильник LEDCRAFT 100 Вт",
        "Лампа D",
        "Лампа E",
    ]
    price_positions = [
        TenderPosition(
            product=name,
            quantity=index,
            unit="шт",
            sourceReference={
                "fileName": "Извещение.doc",
                "row": index + 70,
                "sectionRole": "price_justification",
            },
        )
        for index, name in enumerate(price_names, start=1)
    ]
    technical_positions = [
        TenderPosition(
            product=name,
            quantity=index,
            unit="шт",
            characteristics=[
                ProductCharacteristic(
                    name="Мощность",
                    value=f"{index * 10} Вт",
                    associationMethod="same_row",
                )
            ],
            sourceReference={
                "fileName": "Извещение.doc",
                "row": index + 10,
                "sectionRole": "technical_specification",
            },
        )
        for index, name in enumerate(technical_names, start=1)
    ]

    merged, warnings = merge_positions(technical_positions + price_positions, None)

    assert [position.product for position in merged] == price_names
    assert [
        position.characteristics[0].value for position in merged
    ] == ["10 Вт", "20 Вт", "30 Вт", "40 Вт", "50 Вт"]
    assert any("aligned position order" in warning for warning in warnings)


def test_repeated_merged_word_row_is_not_extracted_as_a_product() -> None:
    text = "\n".join(
        (
            "4. ТЕХНИЧЕСКОЕ ЗАДАНИЕ",
            "Таблица Word 1",
            "Строка 1: A: Наименование товара | B: Ед. изм. | C: Количество",
            "Строка 2: A: ИС-618 | B: ИС-618 | C: ИС-618",
            "Строка 3: A: Лампа светодиодная | B: шт | C: 80",
        )
    )

    positions = extract_deterministic_positions(text)

    assert [(item.product, item.quantity) for item in positions] == [
        ("Лампа светодиодная", 80)
    ]


def test_country_of_origin_column_does_not_replace_product_column() -> None:
    text = "\n".join(
        (
            "4. ТЕХНИЧЕСКОЕ ЗАДАНИЕ",
            "Таблица Word 1",
            (
                "Строка 1: A: № п/п | B: Наименование товара | "
                "C: Технические и качественные характеристики товара | "
                "D: Ед. изм. | E: Кол-во | F: Код ОКПД2 | "
                "G: Наименование страны происхождения товара"
            ),
            (
                "Строка 2: A: 1 | B: Генератор дыма | "
                "C: Вид: генератор тумана; Мощность: не менее 550 Вт | "
                "D: шт. | E: 2 | F: 27.11.32.120 | G: "
            ),
            (
                "Строка 3: A: 2 | B: Жидкость для генератора дыма | "
                "C: Объем: не менее 1 л; Время использования: не менее 35 ч | "
                "D: л | E: 10 | F: 20.59.59.900 | G: "
            ),
        )
    )

    positions = extract_deterministic_positions(text)

    assert [position.product for position in positions] == [
        "Генератор дыма",
        "Жидкость для генератора дыма",
    ]
    assert [position.quantity for position in positions] == [2, 10]
    assert all(position.sourceReference is not None for position in positions)
    assert all(position.characteristics for position in positions)
    assert "550 Вт" in positions[0].requirements
    assert "не менее 1 л" in positions[1].requirements


def test_equal_product_names_receive_characteristics_from_their_own_rows() -> None:
    text = "\n".join(
        (
            "4. ТЕХНИЧЕСКОЕ ЗАДАНИЕ",
            "Таблица Word 1",
            (
                "Строка 1: A: № п/п | B: Наименование товара | "
                "C: Технические характеристики товара | D: Ед. изм. | "
                "E: Кол-во | F: Наименование страны происхождения товара"
            ),
            (
                "Строка 2: A: 1 | B: Прожектор | "
                "C: Мощность: не менее 500 Вт | D: шт. | E: 8 | F: "
            ),
            (
                "Строка 3: A: 2 | B: Прожектор | "
                "C: Мощность: не менее 760 Вт | D: шт. | E: 7 | F: "
            ),
        )
    )

    positions = extract_deterministic_positions(text)

    assert [position.product for position in positions] == ["Прожектор", "Прожектор"]
    assert [position.quantity for position in positions] == [8, 7]
    assert "500 Вт" in positions[0].requirements
    assert "760 Вт" in positions[1].requirements
    assert "760 Вт" not in positions[0].requirements
    assert "500 Вт" not in positions[1].requirements


def test_deterministic_word_row_wins_over_llm_characteristic_as_product() -> None:
    reference = {
        "fileName": "Извещение.doc",
        "table": "Таблица Word 4",
        "row": 4,
        "sectionRole": "technical_specification",
    }
    deterministic = [
        TenderPosition(
            candidateId="table:Извещение.doc:Таблица Word 4:4:B",
            product="Генератор дыма",
            productQuery="Генератор дыма; Мощность 550 Вт",
            quantity=2,
            unit="шт.",
            requirements="Мощность: не менее 550 Вт",
            characteristics=[
                ProductCharacteristic(
                    name="Мощность",
                    value="не менее 550 Вт",
                    associationMethod="same_row",
                )
            ],
            source="excel_table_deterministic",
            sourceReference=reference,
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product="Вид: генератор тумана; Мощность: не менее 550 Вт",
                quantity=2,
                unit="шт.",
                source="llm",
                sourceReference=reference,
            )
        ]
    )

    merged, _ = merge_positions(deterministic, llm)

    assert len(merged) == 1
    assert merged[0].product == "Генератор дыма"
    assert merged[0].quantity == 2
    assert [item.value for item in merged[0].characteristics] == [
        "не менее 550 Вт"
    ]


def test_aligned_llm_copy_without_references_does_not_double_table_rows() -> None:
    deterministic = [
        TenderPosition(
            product=name,
            quantity=index,
            unit="шт",
            source="excel_table_deterministic",
            sourceReference={
                "fileName": "Извещение.doc",
                "table": "Таблица Word 4",
                "row": index + 1,
            },
        )
        for index, name in enumerate(
            (
                "Прожектор",
                "Генератор дыма",
                "Жидкость для генератора дыма",
                "Струбцина",
                "Кабель",
            ),
            start=1,
        )
    ]
    llm = TenderPositionsResponse(
        products=[
            TenderPosition(
                product=(
                    "Мощность: 550 Вт"
                    if index == 3
                    else source_position.product
                ),
                quantity=source_position.quantity,
                unit=source_position.unit,
                source="llm",
            )
            for index, source_position in enumerate(deterministic, start=1)
        ]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert len(merged) == 5
    assert [position.product for position in merged] == [
        position.product for position in deterministic
    ]
    assert any("aligned LLM copy" in warning for warning in warnings)


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
    assert len(merged) == 2
    priced = next(item for item in merged if item.documentPriceSource is not None)
    assert priced.documentUnitPriceRub == 100
    assert priced.documentLineTotalRub == 500
    assert priced.documentPriceSource.extractionMethod == "excel_deterministic"


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

    assert [item.quantity for item in merged] == [1, 200, 70]

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

    assert len(merged) == 4
    """
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


    """


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


def test_deterministic_excel_and_merge_preserve_2500_positions() -> None:
    rows = [
        {
            "row": 1,
            "cells": {
                "A": "Наименование товара",
                "B": "Ед. изм.",
                "C": "Количество",
            },
        }
    ]
    rows.extend(
        {
            "row": index + 1,
            "cells": {
                "A": f"Уникальный товар {index}",
                "B": "шт",
                "C": "1",
            },
        }
        for index in range(1, 2501)
    )

    deterministic = extract_deterministic_positions(
        "",
        [{"fileName": "large.xlsx", "sheet": "Лист1", "rows": rows}],
        max_positions=5_000,
    )
    merged, _ = merge_positions(
        deterministic,
        TenderPositionsResponse(),
        max_positions=5_000,
    )

    assert len(deterministic) == 2500
    assert len(merged) == 2500
    assert merged[-1].product == "Уникальный товар 2500"


def test_excel_characteristics_are_extracted_from_arbitrary_columns() -> None:
    positions = extract_deterministic_positions(
        "",
        [
            {
                "fileName": "spec.xlsx",
                "sheet": "Лист1",
                "rows": [
                    {
                        "row": 3,
                        "cells": {
                            "B": "Количество",
                            "D": "Напряжение питания",
                            "F": "Наименование товара",
                            "H": "Ед. изм.",
                            "K": "Частота",
                        },
                    },
                    {
                        "row": 4,
                        "cells": {
                            "B": "2",
                            "D": "220 В",
                            "F": "БУРС-1В",
                            "H": "шт",
                            "K": "50 Гц",
                        },
                    },
                ],
            }
        ],
    )

    assert len(positions) == 1
    assert [(item.name, item.value) for item in positions[0].characteristics] == [
        ("Напряжение питания", "220 В"),
        ("Частота", "50 Гц"),
    ]
    assert "Напряжение питания: 220 В" in positions[0].requirements


def test_excel_continuation_row_enriches_previous_position() -> None:
    positions = extract_deterministic_positions(
        "",
        [
            {
                "fileName": "spec.xlsx",
                "sheet": "Лист1",
                "headerRows": [1],
                "headerMap": {
                    "product": "B",
                    "unit": "C",
                    "quantity": "D",
                },
                "headerLabels": {
                    "product": "Наименование",
                    "unit": "Ед. изм.",
                    "quantity": "Количество",
                },
                "rows": [
                    {
                        "row": 1,
                        "cells": {
                            "B": "Наименование",
                            "C": "Ед. изм.",
                            "D": "Количество",
                            "E": "Параметр",
                        },
                    },
                    {
                        "row": 2,
                        "cells": {
                            "B": "Насос центробежный",
                            "C": "шт",
                            "D": "1",
                            "E": "Подача 20 м3/ч",
                        },
                    },
                    {
                        "row": 3,
                        "cells": {
                            "E": "Напор 30 м",
                        },
                    },
                ],
            }
        ],
    )

    assert len(positions) == 1
    assert [item.value for item in positions[0].characteristics] == [
        "Подача 20 м3/ч",
        "Напор 30 м",
    ]
    assert positions[0].characteristics[1].associationMethod == "continuation_row"
