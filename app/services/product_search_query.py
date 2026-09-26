from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.models import (
    ProductSemanticClassification,
    ProductSkuClassification,
    SemanticCharacteristic,
    TenderPosition,
)
from app.services.product_characteristics import normalize_product_category


_NON_SEARCH_ROLES = {"TEMPORAL", "QUANTITY"}
_SEARCH_USAGES = {"SEARCH_PRIMARY", "SEARCH_SECONDARY"}
_NUMBER_WITH_UNIT = re.compile(
    r"(-?\d+(?:[.,]\d+)?)\s*([%°A-Za-zА-Яа-яЁё0-9³²/·*]+)?"
)
_UNIT_AFTER_NUMBER = re.compile(
    r"-?\d+(?:[.,]\d+)?\s*([%°A-Za-zА-Яа-яЁё³²/·*]+)"
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _identity(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _split_requirements(value: str) -> list[tuple[str, str]]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"[\r\n]+|\s*\|\s*|\s*;\s*(?=[^;:]{1,80}:)", text)
    result: list[tuple[str, str]] = []
    for part in parts:
        part = _clean(part)
        if not part:
            continue
        name, separator, raw_value = part.partition(":")
        if separator and name and raw_value:
            result.append((_clean(name), _clean(raw_value)))
        else:
            result.append(("", part))
    return result


def _characteristic_candidates(position: TenderPosition) -> list[dict[str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for characteristic in position.characteristics:
        if (
            characteristic.associationStatus == "conflicting"
            or (
                characteristic.associationConfidence > 0
                and characteristic.associationConfidence < 0.80
            )
        ):
            continue
        candidates.append(
            (
                _clean(characteristic.name),
                _clean(characteristic.value),
                _clean(characteristic.evidence),
            )
        )
    if not candidates:
        candidates.extend(
            (name, value, "requirements")
            for name, value in _split_requirements(position.requirements)
        )

    if not candidates:
        reference = position.sourceReference
        price_reference = position.documentPriceSource
        excluded_columns = {
            value
            for value in (
                reference.productColumn if reference else "",
                reference.quantityColumn if reference else "",
                reference.unitColumn if reference else "",
                price_reference.unitPriceColumn if price_reference else "",
                price_reference.lineTotalColumn if price_reference else "",
            )
            if value
        }
        product_identity = _identity(position.product)
        unit_identity = _identity(position.unit)
        quantity_values = {
            str(position.quantity),
            f"{position.quantity:g}" if position.quantity is not None else "",
        }
        for column, raw_value in position.sourceCells.items():
            value = _clean(raw_value)
            value_identity = _identity(value)
            if (
                not value
                or column in excluded_columns
                or value_identity == product_identity
                or value_identity == unit_identity
                or value in quantity_values
            ):
                continue
            candidates.append((f"Колонка {column}", value, "sourceCells"))

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, value, evidence in candidates:
        value = value[:500]
        key = (_identity(name), _identity(value))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "id": f"c{len(result) + 1}",
                "name": name[:200],
                "value": value,
                "evidence": evidence[:300],
            }
        )
        if len(result) >= 40:
            break
    return result


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return (f"{value:.6f}".rstrip("0").rstrip(".")).replace(".", ",")


def _compact_token(value: Any) -> str:
    token = _clean(value)
    token = re.sub(r"(?<=\d)\s+(?=[%°A-Za-zА-Яа-яЁё])", "", token)
    token = re.sub(r"(?<=[A-Za-zА-Яа-яЁё])\s+(?=\d)", "", token)
    return token[:120]


def _boundary_token(characteristic: SemanticCharacteristic, value: float | None) -> str:
    if value is None:
        return _compact_token(characteristic.search_token or characteristic.original_value)
    unit = _clean(characteristic.numeric.unit)
    if not unit:
        match = _NUMBER_WITH_UNIT.search(
            characteristic.search_token or characteristic.original_value
        )
        unit = _clean(match.group(2)) if match and match.group(2) else ""
    return _compact_token(f"{_format_number(value)}{unit}")


def _numbers(value: Any) -> list[float]:
    return [
        float(item.replace(",", "."))
        for item in re.findall(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    ]


def _canonical_unit(value: Any) -> str:
    unit = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    unit = re.sub(r"[^%°a-zа-я0-9/]+", "", unit)
    aliases = {
        "v": "v",
        "в": "v",
        "kv": "kv",
        "кв": "kv",
        "a": "a",
        "а": "a",
        "ma": "ma",
        "ма": "ma",
        "w": "w",
        "вт": "w",
        "kw": "kw",
        "квт": "kw",
        "hz": "hz",
        "гц": "hz",
        "mm": "mm",
        "мм": "mm",
        "mm2": "mm2",
        "мм2": "mm2",
        "cm": "cm",
        "см": "cm",
        "m": "m",
        "м": "m",
        "m2": "m2",
        "м2": "m2",
        "m3": "m3",
        "м3": "m3",
        "m3/h": "m3/h",
        "м3/ч": "m3/h",
        "l": "l",
        "л": "l",
        "kg": "kg",
        "кг": "kg",
        "g": "g",
        "г": "g",
        "rpm": "rpm",
        "об/мин": "rpm",
    }
    return aliases.get(unit, unit)


def _units_after_numbers(value: Any) -> set[str]:
    return {
        canonical
        for match in _UNIT_AFTER_NUMBER.finditer(str(value or ""))
        if (canonical := _canonical_unit(match.group(1)))
    }


def _search_token_is_grounded(
    characteristic: SemanticCharacteristic,
    token: str,
) -> bool:
    allowed_numbers = _numbers(characteristic.original_value)
    numeric = characteristic.numeric
    allowed_numbers.extend(
        value
        for value in (numeric.min, numeric.max, numeric.nominal)
        if value is not None
    )
    if numeric.min is not None and numeric.max is not None:
        allowed_numbers.append((numeric.min + numeric.max) / 2)
    for token_number in _numbers(token):
        if not any(abs(token_number - allowed) < 1e-6 for allowed in allowed_numbers):
            return False

    source_units = _units_after_numbers(characteristic.original_value)
    token_units = _units_after_numbers(token)
    if token_units and source_units and not token_units.issubset(source_units):
        return False
    if token_units and not source_units:
        role_name = _identity(characteristic.name)
        if not (token_units == {"p"} and "полюс" in role_name):
            return False

    if not _numbers(token):
        source_words = set(_identity(f"{characteristic.name} {characteristic.original_value}").split())
        token_words = set(_identity(token).split())
        if token_words and not token_words.issubset(source_words):
            return False
    return True


def normalize_search_characteristic(
    characteristic: SemanticCharacteristic,
) -> dict[str, Any] | None:
    if (
        characteristic.semantic_role in _NON_SEARCH_ROLES
        or characteristic.already_encoded_in_name
    ):
        return None

    value_form = characteristic.value_form
    numeric = characteristic.numeric
    if value_form == "ACCEPTABLE_RANGE" and numeric.min is not None and numeric.max is not None:
        token = _boundary_token(characteristic, (numeric.min + numeric.max) / 2)
    elif value_form == "MINIMUM":
        token = _boundary_token(
            characteristic,
            numeric.min if numeric.min is not None else numeric.nominal,
        )
    elif value_form == "MAXIMUM":
        token = _boundary_token(
            characteristic,
            numeric.max if numeric.max is not None else numeric.nominal,
        )
    elif value_form == "TOLERANCE":
        token = _boundary_token(characteristic, numeric.nominal)
    else:
        token = _compact_token(characteristic.search_token or characteristic.original_value)
    if not token:
        return None
    if not _search_token_is_grounded(characteristic, token):
        return None
    return {
        "id": characteristic.id,
        "name": characteristic.name,
        "semantic_role": characteristic.semantic_role,
        "value_form": characteristic.value_form,
        "search_token": token,
        "source_value": characteristic.original_value,
        "token_verified": True,
    }


def _first_number_token(value: str) -> str:
    match = re.search(r"-?\d+(?:[.,]\d+)?", value)
    return match.group(0) if match else ""


def _compose_search_tokens(items: list[dict[str, Any]]) -> list[str]:
    by_name = {str(item.get("name") or ""): item for item in items}
    consumed: set[str] = set()
    composed: list[str] = []

    cores = by_name.get("cable.cores")
    section = by_name.get("cable.cross_section")
    if cores and section:
        core_value = _first_number_token(str(cores["search_token"]))
        section_value = _first_number_token(str(section["search_token"]))
        if core_value and section_value:
            composed.append(f"{core_value}x{section_value}")
            consumed.update({str(cores["id"]), str(section["id"])})

    thread = by_name.get("connection.thread")
    length = by_name.get("dimension.length")
    if thread and length and str(thread["id"]) not in consumed:
        thread_token = _compact_token(thread["search_token"])
        length_value = _first_number_token(str(length["search_token"]))
        if re.fullmatch(r"M\d+(?:[xх][\d,.]+)?", thread_token, re.IGNORECASE) and length_value:
            composed.append(f"{thread_token}x{length_value}")
            consumed.update({str(thread["id"]), str(length["id"])})

    dimensions = [
        by_name.get("dimension.length"),
        by_name.get("dimension.width"),
        by_name.get("dimension.height"),
    ]
    if all(dimensions) and not any(str(item["id"]) in consumed for item in dimensions if item):
        values = [_first_number_token(str(item["search_token"])) for item in dimensions if item]
        if all(values):
            unit_match = re.search(r"[A-Za-zА-Яа-яЁё²³]+$", str(dimensions[-1]["search_token"]))
            composed.append("x".join(values) + (unit_match.group(0) if unit_match else ""))
            consumed.update(str(item["id"]) for item in dimensions if item)

    for item in items:
        if str(item["id"]) not in consumed:
            composed.append(_compact_token(item["search_token"]))
    return list(dict.fromkeys(token for token in composed if token))


def _safe_base_name(position: TenderPosition, semantic: ProductSemanticClassification) -> str:
    original = _clean(position.product)
    normalized = _clean(semantic.normalized_product)
    if not normalized:
        return original
    original_codes = {
        token.casefold()
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._/-]*", original)
        if any(character.isdigit() for character in token)
    }
    normalized_folded = normalized.casefold()
    if any(code not in normalized_folded for code in original_codes):
        return original
    return normalized


def build_search_query(
    position: TenderPosition,
    semantic: ProductSemanticClassification,
    sku: ProductSkuClassification,
    normalized: list[dict[str, Any]],
) -> TenderPosition:
    by_id = {item["id"]: item for item in normalized}
    decision_by_id = {decision.id: decision for decision in sku.decisions}
    selected_items: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for characteristic_id in sku.selected_for_search:
        item = by_id.get(characteristic_id)
        decision = decision_by_id.get(characteristic_id)
        if item is None or decision is None or decision.usage not in _SEARCH_USAGES:
            continue
        token = _compact_token(item["search_token"])
        token_key = _identity(token)
        if not token or token_key in seen_tokens:
            continue
        seen_tokens.add(token_key)
        selected_items.append({**item, "search_token": token})
        if len(selected_items) >= 5:
            break

    base = _safe_base_name(position, semantic)
    for item in selected_items:
        item["name"] = next(
            (
                characteristic.normalizedName
                for characteristic in position.characteristics
                if _identity(characteristic.value) == _identity(item.get("source_value"))
                and characteristic.normalizedName
            ),
            item.get("name") or "",
        )
    selected = _compose_search_tokens(selected_items)
    query = _clean(" ".join([base, *selected])) or _clean(
        position.productQuery or position.product
    )
    exact_identity = _clean(
        " ".join(filter(None, (position.brand, position.model, position.article)))
    )
    query_variants = list(
        dict.fromkeys(
            value
            for value in (
                exact_identity,
                base,
                _clean(" ".join([base, *selected[:1]])),
                query,
            )
            if value
        )
    )[:4]
    return position.model_copy(
        update={
            "productQuery": query,
            "searchCharacteristics": selected,
            "searchCategory": _clean(semantic.category),
            "searchCategoryCode": normalize_product_category(
                semantic.category,
                position.product,
            ),
            "searchQueries": query_variants,
        }
    )


def enrich_product_search_queries(
    llm: Any,
    positions: list[TenderPosition],
    *,
    batch_size: int = 20,
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    if not positions:
        return [], [], {
            "applied": False,
            "positionCount": 0,
            "enrichedCount": 0,
            "batches": [],
        }

    result = list(positions)
    warnings: list[str] = []
    batch_debug: list[dict[str, Any]] = []
    enriched_count = 0
    candidates_by_index = {
        index: _characteristic_candidates(position)
        for index, position in enumerate(positions, start=1)
    }

    work_indexes = [index for index, candidates in candidates_by_index.items() if candidates]
    for offset in range(0, len(work_indexes), max(1, batch_size)):
        indexes = work_indexes[offset : offset + max(1, batch_size)]
        semantic_input = [
            {
                "position_index": index,
                "product": positions[index - 1].product,
                "brand": positions[index - 1].brand,
                "article": positions[index - 1].article,
                "characteristics": candidates_by_index[index],
            }
            for index in indexes
        ]
        debug_item: dict[str, Any] = {
            "positionIndexes": indexes,
            "semantic": "pending",
            "sku": "pending",
        }
        try:
            semantic_response = llm.classify_product_characteristics(semantic_input)
        except Exception as exc:
            debug_item.update(
                {
                    "semantic": "failed",
                    "sku": "skipped",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            batch_debug.append(debug_item)
            warnings.append(
                "Семантическая классификация характеристик недоступна; исходные productQuery "
                f"сохранены для позиций {indexes[0]}-{indexes[-1]}: {type(exc).__name__}: {exc}"
            )
            continue

        semantic_by_index = {item.position_index: item for item in semantic_response.results}
        normalized_by_index: dict[int, list[dict[str, Any]]] = {}
        sku_input: list[dict[str, Any]] = []
        for index in indexes:
            semantic = semantic_by_index.get(index)
            expected_ids = {item["id"] for item in candidates_by_index[index]}
            returned_ids = {item.id for item in semantic.characteristics} if semantic else set()
            if semantic is None or returned_ids != expected_ids:
                warnings.append(
                    f"LLM №1 вернула неполную классификацию позиции {index}; исходный productQuery сохранён."
                )
                continue
            normalized = [
                item
                for characteristic in semantic.characteristics
                if (item := normalize_search_characteristic(characteristic)) is not None
            ]
            normalized_by_index[index] = normalized
            if not normalized:
                position = positions[index - 1]
                result[index - 1] = position.model_copy(
                    update={
                        "productQuery": _safe_base_name(position, semantic),
                        "searchCharacteristics": [],
                        "searchCategory": _clean(semantic.category),
                        "searchCategoryCode": normalize_product_category(
                            semantic.category,
                            position.product,
                        ),
                        "searchQueries": [_safe_base_name(position, semantic)],
                    }
                )
                enriched_count += 1
                continue
            sku_input.append(
                {
                    "position_index": index,
                    "category": semantic.category,
                    "original_product": positions[index - 1].product,
                    "identifier_strength": semantic.identifier_strength,
                    "characteristics": normalized,
                }
            )

        debug_item["semantic"] = "completed"
        debug_item["semanticResultCount"] = len(semantic_response.results)
        if not sku_input:
            debug_item["sku"] = "skipped"
            batch_debug.append(debug_item)
            continue
        try:
            sku_response = llm.classify_sku_importance(sku_input)
        except Exception as exc:
            debug_item.update({"sku": "failed", "error": f"{type(exc).__name__}: {exc}"})
            batch_debug.append(debug_item)
            warnings.append(
                "Классификация SKU-важности недоступна; исходные productQuery сохранены для "
                f"позиций {sku_input[0]['position_index']}-{sku_input[-1]['position_index']}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        sku_by_index = {item.position_index: item for item in sku_response.results}
        for item in sku_input:
            index = item["position_index"]
            semantic = semantic_by_index[index]
            sku = sku_by_index.get(index)
            expected_ids = {entry["id"] for entry in normalized_by_index[index]}
            decision_ids = {decision.id for decision in sku.decisions} if sku else set()
            if sku is None or decision_ids != expected_ids:
                warnings.append(
                    f"LLM №2 вернула неполную классификацию позиции {index}; исходный productQuery сохранён."
                )
                continue
            result[index - 1] = build_search_query(
                positions[index - 1], semantic, sku, normalized_by_index[index]
            )
            enriched_count += 1
        warnings.extend(semantic_response.warnings)
        warnings.extend(sku_response.warnings)
        debug_item["sku"] = "completed"
        debug_item["skuResultCount"] = len(sku_response.results)
        batch_debug.append(debug_item)

    return result, list(dict.fromkeys(warnings)), {
        "applied": bool(work_indexes),
        "positionCount": len(positions),
        "positionsWithCharacteristics": len(work_indexes),
        "enrichedCount": enriched_count,
        "batchSize": max(1, batch_size),
        "batches": batch_debug,
    }
