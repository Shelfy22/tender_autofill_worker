from __future__ import annotations

from app.services.product_matching_evaluation import (
    evaluate_product_matching,
    extract_prediction_items,
)


def test_extract_prediction_items_from_product_check_details() -> None:
    payload = {
        "productCheck": {
            "details": [
                {
                    "positionKey": "pos_1",
                    "product": "Кабель",
                    "match": {"ID товара": "CAT-1"},
                }
            ]
        }
    }

    assert extract_prediction_items(payload)[0]["positionKey"] == "pos_1"


def test_evaluation_measures_selection_and_search_tokens() -> None:
    golden = [
        {
            "caseId": "case-1",
            "positionKey": "pos_1",
            "categoryCode": "electrical_lighting",
            "source": {"product": "Кабель ПВС"},
            "expected": {
                "outcome": "matched",
                "catalogProductIds": ["CAT-1"],
                "mustContainSearchTokens": ["3x0,75"],
                "mustNotContainSearchTokens": ["10 шт"],
            },
        },
        {
            "caseId": "case-2",
            "positionKey": "pos_2",
            "categoryCode": "other",
            "source": {"product": "Редкое изделие"},
            "expected": {"outcome": "not_found"},
        },
    ]
    predictions = {
        "productCheck": {
            "details": [
                {
                    "positionKey": "pos_1",
                    "product": "Кабель ПВС",
                    "productQuery": "Кабель ПВС 3x0,75",
                    "searchCharacteristics": ["3x0,75"],
                    "match": {
                        "ID товара": "CAT-1",
                        "Артикул": "PVS",
                        "Наименование": "Кабель ПВС",
                        "Соответствие": "Полное соответствие",
                    },
                },
                {
                    "positionKey": "pos_2",
                    "product": "Редкое изделие",
                    "productQuery": "Редкое изделие",
                    "match": {
                        "Соответствие": "Товар не найден",
                    },
                },
            ]
        }
    }

    report = evaluate_product_matching(golden, predictions)

    assert report["positionLinkRate"] == 1.0
    assert report["top1Accuracy"] == 1.0
    assert report["falseNotFoundRate"] == 0.0
    assert report["wrongMatchRate"] == 0.0
    assert report["requiredSearchTokenRecall"] == 1.0
    assert report["forbiddenSearchTokenLeakRate"] == 0.0


def test_evaluation_counts_missing_prediction_as_false_not_found() -> None:
    golden = [
        {
            "caseId": "case-1",
            "source": {"product": "Лампа"},
            "expected": {"outcome": "matched", "articles": ["A-1"]},
        }
    ]
    predictions = {
        "details": [
            {
                "product": "Лампа",
                "match": {"Соответствие": "Товар не найден"},
            },
            {
                "product": "Лампа",
                "match": {"Артикул": "A-1", "Соответствие": "Полное соответствие"},
            },
        ]
    }

    report = evaluate_product_matching(golden, predictions)

    assert report["positionLinkRate"] == 0.0
    assert report["top1Accuracy"] == 0.0
    assert report["falseNotFoundRate"] == 1.0
