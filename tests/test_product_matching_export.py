from io import BytesIO

from openpyxl import load_workbook

from app.services.product_matching_export import build_product_matching_workbook


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
