from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _identity(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", "", text)


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must contain an object")
            records.append(value)
    return records


def extract_prediction_items(payload: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if isinstance(value.get("match"), dict) and (
            value.get("positionKey") or value.get("product") or value.get("productQuery")
        ):
            result.append(value)
            return
        for key in ("productCheck", "details", "items", "products", "positions"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                visit(nested)

    visit(payload)
    return result


def _gold_source_product(record: dict[str, Any]) -> str:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    return _clean(_first(record, "sourceProduct", "product") or source.get("product"))


def _gold_expected(record: dict[str, Any]) -> dict[str, Any]:
    expected = record.get("expected")
    return expected if isinstance(expected, dict) else {}


def _string_set(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {_identity(item) for item in values if _identity(item)}


def _prediction_match(item: dict[str, Any]) -> dict[str, Any]:
    match = item.get("match")
    return match if isinstance(match, dict) else {}


def evaluate_product_matching(
    golden_records: Iterable[dict[str, Any]],
    prediction_payload: Any,
) -> dict[str, Any]:
    golden = list(golden_records)
    predictions = extract_prediction_items(prediction_payload)
    by_key = {
        _clean(item.get("positionKey")): index
        for index, item in enumerate(predictions)
        if _clean(item.get("positionKey"))
    }
    by_product: dict[str, list[int]] = {}
    for index, item in enumerate(predictions):
        by_product.setdefault(_identity(item.get("product")), []).append(index)

    used: set[int] = set()
    details: list[dict[str, Any]] = []
    correct_count = 0
    linked_count = 0
    expected_match_count = 0
    false_not_found_count = 0
    wrong_match_count = 0
    required_token_count = 0
    present_required_token_count = 0
    forbidden_token_count = 0
    leaked_forbidden_token_count = 0
    category_totals: dict[str, dict[str, int]] = {}

    for ordinal, record in enumerate(golden, start=1):
        expected = _gold_expected(record)
        source_product = _gold_source_product(record)
        position_key = _clean(record.get("positionKey"))
        prediction_index = by_key.get(position_key) if position_key else None
        if prediction_index in used:
            prediction_index = None
        if prediction_index is None:
            candidates = [
                index
                for index in by_product.get(_identity(source_product), [])
                if index not in used
            ]
            if len(candidates) == 1:
                prediction_index = candidates[0]

        prediction = predictions[prediction_index] if prediction_index is not None else None
        if prediction_index is not None:
            used.add(prediction_index)
            linked_count += 1

        outcome = _clean(expected.get("outcome") or "matched").casefold()
        expected_not_found = outcome in {"not_found", "товар не найден"}
        if not expected_not_found:
            expected_match_count += 1
        expected_ids = _string_set(
            expected.get("catalogProductIds")
            or expected.get("productIds")
            or expected.get("productId")
        )
        expected_articles = _string_set(
            expected.get("articles") or expected.get("article")
        )

        match = _prediction_match(prediction or {})
        correspondence = _clean(_first(match, "correspondence", "Соответствие"))
        predicted_id = _clean(_first(match, "product_id", "productId", "ID товара"))
        predicted_article = _clean(_first(match, "article", "Артикул"))
        predicted_name = _clean(_first(match, "name", "Наименование"))
        actual_not_found = (
            prediction is None
            or correspondence.casefold() == "товар не найден"
            or not (predicted_id or predicted_article or predicted_name)
        )

        if expected_not_found:
            correct = actual_not_found
            if not actual_not_found:
                wrong_match_count += 1
        else:
            id_matches = bool(expected_ids and _identity(predicted_id) in expected_ids)
            article_matches = bool(
                expected_articles and _identity(predicted_article) in expected_articles
            )
            correct = not actual_not_found and (id_matches or article_matches)
            if actual_not_found:
                false_not_found_count += 1
            elif not correct:
                wrong_match_count += 1
        correct_count += int(correct)

        prediction_data = prediction or {}
        search_characteristics = prediction_data.get("searchCharacteristics", [])
        if not isinstance(search_characteristics, list):
            search_characteristics = []
        query_text = _clean(
            " ".join(
                [
                    _clean(prediction_data.get("productQuery")),
                    *[_clean(value) for value in search_characteristics],
                ]
            )
        )
        query_identity = _identity(query_text)
        required_tokens = [
            _clean(item)
            for item in expected.get("mustContainSearchTokens", [])
            if _clean(item)
        ]
        forbidden_tokens = [
            _clean(item)
            for item in expected.get("mustNotContainSearchTokens", [])
            if _clean(item)
        ]
        missing_required = [
            token for token in required_tokens if _identity(token) not in query_identity
        ]
        leaked_forbidden = [
            token for token in forbidden_tokens if _identity(token) in query_identity
        ]
        required_token_count += len(required_tokens)
        present_required_token_count += len(required_tokens) - len(missing_required)
        forbidden_token_count += len(forbidden_tokens)
        leaked_forbidden_token_count += len(leaked_forbidden)

        category = _clean(record.get("categoryCode") or "unclassified")
        category_stat = category_totals.setdefault(category, {"cases": 0, "correct": 0})
        category_stat["cases"] += 1
        category_stat["correct"] += int(correct)
        details.append(
            {
                "caseId": _clean(record.get("caseId") or f"case-{ordinal}"),
                "positionKey": position_key,
                "sourceProduct": source_product,
                "linked": prediction is not None,
                "expectedOutcome": "not_found" if expected_not_found else "matched",
                "predictedProductId": predicted_id,
                "predictedArticle": predicted_article,
                "predictedCorrespondence": correspondence,
                "correct": correct,
                "missingRequiredSearchTokens": missing_required,
                "leakedForbiddenSearchTokens": leaked_forbidden,
            }
        )

    total = len(golden)
    return {
        "cases": total,
        "linkedPredictions": linked_count,
        "positionLinkRate": linked_count / total if total else 0.0,
        "correctSelections": correct_count,
        "top1Accuracy": correct_count / total if total else 0.0,
        "expectedMatchedCases": expected_match_count,
        "falseNotFoundRate": (
            false_not_found_count / expected_match_count if expected_match_count else 0.0
        ),
        "wrongMatchRate": wrong_match_count / total if total else 0.0,
        "requiredSearchTokenRecall": (
            present_required_token_count / required_token_count
            if required_token_count
            else None
        ),
        "forbiddenSearchTokenLeakRate": (
            leaked_forbidden_token_count / forbidden_token_count
            if forbidden_token_count
            else None
        ),
        "byCategory": {
            category: {
                **values,
                "accuracy": values["correct"] / values["cases"],
            }
            for category, values in sorted(category_totals.items())
        },
        "details": details,
    }
