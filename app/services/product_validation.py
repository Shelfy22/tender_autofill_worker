from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from app.models import (
    ProductCandidateAssignment,
    ProductCandidateAuditResponse,
    ProductHierarchyAssignment,
    ProductHierarchyResponse,
    SpreadsheetCandidateReviewResponse,
    TenderPosition,
)
from app.services.product_hierarchy import apply_product_hierarchy

if TYPE_CHECKING:
    from app.services.llm import LlmClient


AUDIT_ACTION_CONFIDENCE = 0.85
AUDIT_REVIEW_CONFIDENCE = 0.65
_NON_PRODUCT_ROLES = {"characteristic", "address", "service", "header"}
_STANDALONE_SERVICE_PATTERN = re.compile(
    r"^\s*(?:аналоги?\s+рассматрива(?:ются|ется)|"
    r"эквиваленты?\s+(?:допуска(?:ются|ется)|разрешены?)|"
    r"аналог\s+допуска(?:ется|ются)|без\s+аналогов)\s*[.!;:]?\s*$",
    re.IGNORECASE,
)
_STANDALONE_HEADER_PATTERN = re.compile(
    r"^\s*(?:наименование(?:\s+(?:товара|продукции|изделия|показателя))?|"
    r"технические\s+характеристики|характеристики|описание|требования|"
    r"значение(?:\s+показателя)?|наименование\s+показателя|параметр|показатель|"
    r"комплектность|компоненты|состав\s+комплекта|преимущество)\s*[.!;:]?\s*$",
    re.IGNORECASE,
)
_STANDALONE_CHARACTERISTIC_PATTERN = re.compile(
    r"^\s*(?:(?:количество|число)\s+(?:полюсов|фаз|контактов|входов|выходов|"
    r"жил|модулей|секций|каналов)|(?:тип|вид|класс|категория|степень|"
    r"напряжение|ток|мощность|частота|габариты|материал|цвет|исполнение|"
    r"условное\s+обозначение)(?:\s+\S+){0,5})\s*[.!;:]?\s*$",
    re.IGNORECASE,
)
_NATIONAL_REGIME_CELL_PATTERN = re.compile(
    r"^\s*(?=.*\b(?:преимущество|ограничение|запрет)\b)"
    r"(?=.*\b(?:установлен[а-яё]*|не\s+установлен[а-яё]*|предоставля[а-яё]*|"
    r"не\s+предоставля[а-яё]*)\b).{3,500}$",
    re.IGNORECASE,
)
_MODEL_TOKEN_PATTERN = re.compile(
    r"\b(?=[a-zа-яё0-9./-]*[a-zа-яё])(?=[a-zа-яё0-9./-]*\d)"
    r"[a-zа-яё0-9]+(?:[-./][a-zа-яё0-9]+)+\b",
    re.IGNORECASE,
)


_COMPACT_MODEL_TOKEN_PATTERN = re.compile(
    r"(?<![a-zа-яё0-9])[a-zа-яё0-9]{5,}(?![a-zа-яё0-9])",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _missing(value: Any) -> bool:
    return value is None or value == ""


def _deterministic_non_product_role(value: Any) -> str | None:
    text = _clean(value)
    if _STANDALONE_SERVICE_PATTERN.fullmatch(text):
        return "service"
    if _STANDALONE_HEADER_PATTERN.fullmatch(text):
        return "header"
    if _STANDALONE_CHARACTERISTIC_PATTERN.fullmatch(text):
        return "characteristic"
    if _NATIONAL_REGIME_CELL_PATTERN.fullmatch(text):
        return "service"
    return None


def _identity(value: Any) -> str:
    text = _clean(value).casefold().replace("ё", "е")
    text = re.sub(
        r"\s*[([]?\s*(?:или\s+)?(?:аналог|эквивалент)\s*[)\]]?\s*$",
        "",
        text,
    )
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def _model_tokens(position: TenderPosition) -> set[str]:
    text = " ".join((position.product, position.productQuery or "", position.article))
    tokens = {
        match.group(0).casefold()
        for match in _MODEL_TOKEN_PATTERN.finditer(text)
    }
    tokens.update(
        token
        for match in _COMPACT_MODEL_TOKEN_PATTERN.finditer(text)
        if (token := match.group(0).casefold())
        and sum(character.isalpha() for character in token) >= 2
        and sum(character.isdigit() for character in token) >= 2
    )
    return tokens


def _quantity_compatible(left: TenderPosition, right: TenderPosition) -> bool:
    return (
        left.quantity is None
        or right.quantity is None
        or left.quantity == right.quantity
    )


def _unit_compatible(left: TenderPosition, right: TenderPosition) -> bool:
    return not left.unit or not right.unit or left.unit.casefold() == right.unit.casefold()


def _structured_source_issue(position: TenderPosition) -> str | None:
    evidence = str(position.evidence or "")
    row_match = re.search(r"(?:^|\n)Строка\s+(\d+)\s*:\s*", evidence, re.IGNORECASE)
    if row_match is None:
        return None
    reference = position.sourceReference
    if reference is None:
        return "Для позиции из строки Excel отсутствуют координаты sourceReference"
    if reference.row is None or reference.row != int(row_match.group(1)):
        return "Номер строки sourceReference не совпадает с evidence"
    column = reference.productColumn.strip().upper()
    if not column:
        return "В sourceReference не указана колонка наименования товара"
    if re.search(rf"(?:^|\|)\s*{re.escape(column)}\s*:\s*", evidence) is None:
        return "Колонка товара sourceReference не подтверждена в evidence"
    return None


def _duplicate_supported(left: TenderPosition, right: TenderPosition) -> bool:
    if not _quantity_compatible(left, right) or not _unit_compatible(left, right):
        return False
    left_name = _identity(left.productQuery or left.product)
    right_name = _identity(right.productQuery or right.product)
    if not left_name or not right_name:
        return False
    if left_name == right_name:
        return True
    left_article = _identity(left.article)
    right_article = _identity(right.article)
    if left_article and left_article == right_article:
        return True
    shared_models = _model_tokens(left) & _model_tokens(right)
    if shared_models:
        return True
    shorter, longer = sorted((left_name, right_name), key=len)
    if len(shorter) >= 18 and shorter in longer:
        return True
    return SequenceMatcher(None, left_name, right_name).ratio() >= 0.88


def _merge_duplicate(target: TenderPosition, duplicate: TenderPosition) -> TenderPosition:
    updates: dict[str, Any] = {}
    if target.quantity is None and duplicate.quantity is not None:
        updates["quantity"] = duplicate.quantity
    if not target.unit and duplicate.unit:
        updates["unit"] = duplicate.unit
    for field in (
        "brand",
        "article",
        "analogsAllowed",
        "documentUnitPriceRub",
        "documentLineTotalRub",
        "documentCurrency",
        "documentPriceEvidence",
        "documentPriceSource",
        "sourceReference",
        "sourceCells",
    ):
        if _missing(getattr(target, field)) and not _missing(getattr(duplicate, field)):
            updates[field] = getattr(duplicate, field)
    for field, limit in (("requirements", 6000), ("evidence", 1200)):
        current = _clean(getattr(target, field))
        candidate = _clean(getattr(duplicate, field))
        if candidate and candidate not in current:
            updates[field] = _clean(" | ".join(filter(None, (current, candidate))))[:limit]
    return target.model_copy(update=updates) if updates else target


def _base_debug(position_count: int) -> dict[str, Any]:
    return {
        "reviewRequested": position_count > 0,
        "applied": False,
        "requiresManualReview": False,
        "originalPositionCount": position_count,
        "validatedPositionCount": position_count,
        "rejectedPositionCount": 0,
        "duplicateCount": 0,
        "componentCount": 0,
        "rejectedPositions": [],
        "duplicates": [],
        "unresolved": [],
        "assignments": [],
        "hierarchy": {},
    }


def apply_product_candidate_audit(
    positions: list[TenderPosition],
    response: ProductCandidateAuditResponse,
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    debug = _base_debug(len(positions))
    warnings = list(response.warnings)
    if not positions:
        return [], warnings, debug

    working = list(positions)
    assignments: dict[int, ProductCandidateAssignment] = {}
    duplicate_assignment_indexes: set[int] = set()
    for assignment in response.assignments:
        if not 1 <= assignment.positionIndex <= len(positions):
            continue
        if assignment.positionIndex in assignments:
            duplicate_assignment_indexes.add(assignment.positionIndex)
            continue
        assignments[assignment.positionIndex] = assignment
        debug["assignments"].append(assignment.model_dump())

    unresolved: list[dict[str, Any]] = []

    def require_review(index: int, reason: str) -> None:
        item = {
            "positionIndex": index,
            "product": positions[index - 1].product,
            "reason": reason,
        }
        if item not in unresolved:
            unresolved.append(item)

    for index in range(1, len(positions) + 1):
        if index not in assignments:
            require_review(index, "LLM-аудит не вернул назначение для позиции")
        elif index in duplicate_assignment_indexes:
            require_review(index, "LLM-аудит вернул несколько назначений для позиции")

    hierarchy_response = ProductHierarchyResponse(
        assignments=[
            ProductHierarchyAssignment(
                positionIndex=index,
                role=(
                    "component"
                    if assignment.role == "component"
                    else "purchase_item"
                    if assignment.role == "purchase_item"
                    else "ambiguous"
                ),
                parentPositionIndex=assignment.parentPositionIndex,
                confidence=assignment.confidence,
                rationale=assignment.rationale,
            )
            for index, assignment in assignments.items()
        ]
    )
    hierarchy_positions, hierarchy_warnings, hierarchy_debug = apply_product_hierarchy(
        working,
        hierarchy_response,
    )
    warnings.extend(hierarchy_warnings)
    debug["hierarchy"] = hierarchy_debug
    component_indexes = {
        int(item["positionIndex"])
        for item in hierarchy_debug.get("components", [])
        if isinstance(item, dict) and item.get("positionIndex") is not None
    }
    remaining_original_indexes = [
        index for index in range(1, len(working) + 1) if index not in component_indexes
    ]
    for index, updated_position in zip(remaining_original_indexes, hierarchy_positions):
        working[index - 1] = updated_position

    removed_indexes = set(component_indexes)
    debug["componentCount"] = len(component_indexes)
    for index, assignment in assignments.items():
        if assignment.role != "component":
            continue
        if index not in component_indexes:
            require_review(
                index,
                "Компонент не исключён: недостаточно подтверждён контекст комплектности",
            )

    for index, position in enumerate(working, start=1):
        if index in removed_indexes:
            continue
        assignment = assignments.get(index)
        if assignment is None:
            continue
        if assignment.role == "ambiguous":
            require_review(index, assignment.rationale or "Роль позиции неоднозначна")
            continue
        if assignment.role == "purchase_item":
            if assignment.confidence < AUDIT_REVIEW_CONFIDENCE:
                require_review(index, "Низкая уверенность, что строка является товаром")
            source_issue = _structured_source_issue(position)
            if source_issue:
                require_review(index, source_issue)
            continue
        if assignment.role in _NON_PRODUCT_ROLES:
            if assignment.confidence >= AUDIT_ACTION_CONFIDENCE:
                removed_indexes.add(index)
                debug["rejectedPositions"].append(
                    {
                        "positionIndex": index,
                        "product": position.product,
                        "role": assignment.role,
                        "confidence": assignment.confidence,
                        "rationale": assignment.rationale,
                        "sourceReference": (
                            position.sourceReference.model_dump()
                            if position.sourceReference is not None
                            else None
                        ),
                    }
                )
                warnings.append(
                    "Исключена строка, не являющаяся товаром: "
                    f"{position.product[:200]} ({assignment.role})."
                )
            else:
                require_review(
                    index,
                    f"Недостаточная уверенность для исключения роли {assignment.role}",
                )
            continue
        if assignment.role == "duplicate":
            target_index = assignment.duplicateOf
            valid_target = (
                target_index is not None
                and 1 <= target_index < index
                and target_index not in removed_indexes
            )
            if (
                assignment.confidence >= AUDIT_ACTION_CONFIDENCE
                and valid_target
                and _duplicate_supported(working[target_index - 1], position)
            ):
                working[target_index - 1] = _merge_duplicate(
                    working[target_index - 1],
                    position,
                )
                removed_indexes.add(index)
                debug["duplicates"].append(
                    {
                        "positionIndex": index,
                        "duplicateOf": target_index,
                        "product": position.product,
                        "confidence": assignment.confidence,
                        "rationale": assignment.rationale,
                    }
                )
                warnings.append(
                    "Объединён смысловой дубль товарной позиции: "
                    f"{position.product[:200]} -> позиция {target_index}."
                )
            else:
                reason = "Смысловой дубль не подтверждён детерминированным сравнением"
                if valid_target and not _quantity_compatible(working[target_index - 1], position):
                    reason = "Похожие позиции имеют конфликтующие количества"
                require_review(index, reason)

    validated = [
        position
        for index, position in enumerate(working, start=1)
        if index not in removed_indexes
    ]
    if not validated:
        require_review(1, "Аудит исключил все позиции; требуется проверить извлечение товаров")
        warnings.append(
            "Аудит товарных кандидатов исключил все позиции; ложные строки не переданы "
            "в поиск по каталогу и расчёт покрытия."
        )

    debug.update(
        {
            "applied": bool(removed_indexes),
            "requiresManualReview": bool(unresolved),
            "validatedPositionCount": len(validated),
            "rejectedPositionCount": len(debug["rejectedPositions"]),
            "duplicateCount": len(debug["duplicates"]),
            "unresolved": unresolved,
        }
    )
    if unresolved:
        warnings.append(
            "Аудит товарных позиций оставил неразрешённые случаи; "
            "они сохранены в диагностике, расчёт покрытия продолжен."
        )
    return validated, list(dict.fromkeys(warnings)), debug


def _spreadsheet_candidate_payload(position: TenderPosition) -> dict[str, Any]:
    return {
        "candidateId": position.candidateId,
        "product": position.product,
        "productQuery": position.productQuery,
        "quantity": position.quantity,
        "unit": position.unit,
        "sourceReference": (
            position.sourceReference.model_dump()
            if position.sourceReference is not None
            else None
        ),
        "sourceCells": position.sourceCells,
        "evidence": position.evidence[:300],
    }


def _spreadsheet_review_batches(
    settings: Any,
    positions: list[TenderPosition],
) -> list[list[TenderPosition]]:
    max_rows = max(1, int(getattr(settings, "spreadsheet_candidate_review_max_rows", 40)))
    max_chars = max(1_000, int(getattr(settings, "spreadsheet_candidate_review_max_chars", 24_000)))
    batches: list[list[TenderPosition]] = []
    current: list[TenderPosition] = []
    current_chars = 0
    for position in positions:
        candidate_chars = len(str(_spreadsheet_candidate_payload(position)))
        if current and (len(current) >= max_rows or current_chars + candidate_chars > max_chars):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(position)
        current_chars += candidate_chars
    if current:
        batches.append(current)
    return batches


def apply_spreadsheet_candidate_review(
    positions: list[TenderPosition],
    response: SpreadsheetCandidateReviewResponse,
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    warnings = list(response.warnings)
    debug: dict[str, Any] = {
        "reviewRequested": bool(positions),
        "applied": False,
        "originalPositionCount": len(positions),
        "validatedPositionCount": len(positions),
        "removedCount": 0,
        "correctedCount": 0,
        "newCount": 0,
        "unresolved": [],
        "decisions": [],
    }
    by_id = {position.candidateId: index for index, position in enumerate(positions) if position.candidateId}
    decisions = {}
    for decision in response.decisions:
        if not decision.candidateId:
            continue
        decisions[decision.candidateId] = decision
        debug["decisions"].append(decision.model_dump())

    result: list[TenderPosition] = []
    removed_ids: set[str] = set()
    for position in positions:
        decision = decisions.get(position.candidateId)
        if decision is None:
            result.append(position)
            debug["unresolved"].append(
                {
                    "candidateId": position.candidateId,
                    "product": position.product,
                    "reason": "LLM review did not return a decision for candidateId",
                }
            )
            continue
        if decision.decision == "REMOVE":
            target_ok = not decision.duplicateOfCandidateId or decision.duplicateOfCandidateId in by_id
            if decision.confidence >= AUDIT_ACTION_CONFIDENCE and target_ok:
                removed_ids.add(position.candidateId)
                warnings.append(
                    "Excel candidate исключён до Qdrant: "
                    f"{position.product[:200]} ({decision.reason[:200]})."
                )
                continue
            result.append(position)
            debug["unresolved"].append(
                {
                    "candidateId": position.candidateId,
                    "product": position.product,
                    "reason": decision.reason or "Недостаточная уверенность для REMOVE",
                }
            )
            continue
        if decision.decision == "CORRECT" and decision.normalizedProduct and decision.confidence >= AUDIT_REVIEW_CONFIDENCE:
            corrected_name = _clean(decision.normalizedProduct)
            if corrected_name and corrected_name != position.product:
                result.append(
                    position.model_copy(
                        update={
                            "product": corrected_name,
                            "productQuery": corrected_name,
                            "evidence": _clean(
                                " | ".join(
                                    filter(
                                        None,
                                        (
                                            position.evidence,
                                            f"Excel candidate review corrected name: {decision.reason[:300]}",
                                        ),
                                    )
                                )
                            )[:1200],
                        }
                    )
                )
                debug["correctedCount"] += 1
                continue
        result.append(position)

    existing_names = {_identity(position.productQuery or position.product) for position in result}
    for decision in response.decisions:
        if decision.decision != "NEW" or not decision.normalizedProduct:
            continue
        normalized = _clean(decision.normalizedProduct)
        if decision.confidence < AUDIT_ACTION_CONFIDENCE or not normalized:
            debug["unresolved"].append(
                {
                    "candidateId": decision.candidateId,
                    "product": normalized,
                    "reason": decision.reason or "Недостаточная уверенность для NEW",
                }
            )
            continue
        key = _identity(normalized)
        if key and key in existing_names:
            continue
        existing_names.add(key)
        result.append(
            TenderPosition(
                candidateId=f"llm:new:{decision.candidateId}",
                product=normalized,
                productQuery=normalized,
                evidence=decision.reason[:500],
                source="llm_excel_candidate_review",
            )
        )
        debug["newCount"] += 1
        warnings.append(f"LLM обнаружила пропущенную Excel-позицию: {normalized[:200]}.")

    debug["removedCount"] = len(removed_ids)
    debug["validatedPositionCount"] = len(result)
    debug["applied"] = bool(removed_ids or debug["correctedCount"] or debug["newCount"])
    return result, list(dict.fromkeys(warnings)), debug


def review_spreadsheet_candidate_positions(
    llm: LlmClient,
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    spreadsheet_positions = [position for position in positions if position.candidateId.startswith("xlsx:")]
    if not spreadsheet_positions:
        return positions, [], {
            "reviewRequested": False,
            "originalPositionCount": len(positions),
            "validatedPositionCount": len(positions),
        }

    current_by_id = {position.candidateId: position for position in positions if position.candidateId}
    all_warnings: list[str] = []
    combined_debug: dict[str, Any] = {
        "reviewRequested": True,
        "batchCount": 0,
        "originalPositionCount": len(positions),
        "validatedPositionCount": len(positions),
        "batches": [],
    }
    try:
        for batch in _spreadsheet_review_batches(llm.settings, spreadsheet_positions):
            combined_debug["batchCount"] += 1
            response = llm.review_spreadsheet_candidates(batch)
            reviewed_batch, warnings, debug = apply_spreadsheet_candidate_review(batch, response)
            all_warnings.extend(warnings)
            combined_debug["batches"].append(debug)
            batch_ids = {position.candidateId for position in batch}
            for candidate_id in batch_ids:
                current_by_id.pop(candidate_id, None)
            for position in reviewed_batch:
                if position.candidateId:
                    current_by_id[position.candidateId] = position
    except Exception as exc:
        combined_debug["failed"] = True
        combined_debug["error"] = f"{type(exc).__name__}: {exc}"
        return positions, [
            "Excel candidate review недоступен; deterministic Excel позиции сохранены без LLM-фильтра: "
            f"{type(exc).__name__}: {exc}"
        ], combined_debug

    ordered: list[TenderPosition] = []
    emitted: set[str] = set()
    for position in positions:
        if position.candidateId and position.candidateId in current_by_id:
            ordered.append(current_by_id[position.candidateId])
            emitted.add(position.candidateId)
        elif not position.candidateId:
            ordered.append(position)
    for candidate_id, position in current_by_id.items():
        if candidate_id not in emitted and candidate_id.startswith("llm:new:"):
            ordered.append(position)

    combined_debug["validatedPositionCount"] = len(ordered)
    combined_debug["applied"] = any(batch.get("applied") for batch in combined_debug["batches"])
    return ordered, list(dict.fromkeys(all_warnings)), combined_debug


def validate_product_candidates(
    llm: LlmClient,
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    debug = _base_debug(len(positions))
    if not positions:
        return [], [], debug

    retained: list[TenderPosition] = []
    deterministic_rejections: list[dict[str, Any]] = []
    for index, position in enumerate(positions, start=1):
        rejected_role = _deterministic_non_product_role(position.product)
        if rejected_role:
            deterministic_rejections.append(
                {
                    "positionIndex": index,
                    "product": position.product,
                    "role": rejected_role,
                    "confidence": 1.0,
                    "rationale": (
                        "Заголовок, характеристика или служебная ячейка, "
                        "а не наименование товара"
                    ),
                }
            )
        else:
            retained.append(position)

    warnings = [
        f"Исключена служебная строка, не являющаяся товаром: {item['product'][:200]}."
        for item in deterministic_rejections
    ]
    if not retained:
        debug.update(
            {
                "applied": bool(deterministic_rejections),
                "requiresManualReview": True,
                "validatedPositionCount": 0,
                "rejectedPositions": deterministic_rejections,
                "rejectedPositionCount": len(deterministic_rejections),
                "unresolved": [
                    {
                        "positionIndex": 1,
                        "product": positions[0].product,
                        "reason": "После детерминированной проверки не осталось товаров",
                    }
                ],
            }
        )
        warnings.append(
            "После детерминированной проверки не осталось товарных позиций; "
            "ложные строки не переданы в поиск по каталогу и расчёт покрытия."
        )
        return [], warnings, debug

    try:
        response = llm.audit_product_candidates(retained)
    except Exception as exc:
        debug.update(
            {
                "requiresManualReview": True,
                "rejectedPositions": deterministic_rejections,
                "unresolved": [
                    {
                        "positionIndex": None,
                        "product": "",
                        "reason": f"LLM-аудит недоступен: {type(exc).__name__}: {exc}",
                    }
                ],
            }
        )
        warnings.append(
            "Обязательный аудит товарных кандидатов не выполнен; "
            "исходные позиции сохранены, расчёт покрытия продолжен: "
            f"{type(exc).__name__}: {exc}"
        )
        return retained, warnings, debug

    validated, audit_warnings, audit_debug = apply_product_candidate_audit(
        retained,
        response,
    )
    audit_debug["rejectedPositions"] = (
        deterministic_rejections + audit_debug["rejectedPositions"]
    )
    audit_debug["rejectedPositionCount"] = len(audit_debug["rejectedPositions"])
    audit_debug["applied"] = audit_debug["applied"] or bool(deterministic_rejections)
    audit_debug["originalPositionCount"] = len(positions)
    return validated, list(dict.fromkeys(warnings + audit_warnings)), audit_debug
