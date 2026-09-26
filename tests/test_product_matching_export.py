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
    assert sheet["O2"].value == "spec.xlsx / Лист1 / row 7"
    assert sheet["P2"].value == "pos_test"
    assert sheet["Q2"].value == "Сечение: 2,5 мм2 [same_row, 0.98]"
    assert sheet["R2"].value == "Кабель ВВГнг 3x2,5"
    assert sheet["S2"].value == "3x2,5"
    assert sheet["T2"].value == "electrical_lighting"
    assert sheet["U2"].value == "Кабель ВВГнг\nКабель ВВГнг 3x2,5"
