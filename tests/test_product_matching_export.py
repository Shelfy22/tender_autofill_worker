from io import BytesIO

from openpyxl import load_workbook
import pytest

from app.services.product_matching_export import HEADERS, MATCHING_NOTE_HINT, build_product_matching_workbook


@pytest.mark.skip(reason="Superseded by the approved final employee template.")
def test_build_product_matching_workbook_contains_expected_columns_and_values() -> None:
    workbook_bytes = build_product_matching_workbook(
        {
            "details": [
                {
                    "sourceProduct": "Кабель ВВГнг 3x2.5",
                    "quantity": 10,
                    "unit": "м",
                    "article": "A-1",
                    "link": "https://example.test/item",
                    "productId": "1001",
                    "medianUnitPriceRub": 42.5,
                    "positionTotalPriceRub": 425,
                    "sourceReference": {"fileName": "spec.xlsx", "sheet": "Лист1", "row": 7},
                    "positionKey": "pos_test",
                    "sourceCharacteristics": [
                        {
                            "name": "Сечение",
                            "value": "2,5 мм2",
                            "associationMethod": "same_row",
                            "associationConfidence": 0.98,
                        }
                    ],
                    "productQuery": "Кабель ВВГнг 3x2,5",
                    "searchCharacteristics": ["3x2,5"],
                    "searchCategoryCode": "electrical_lighting",
                    "searchQueries": [
                        "Кабель ВВГнг",
                        "Кабель ВВГнг 3x2,5",
                    ],
                    "result": {
                        "Наименование": "Кабель силовой",
                        "Производитель": "ETM",
                        "Валюта": "RUB",
                        "Соответствие": "полное",
                        "Обоснование": "Совпали тип и сечение",
                    },
                }
            ]
        }
    )

    workbook = load_workbook(BytesIO(workbook_bytes))
    sheet = workbook.active

    assert sheet.title == "Автоподбор"
    assert sheet["A1"].value == "N"
    assert sheet["B1"].value == "Название товара тендера"
    assert sheet["B2"].value == "Кабель ВВГнг 3x2.5"
    assert sheet["E2"].value == "A-1"
    assert sheet["H2"].value == "Кабель силовой"
    headers = [cell.value for cell in sheet[1]]
    assert headers == [
        "N", "Название товара тендера", "Количество", "Ед. изм.", "Артикул", "Ссылка",
        "Код ETM", "Наименование ETM", "Производитель", "Медианная цена", "Валюта",
        "Сумма позиции", "Характеристики из документов",
        "Характеристики выбранного товара ETM", "Обоснование (почему товар подобран)",
        "Корректно ли подобрана позиция (да / нет)", "Примечание о подборе",
    ]
    assert sheet["M2"].value == "Сечение: 2,5 мм2"
    assert sheet["N2"].value is None
    assert sheet["O2"].value == "Совпали тип и сечение"
    assert sheet["P2"].value is None
    assert sheet["Q2"].value == "В случае если товар подобран не верно, указать что именно"
    assert len(sheet.data_validations.dataValidation) == 1
    assert sheet.data_validations.dataValidation[0].formula1 == '"Да,Нет"'
    assert str(sheet.data_validations.dataValidation[0].sqref) == "P2"


@pytest.mark.skip(reason="Superseded by the approved final employee template.")
def test_export_shows_params_only_for_selected_catalog_product() -> None:
    workbook_bytes = build_product_matching_workbook(
        {"details": [
            {"sourceProduct": "Кабель", "result": {"Наименование": "Кабель ETM"}},
            {"sourceProduct": "Лампа", "result": {"Соответствие": "Товар не найден"}},
        ]},
        selected_catalog_params=[
            {"Число жил": "3", "Сечение": "2,5 мм2"},
            {},
        ],
    )

    sheet = load_workbook(BytesIO(workbook_bytes)).active
    assert sheet["N2"].value == "Число жил: 3\nСечение: 2,5 мм2"
    assert sheet["N3"].value is None
    assert sheet["Q2"].value == "В случае если товар подобран не верно, указать что именно"
    assert sheet["Q3"].value is None
    assert sheet.max_column == 17
    assert str(sheet.data_validations.dataValidation[0].sqref) == "P2:P3"


@pytest.mark.skip(reason="Superseded by the approved final employee template.")
def test_export_replaces_semicolons_with_colons_in_all_text_cells() -> None:
    workbook_bytes = build_product_matching_workbook(
        {
            "details": [
                {
                    "sourceProduct": "cable; 3x2.5",
                    "searchCharacteristics": ["core; copper"],
                    "searchQueries": ["cable; power"],
                    "sourceCharacteristics": [
                        {"name": "core; material", "value": "copper; tinned"}
                    ],
                }
            ]
        }
    )

    sheet = load_workbook(BytesIO(workbook_bytes)).active

    assert sheet["B2"].value == "cable: 3x2.5"
    assert sheet["M2"].value == "core: material: copper: tinned"
    assert all(";" not in str(cell.value) for row in sheet.iter_rows() for cell in row)


def test_export_matches_final_employee_template() -> None:
    workbook_bytes = build_product_matching_workbook(
        {
            "details": [
                {
                    "sourceProduct": "Tender cable",
                    "quantity": 10,
                    "unit": "m",
                    "article": "A-1",
                    "link": "https://example.test/item",
                    "productId": "1001",
                    "medianUnitPriceRub": 42.5,
                    "positionTotalPriceRub": 425,
                    "sourceCharacteristics": [
                        {"name": "Section", "value": "2.5 mm2"}
                    ],
                    "result": {
                        "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435": "ETM cable",
                        "\u041f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c": "ETM",
                        "\u0412\u0430\u043b\u044e\u0442\u0430": "RUB",
                        "\u041e\u0431\u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435": "Matched type and section",
                    },
                }
            ]
        },
        selected_catalog_params=[{"Section": "2.5 mm2"}],
    )

    sheet = load_workbook(BytesIO(workbook_bytes)).active
    expected_headers = [
        "N",
        "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0442\u043e\u0432\u0430\u0440\u0430 \u0432 \u0442\u0435\u043d\u0434\u0435\u0440\u0435",
        "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 \u0442\u043e\u0432\u0430\u0440\u0430 \u042d\u0422\u041c",
        "\u041a\u043e\u0434 \u042d\u0422\u041c",
        "\u0421\u0441\u044b\u043b\u043a\u0430 \u042d\u0422\u041c",
        "\u041f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c",
        "\u041a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e",
        "\u041c\u0435\u0434\u0438\u0430\u043d\u043d\u0430\u044f \u0446\u0435\u043d\u0430",
        "\u0412\u0430\u043b\u044e\u0442\u0430",
        "\u0421\u0443\u043c\u043c\u0430",
        "\u0425\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438 \u0438\u0437 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430\u0446\u0438\u0438",
        "\u0425\u0430\u0440\u0430\u043a\u0442\u0435\u0440\u0438\u0441\u0442\u0438\u043a\u0438 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u0433\u043e \u0442\u043e\u0432\u0430\u0440\u0430 \u042d\u0422\u041c",
        "\u041e\u0431\u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435 (\u043f\u043e\u0447\u0435\u043c\u0443 \u0442\u043e\u0432\u0430\u0440 \u043f\u043e\u0434\u043e\u0431\u0440\u0430\u043d)",
        "\u041a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u043e\u0441\u0442\u044c \u043f\u043e\u0434\u0431\u043e\u0440\u0430",
        "\u041f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435 \u043e \u043f\u043e\u0434\u0431\u043e\u0440\u0435",
    ]
    assert [cell.value for cell in sheet[1]] == expected_headers
    assert HEADERS == expected_headers
    assert sheet["B2"].value == "Tender cable"
    assert sheet["C2"].value == "ETM cable"
    assert sheet["D2"].value == "1001"
    assert sheet["E2"].value == "https://example.test/item"
    assert sheet["F2"].value == "ETM"
    assert sheet["G2"].value == 10
    assert sheet["H2"].value == 42.5
    assert sheet["J2"].value == 425
    assert sheet["K2"].value == "Section: 2.5 mm2"
    assert sheet["L2"].value == "Section: 2.5 mm2"
    assert sheet["M2"].value == "Matched type and section"
    assert sheet["N2"].value is None
    assert sheet["O2"].value == MATCHING_NOTE_HINT
    assert len(sheet.data_validations.dataValidation) == 0


def test_export_keeps_correctness_column_manual_and_replaces_semicolons() -> None:
    workbook_bytes = build_product_matching_workbook(
        {
            "details": [
                {
                    "sourceProduct": "cable; 3x2.5",
                    "sourceCharacteristics": [
                        {"name": "core; material", "value": "copper; tinned"}
                    ],
                },
                {"sourceProduct": "Lamp"},
            ]
        }
    )

    sheet = load_workbook(BytesIO(workbook_bytes)).active
    assert sheet.max_column == 15
    assert sheet["B2"].value == "cable: 3x2.5"
    assert sheet["K2"].value == "core: material: copper: tinned"
    assert sheet["N2"].value is None
    assert sheet["N3"].value is None
    assert sheet["O2"].value == MATCHING_NOTE_HINT
    assert sheet["O3"].value is None
    assert len(sheet.data_validations.dataValidation) == 0
    assert all(";" not in str(cell.value) for row in sheet.iter_rows() for cell in row)
