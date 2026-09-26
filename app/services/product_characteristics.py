from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from app.models import (
    CharacteristicConflict,
    CharacteristicConflictValue,
    DocumentCharacteristicSet,
    ProductCharacteristic,
    TenderPosition,
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _identity(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def canonical_characteristic_name(name: Any) -> str:
    value = _identity(name)
    if not value:
        return ""
    rules = (
        (r"напряж.*питан|питан.*напряж|supply voltage", "electrical.supply_voltage"),
        (r"выход.*напряж|напряж.*выход", "electrical.output_voltage"),
        (r"номинал.*напряж|напряж.*номинал", "electrical.nominal_voltage"),
        (r"номинал.*ток|ток.*номинал", "electrical.nominal_current"),
        (r"частот", "electrical.frequency"),
        (r"числ.*полюс|колич.*полюс", "electrical.poles"),
        (r"мощност", "performance.power"),
        (r"производительност|подач", "performance.flow"),
        (r"напор", "performance.head"),
        (r"давлен", "performance.pressure"),
        (r"числ.*жил|колич.*жил", "cable.cores"),
        (r"сечен", "cable.cross_section"),
        (r"диаметр|\bdn\b|условн.*проход", "dimension.diameter"),
        (r"толщин", "dimension.thickness"),
        (r"длин", "dimension.length"),
        (r"ширин", "dimension.width"),
        (r"высот", "dimension.height"),
        (r"габарит|размер", "dimension.size"),
        (r"резьб", "connection.thread"),
        (r"степен.*защит|\bip\b", "environment.ip_rating"),
        (r"температур", "environment.temperature"),
        (r"материал", "material.type"),
        (r"цвет", "appearance.color"),
        (r"вязкост", "fluid.viscosity"),
        (r"объем.*упаков|фасов|масса.*упаков", "packaging.size"),
        (r"бренд|производител|торгов.*марк", "identity.brand"),
        (r"модел|серия|типоразмер", "identity.model"),
        (r"артикул|каталож.*номер", "identity.article"),
    )
    for pattern, canonical in rules:
        if re.search(pattern, value):
            return canonical
    return "other." + value.replace(" ", "_")[:80]


_CATEGORY_RULES = (
    (
        "electrical_lighting",
        r"кабел|провод|светиль|ламп|розет|выключател|автомат.*выключ|"
        r"щит(?:ок|овой|овое|овая|ы|а|у|е|ом)?|электромонтаж",
    ),
    ("power_supply", r"аккумулятор|источник.*питан|ибп|ups|генератор|трансформатор|преобразовател"),
    ("water_supply", r"насос|труб|шланг|арматур|кран|сантех|водоснаб|водоотвед"),
    ("climate", r"кондиционер|вентил|радиатор|котел|обогрев|отоплен|климат"),
    ("security", r"камер|видеонаблюд|пожар|охран|скуд|домофон|шлагбаум"),
    ("automation_instrumentation", r"датчик|кип|реле|контроллер|частотн.*привод|электропривод|автоматизац"),
    ("tools", r"инструмент|дрель|шуруповерт|оснаст|свар|паяль|компрессор"),
    ("fasteners", r"болт|гайк|шайб|винт|анкер|дюбел|заклеп|саморез|крепеж"),
    ("telecom", r"телеком|скс|сетев.*оборуд|оптическ.*кабел|ip телеф|шкаф.*телеком"),
    ("it_multimedia", r"компьютер|ноутбук|сервер|монитор|принтер|мфу|перифери|мультимедиа"),
    ("software", r"программ.*обеспеч|лицензи"),
    ("ppe_workwear", r"спецодеж|сиз|каск|перчат|респиратор|спецобув"),
    ("lubricants_fluids", r"масло|смазк|жидкост|антифриз|автохим"),
    ("construction_materials", r"цемент|бетон|кирпич|герметик|изоляц|строительн.*материал"),
    ("industrial_components", r"подшипник|уплотн|промышлен.*трансмис|гидравлическ"),
    ("warehouse_equipment", r"стеллаж|погрузчик|склад|грузоподъем|тара"),
    ("cleaning", r"клининг|уборк|моющ|диспенсер|мусор"),
    ("furniture", r"мебел|стол|стул|шкаф|кресл"),
    ("appliances", r"чайник|холодильник|бытов.*техник|кухон"),
    ("office_supplies", r"канцтовар|бумаг|папк|письменн.*принадлеж"),
)


def normalize_product_category(category: Any, product: Any = "") -> str:
    value = _identity(f"{category} {product}")
    for code, pattern in _CATEGORY_RULES:
        if re.search(pattern, value):
            return code
    return "other"


def _source_position_number(position: TenderPosition) -> str:
    if _clean(position.positionNumber):
        return _clean(position.positionNumber)
    reference = position.sourceReference
    if reference and _clean(reference.positionNumber):
        return _clean(reference.positionNumber)
    excluded = {
        reference.productColumn if reference else "",
        reference.quantityColumn if reference else "",
        reference.unitColumn if reference else "",
    }
    for column, value in sorted(position.sourceCells.items()):
        if column in excluded:
            continue
        match = re.fullmatch(r"\s*(\d{1,6})\s*[.)]?\s*", value)
        if match:
            return match.group(1)
    return ""


def ensure_position_identity(
    position: TenderPosition,
    *,
    source_file: str = "",
    ordinal: int = 0,
) -> TenderPosition:
    position_number = _source_position_number(position)
    reference = position.sourceReference
    file_name = _clean(reference.fileName if reference else "") or _clean(source_file)
    lot_number = _clean(position.lotNumber) or _clean(reference.lotNumber if reference else "")
    if position.positionKey:
        position_key = position.positionKey
    else:
        source_coordinates = {
            "candidateId": _clean(position.candidateId),
            "file": Path(file_name).name.casefold(),
            "sheet": _clean(reference.sheet if reference else "").casefold(),
            "table": _clean(reference.table if reference else "").casefold(),
            "page": reference.page if reference else None,
            "row": reference.row if reference else None,
            "lot": lot_number,
            "position": position_number,
            "product": _identity(position.product),
            "quantity": position.quantity,
            "unit": _identity(position.unit),
            "ordinal": ordinal if not reference and not position.candidateId else 0,
        }
        digest = hashlib.sha256(
            json.dumps(source_coordinates, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        position_key = f"pos_{digest}"
    characteristics = [
        characteristic.model_copy(
            update={
                "normalizedName": characteristic.normalizedName
                or canonical_characteristic_name(characteristic.name),
                "targetPositionKey": characteristic.targetPositionKey or position_key,
                "associationConfidence": (
                    characteristic.associationConfidence
                    or (0.98 if characteristic.associationMethod in {"same_row", "continuation_row"} else 0.82)
                ),
                "associationMethod": (
                    characteristic.associationMethod
                    if characteristic.associationMethod != "unresolved"
                    else "llm_consolidation"
                ),
                "associationStatus": (
                    characteristic.associationStatus
                    if characteristic.associationStatus != "unverified"
                    else "confirmed"
                ),
            }
        )
        for characteristic in position.characteristics
    ]
    return position.model_copy(
        update={
            "positionKey": position_key,
            "lotNumber": lot_number,
            "positionNumber": position_number,
            "characteristics": characteristics,
        }
    )


def _association_score(
    position: TenderPosition,
    characteristic_set: DocumentCharacteristicSet,
) -> tuple[float, str]:
    if characteristic_set.targetPositionKey:
        return (
            (1.0, "position_key")
            if characteristic_set.targetPositionKey == position.positionKey
            else (0.0, "unresolved")
        )
    if characteristic_set.article and position.article:
        if _identity(characteristic_set.article) == _identity(position.article):
            return 0.99, "article"
    if characteristic_set.model and position.model:
        if _identity(characteristic_set.model) == _identity(position.model):
            return 0.97, "model"
    if (
        characteristic_set.lotNumber
        and characteristic_set.positionNumber
        and _identity(characteristic_set.lotNumber) == _identity(position.lotNumber)
        and _identity(characteristic_set.positionNumber) == _identity(position.positionNumber)
    ):
        return 0.96, "lot_position"
    if (
        characteristic_set.positionNumber
        and _identity(characteristic_set.positionNumber) == _identity(position.positionNumber)
    ):
        return 0.86, "position_number"
    hint = _identity(characteristic_set.productHint)
    product = _identity(position.product)
    if hint and hint == product:
        return 0.82, "exact_product_name"
    return 0.0, "unresolved"


def associate_characteristic_sets(
    positions: list[TenderPosition],
    characteristic_sets: list[DocumentCharacteristicSet],
) -> tuple[list[TenderPosition], list[DocumentCharacteristicSet], list[str], dict[str, Any]]:
    prepared = [
        ensure_position_identity(position, ordinal=index)
        for index, position in enumerate(positions, start=1)
    ]
    unresolved: list[DocumentCharacteristicSet] = []
    warnings: list[str] = []
    attached = 0
    ambiguous = 0
    for characteristic_set in characteristic_sets:
        scored = [
            (score, method, index)
            for index, position in enumerate(prepared)
            for score, method in [_association_score(position, characteristic_set)]
            if score > 0
        ]
        scored.sort(reverse=True)
        if not scored:
            unresolved.append(characteristic_set)
            continue
        best_score, method, best_index = scored[0]
        tied = [item for item in scored if abs(item[0] - best_score) < 1e-9]
        if best_score < 0.80 or len(tied) != 1:
            ambiguous += 1
            unresolved.append(
                characteristic_set.model_copy(
                    update={
                        "associationConfidence": best_score,
                        "associationMethod": "unresolved",
                    }
                )
            )
            continue
        target = prepared[best_index]
        existing = list(target.characteristics)
        seen = {
            (item.normalizedName or canonical_characteristic_name(item.name), _identity(item.value))
            for item in existing
        }
        for characteristic in characteristic_set.characteristics:
            normalized_name = characteristic.normalizedName or canonical_characteristic_name(
                characteristic.name
            )
            key = (normalized_name, _identity(characteristic.value))
            if not key[1] or key in seen:
                continue
            existing.append(
                characteristic.model_copy(
                    update={
                        "normalizedName": normalized_name,
                        "targetPositionKey": target.positionKey,
                        "associationConfidence": best_score,
                        "associationMethod": method,
                        "associationStatus": "confirmed",
                    }
                )
            )
            seen.add(key)
            attached += 1
        prepared[best_index] = target.model_copy(update={"characteristics": existing})
    if unresolved:
        warnings.append(
            f"{len(unresolved)} наборов характеристик не привязаны автоматически: отсутствует уникальный надёжный ключ позиции."
        )
    return prepared, unresolved, warnings, {
        "inputSetCount": len(characteristic_sets),
        "attachedCharacteristicCount": attached,
        "unresolvedSetCount": len(unresolved),
        "ambiguousSetCount": ambiguous,
    }


def _normalized_characteristic_value(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold().replace("ё", "е")
    text = text.replace(" ", "").replace(".", ",")
    return text


def finalize_position_characteristics(
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    result: list[TenderPosition] = []
    warnings: list[str] = []
    conflict_count = 0
    low_confidence_count = 0
    for index, raw_position in enumerate(positions, start=1):
        position = ensure_position_identity(raw_position, ordinal=index)
        by_name: dict[str, list[ProductCharacteristic]] = {}
        for characteristic in position.characteristics:
            normalized_name = characteristic.normalizedName or canonical_characteristic_name(
                characteristic.name
            )
            updated = characteristic.model_copy(update={"normalizedName": normalized_name})
            by_name.setdefault(normalized_name, []).append(updated)
        conflicts: list[CharacteristicConflict] = []
        updated_characteristics: list[ProductCharacteristic] = []
        for normalized_name, items in by_name.items():
            values: dict[str, list[ProductCharacteristic]] = {}
            source_files: set[str] = set()
            for item in items:
                values.setdefault(_normalized_characteristic_value(item.value), []).append(item)
                source_files.add(_identity(item.sourceReference.get("fileName")))
            is_conflict = len(values) > 1 and len({item for item in source_files if item}) > 1
            if is_conflict:
                conflict_count += 1
                conflicts.append(
                    CharacteristicConflict(
                        normalizedName=normalized_name,
                        values=[
                            CharacteristicConflictValue(
                                value=group[0].value,
                                sourceReference=group[0].sourceReference,
                                confidence=group[0].confidence,
                            )
                            for group in values.values()
                        ],
                    )
                )
            for item in items:
                if item.associationConfidence < 0.80:
                    low_confidence_count += 1
                updated_characteristics.append(
                    item.model_copy(
                        update={
                            "associationStatus": "conflicting"
                            if is_conflict
                            else item.associationStatus,
                        }
                    )
                )
        if conflicts:
            warnings.append(
                f"Позиция {position.positionKey}: найдены противоречащие характеристики из разных документов; они исключены из поискового запроса."
            )
        result.append(
            position.model_copy(
                update={
                    "characteristics": updated_characteristics,
                    "characteristicConflicts": conflicts,
                }
            )
        )
    return result, warnings, {
        "positionCount": len(result),
        "conflictCount": conflict_count,
        "lowConfidenceCharacteristicCount": low_confidence_count,
    }
