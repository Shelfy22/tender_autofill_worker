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
_CABLE_PRODUCT_PATTERN = re.compile(r"\b(?:кабел|провод|шнур)", re.IGNORECASE)
_CABLE_DESIGNATION_PATTERN = re.compile(
    r"(?P<mark>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9()\-]{1,40})\s*"
    r"(?P<cores>\d{1,2})\s*[xх×*]\s*(?P<section>\d{1,4}(?:[.,]\d+)?)",
    re.IGNORECASE,
)
_CABLE_MARK_PATTERN = re.compile(
    r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9()\-]{2,40}",
    re.IGNORECASE,
)
_COMPACT_DESIGNATION_CANDIDATE = re.compile(
    r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9()._/-]{2,80}",
    re.IGNORECASE,
)
_MEASUREMENT_LIKE_DESIGNATION = re.compile(
    r"\d+(?:[.,]\d+)?(?:[xх×*]\d+(?:[.,]\d+)?)?[A-Za-zА-Яа-яЁё³²/%°]+$",
    re.IGNORECASE,
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
    parts = re.split(
        r"[\r\n]+|\s*\|\s*|\s*;{2,}\s*|\s*;\s*(?=[^;:]{1,80}:)",
        text,
    )
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


def _is_weak_standalone_token(value: Any) -> bool:
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", _clean(value)))


def _compact_designations(value: Any) -> list[str]:
    """Return model/article-like tokens while excluding measurements and dimensions."""
    result: list[str] = []
    for match in _COMPACT_DESIGNATION_CANDIDATE.finditer(_clean(value)):
        token = _compact_token(match.group(0)).strip(".,;:")
        if (
            not token
            or not re.search(r"[A-Za-zА-Яа-яЁё]", token)
            or not re.search(r"\d", token)
            or token[0].isdigit()
            or _MEASUREMENT_LIKE_DESIGNATION.fullmatch(token)
        ):
            continue
        has_structure = any(symbol in token for symbol in "-_().")
        if has_structure:
            result.append(token)
    return list(dict.fromkeys(result))


def _designation_identity_tokens(position: TenderPosition) -> list[str]:
    values = (
        position.model,
        position.article,
        *(characteristic.value for characteristic in position.characteristics),
    )
    tokens: list[str] = []
    for value in values:
        tokens.extend(_compact_designations(value))
    return list(dict.fromkeys(tokens))[:3]


def _is_cable_position(
    position: TenderPosition,
    semantic: ProductSemanticClassification | None = None,
) -> bool:
    text = " ".join(
        value
        for value in (
            _clean(position.product),
            _clean(position.productQuery),
            _clean(semantic.category) if semantic is not None else "",
        )
        if value
    )
    return bool(_CABLE_PRODUCT_PATTERN.search(text))


def _cable_identity_tokens(
    position: TenderPosition,
    semantic: ProductSemanticClassification | None = None,
) -> list[str]:
    if not _is_cable_position(position, semantic):
        return []

    values = [
        _clean(value)
        for value in (
            position.product,
            position.productQuery,
            *(characteristic.value for characteristic in position.characteristics),
        )
        if _clean(value)
    ]
    for value in values:
        match = _CABLE_DESIGNATION_PATTERN.search(value)
        if match:
            mark = _compact_token(match.group("mark")).rstrip(".,;:")
            return [f"{mark} {match.group('cores')}x{match.group('section')}"]

    mark = ""
    cores = ""
    section = ""
    for characteristic in position.characteristics:
        name = _identity(characteristic.name)
        value = _clean(characteristic.value)
        if not value:
            continue
        if not mark and re.search(r"\b(?:марка|маркиров|обозначен|тип)\b", name):
            candidate = _CABLE_MARK_PATTERN.search(value)
            if candidate:
                mark = _compact_token(candidate.group(0)).rstrip(".,;:")
        if not cores and re.search(r"\b(?:числ|колич).*жил\b", name):
            cores = _first_number_token(value)
        if not section and re.search(r"\b(?:сечен|площад.*попереч)\b", name):
            section = _first_number_token(value)

    tokens: list[str] = []
    if mark:
        tokens.append(mark)
    if cores and section:
        tokens.append(f"{cores}x{section}")
    return tokens


def _prepend_required_tokens(
    required: list[str],
    selected: list[str],
    *,
    base: str = "",
) -> list[str]:
    base_identity = _identity(base)
    values = [
        *(token for token in required if _identity(token) not in base_identity),
        *(token for token in selected if not _is_weak_standalone_token(token)),
    ]
    return list(dict.fromkeys(_compact_token(value) for value in values if _compact_token(value)))[:5]


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
        name_unit = _canonical_unit(_fallback_unit_hint(characteristic.name))
        unit_is_grounded_in_name = bool(name_unit and token_units == {name_unit})
        if not (
            unit_is_grounded_in_name
            or (token_units == {"p"} and "полюс" in role_name)
        ):
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
        or _is_binary_presence_characteristic(
            characteristic.name,
            characteristic.original_value,
        )
    ):
        return None

    designation_tokens = _compact_designations(characteristic.original_value)
    if designation_tokens:
        token = designation_tokens[0]
        if _search_token_is_grounded(characteristic, token):
            return {
                "id": characteristic.id,
                "name": characteristic.name,
                "semantic_role": "IDENTIFIER",
                "value_form": "EXACT",
                "search_token": token,
                "source_value": characteristic.original_value,
                "token_verified": True,
            }

    value_form = characteristic.value_form
    numeric = characteristic.numeric
    name_identity = _identity(characteristic.name)
    is_color_temperature_range = bool(
        numeric.min is not None
        and numeric.max is not None
        and re.search(r"цветов.*температур|диапазон.*(?:сто|cct)", name_identity)
    )
    if is_color_temperature_range:
        unit = _clean(numeric.unit) or _fallback_unit_hint(characteristic.name)
        token = _compact_token(
            f"{_format_number(numeric.min)}-{_format_number(numeric.max)}{unit}"
        )
    elif value_form == "ACCEPTABLE_RANGE" and numeric.min is not None and numeric.max is not None:
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


def _is_binary_presence_characteristic(name: Any, value: Any) -> bool:
    normalized_name = _identity(name)
    normalized_value = _identity(value)
    binary_values = {
        "да",
        "нет",
        "есть",
        "имеется",
        "предусмотрено",
        "не предусмотрено",
        "true",
        "false",
    }
    if normalized_value in binary_values:
        return True
    if re.match(r"^(?:наличие|отсутствие)\b", normalized_value):
        return True
    return bool(
        re.search(r"\b(?:наличие|поддержка|совместимость)\b", normalized_name)
        and normalized_value in binary_values
    )


def _fallback_unit_hint(name: Any) -> str:
    normalized = _clean(name)
    match = re.search(
        r"(?:,|\()\s*(Вт|кВт|В|кВ|А|мА|Гц|мм|см|м|м2|м²|м3|м³|"
        r"м3/ч|м³/ч|л|кг|г|К|°C|градус(?:а|ов)?)\s*\)?(?:\s|$)",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        return ""
    unit = match.group(1)
    if unit.casefold().startswith("градус"):
        return "°"
    return unit


def _fallback_characteristic_token(name: Any, value: Any) -> str:
    raw_name = _clean(name)
    raw_value = _clean(value).strip(" ;,.")
    if not raw_value or _is_binary_presence_characteristic(raw_name, raw_value):
        return ""
    if re.search(
        r"\b(?:гарант|срок|время\s+использования|ресурс|наработка)\b",
        _identity(raw_name),
    ):
        return ""

    unit_hint = _fallback_unit_hint(raw_name)
    range_match = re.search(
        r"(-?\d+(?:[.,]\d+)?)\s*[-–—…]\s*(-?\d+(?:[.,]\d+)?)",
        raw_value,
    )
    if range_match:
        trailing_unit_match = re.match(
            r"\s*([%°A-Za-zА-Яа-яЁё³²/]+)",
            raw_value[range_match.end() :],
        )
        trailing_unit = (
            trailing_unit_match.group(1)
            if trailing_unit_match
            and _canonical_unit(trailing_unit_match.group(1))
            in {
                "v", "kv", "a", "ma", "w", "kw", "hz", "mm", "mm2",
                "cm", "m", "m2", "m3", "m3/h", "l", "kg", "g", "rpm",
                "%", "°c", "к",
            }
            else ""
        )
        return _compact_token(
            f"{range_match.group(1)}-{range_match.group(2)}"
            f"{trailing_unit or unit_hint}"
        )

    number_match = re.search(r"-?\d+(?:[.,]\d+)?", raw_value)
    if number_match:
        value_unit_match = re.search(
            r"-?\d+(?:[.,]\d+)?\s*([%°A-Za-zА-Яа-яЁё³²/]+)",
            raw_value,
        )
        value_unit = value_unit_match.group(1) if value_unit_match else ""
        if value_unit and _canonical_unit(value_unit) not in {
            "v", "kv", "a", "ma", "w", "kw", "hz", "mm", "mm2",
            "cm", "m", "m2", "m3", "m3/h", "l", "kg", "g", "rpm",
            "%", "°c", "к",
        }:
            value_unit = ""
        unit = value_unit or unit_hint
        if not unit:
            return ""
        return _compact_token(f"{number_match.group(0)}{unit}")

    if (
        len(raw_value) <= 60
        and len(raw_value.split()) <= 5
        and not re.search(r"[.;:]", raw_value)
    ):
        return _compact_token(raw_value)
    return ""


def _fallback_characteristic_score(name: Any, token: str) -> int:
    normalized = _identity(name)
    rules = (
        (r"модел|серия|артикул|маркиров", 120),
        (r"мощност|номинал.*ток|напряж|частот", 110),
        (r"тип.*цветосмеш|цветосмеш", 105),
        (r"диапазон.*(?:сто|цвет.*температур)|цветов.*температур", 100),
        (r"сечен|числ.*жил|колич.*жил|диаметр|резьб|типоразмер", 95),
        (r"производительност|подач|напор|давлен|скорост", 90),
        (r"источник.*свет|тип.*луч|тип\b|вид\b", 80),
        (r"интерфейс|сигнал|протокол|вход|выход", 70),
        (r"материал|цвет", 55),
        (r"колич", 40),
    )
    for pattern, score in rules:
        if re.search(pattern, normalized):
            return score
    return 60 if any(character.isdigit() for character in token) else 45


def _search_query_variants(
    base: str,
    selected: list[str],
    *,
    exact_identity: str = "",
) -> list[str]:
    query = _clean(" ".join((base, *selected))) or base
    candidates = [query, exact_identity]
    if selected:
        if len(selected) > 2:
            candidates.append(_clean(" ".join((base, *selected[:2]))))
        if len(selected) > 1:
            candidates.append(_clean(" ".join((base, selected[0]))))
    else:
        candidates.append(base)
    return list(dict.fromkeys(value for value in candidates if value))[:4]


def _fallback_search_query(position: TenderPosition) -> TenderPosition:
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for ordinal, candidate in enumerate(_characteristic_candidates(position)):
        token = _fallback_characteristic_token(
            candidate.get("name"),
            candidate.get("value"),
        )
        token_key = _identity(token)
        if not token or not token_key or token_key in seen:
            continue
        seen.add(token_key)
        scored.append(
            (
                _fallback_characteristic_score(candidate.get("name"), token),
                -ordinal,
                token,
            )
        )
    selected = [
        token
        for _, _, token in sorted(scored, reverse=True)[:3]
    ]
    selected = _prepend_required_tokens(
        [*_designation_identity_tokens(position), *_cable_identity_tokens(position)],
        selected,
        base=_clean(position.product),
    )
    base = _clean(position.product)
    query = _clean(" ".join((base, *selected))) or base
    variants = _search_query_variants(base, selected)
    return position.model_copy(
        update={
            "productQuery": query,
            "searchCharacteristics": selected,
            "searchCategoryCode": normalize_product_category("", position.product),
            "searchQueries": variants,
        }
    )


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
    identity_ids = list(dict.fromkeys(sku.identity_bundle))
    selected_ids = list(dict.fromkeys([*identity_ids, *sku.selected_for_search]))
    for characteristic_id in selected_ids:
        item = by_id.get(characteristic_id)
        decision = decision_by_id.get(characteristic_id)
        is_identity = characteristic_id in identity_ids
        if (
            item is None
            or decision is None
            or (not is_identity and decision.usage not in _SEARCH_USAGES)
        ):
            continue
        token = _compact_token(item["search_token"])
        token_key = _identity(token)
        is_cable_cores = any(
            characteristic.normalizedName == "cable.cores"
            and _identity(characteristic.value) == _identity(item.get("source_value"))
            for characteristic in position.characteristics
        )
        if (
            not token
            or (_is_weak_standalone_token(token) and not is_cable_cores)
            or token_key in seen_tokens
        ):
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
    selected = _prepend_required_tokens(
        [
            *_designation_identity_tokens(position),
            *_cable_identity_tokens(position, semantic),
        ],
        _compose_search_tokens(selected_items),
        base=base,
    )
    query = _clean(" ".join([base, *selected])) or _clean(
        position.productQuery or position.product
    )
    exact_identity = _clean(
        " ".join(filter(None, (position.brand, position.model, position.article)))
    )
    query_variants = _search_query_variants(
        base,
        selected,
        exact_identity=exact_identity,
    )
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
                "Семантическая классификация характеристик недоступна; применён компактный "
                f"детерминированный fallback для позиций {indexes[0]}-{indexes[-1]}: "
                f"{type(exc).__name__}: {exc}"
            )
            for index in indexes:
                result[index - 1] = _fallback_search_query(positions[index - 1])
                enriched_count += 1
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
                    f"LLM №1 вернула неполную классификацию позиции {index}; "
                    "применён компактный детерминированный fallback."
                )
                result[index - 1] = _fallback_search_query(positions[index - 1])
                enriched_count += 1
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
                "Классификация SKU-важности недоступна; применён компактный "
                "детерминированный fallback для "
                f"позиций {sku_input[0]['position_index']}-{sku_input[-1]['position_index']}: "
                f"{type(exc).__name__}: {exc}"
            )
            for item in sku_input:
                index = item["position_index"]
                result[index - 1] = _fallback_search_query(positions[index - 1])
                enriched_count += 1
            continue

        sku_by_index = {item.position_index: item for item in sku_response.results}
        for item in sku_input:
            index = item["position_index"]
            semantic = semantic_by_index[index]
            sku = sku_by_index.get(index)
            expected_ids = {entry["id"] for entry in normalized_by_index[index]}
            decision_ids = {decision.id for decision in sku.decisions} if sku else set()
            identity_ids = set(sku.identity_bundle) if sku else set()
            if (
                sku is None
                or decision_ids != expected_ids
                or not identity_ids.issubset(expected_ids)
            ):
                warnings.append(
                    f"LLM №2 вернула неполную классификацию позиции {index}; "
                    "применён компактный детерминированный fallback."
                )
                result[index - 1] = _fallback_search_query(positions[index - 1])
                enriched_count += 1
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
