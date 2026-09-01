from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.models import ProductHierarchyResponse, TenderPosition

if TYPE_CHECKING:
    from app.services.llm import LlmClient


HIERARCHY_MIN_CONFIDENCE = 0.8
HIERARCHY_COMPONENT_LIMIT = 100
_KTP_PATTERN = re.compile(r"\b(?:2\s*)?\u043a\u0442\u043f(?:\u043d|\u043f)?\b", re.I)
_COMPONENT_TERMS = (
    "\u0432 \u0441\u043e\u0441\u0442\u0430\u0432\u0435",
    "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u043d\u043e\u0441\u0442",
    "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u0430\u0446",
    "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442 \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438",
    "\u0441\u043e\u0441\u0442\u0430\u0432\u043d\u0430\u044f \u0447\u0430\u0441\u0442\u044c",
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _position_text(position: TenderPosition) -> str:
    return _clean(
        " ".join(
            (position.product, position.productQuery or "", position.requirements, position.evidence)
        )
    ).casefold()


def _is_assembly_parent(position: TenderPosition) -> bool:
    text = _position_text(position)
    is_substation = "\u043f\u043e\u0434\u0441\u0442\u0430\u043d\u0446" in text and (
        "\u0442\u0440\u0430\u043d\u0441\u0444\u043e\u0440\u043c\u0430\u0442\u043e\u0440" in text
        or "\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u043d" in text
    )
    return bool(is_substation or _KTP_PATTERN.search(text))


def _has_component_context(position: TenderPosition) -> bool:
    text = _position_text(position)
    return any(term in text for term in _COMPONENT_TERMS)


def hierarchy_review_needed(positions: list[TenderPosition]) -> bool:
    if len(positions) < 2:
        return False
    return any(
        _is_assembly_parent(position) or _has_component_context(position)
        for position in positions
    )


def _component_requirement(
    position: TenderPosition,
    *,
    confidence: float,
    rationale: str,
) -> str:
    quantity = "" if position.quantity is None else f"{position.quantity:g}"
    identity = " ".join(
        part
        for part in (
            position.product,
            quantity,
            position.unit,
        )
        if part
    )
    suffix = f"; confidence={confidence:.2f}"
    if rationale:
        suffix += f"; {rationale}"
    return f"- {identity}{suffix}"


def _base_debug(position_count: int, review_requested: bool) -> dict[str, Any]:
    return {
        "reviewRequested": review_requested,
        "applied": False,
        "originalPositionCount": position_count,
        "purchaseItemCount": position_count,
        "componentCount": 0,
        "components": [],
    }


def apply_product_hierarchy(
    positions: list[TenderPosition],
    response: ProductHierarchyResponse,
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    debug = _base_debug(len(positions), True)
    warnings = list(response.warnings)
    assignments = {
        assignment.positionIndex: assignment
        for assignment in response.assignments
        if 1 <= assignment.positionIndex <= len(positions)
    }
    component_indexes: set[int] = set()
    components_by_parent: dict[int, list[str]] = {}

    for index, position in enumerate(positions, start=1):
        assignment = assignments.get(index)
        if (
            assignment is None
            or assignment.role != "component"
            or assignment.confidence < HIERARCHY_MIN_CONFIDENCE
            or assignment.parentPositionIndex is None
            or assignment.parentPositionIndex == index
            or not 1 <= assignment.parentPositionIndex <= len(positions)
        ):
            continue
        parent = positions[assignment.parentPositionIndex - 1]
        if not (
            _is_assembly_parent(parent)
            or _has_component_context(parent)
            or _has_component_context(position)
        ):
            continue
        component_indexes.add(index)
        components_by_parent.setdefault(assignment.parentPositionIndex, []).append(
            _component_requirement(
                position,
                confidence=assignment.confidence,
                rationale=_clean(assignment.rationale)[:300],
            )
        )
        debug["components"].append(
            {
                "positionIndex": index,
                "product": position.product,
                "quantity": position.quantity,
                "unit": position.unit,
                "parentPositionIndex": assignment.parentPositionIndex,
                "confidence": assignment.confidence,
                "rationale": _clean(assignment.rationale)[:500],
                "source": position.source,
                "evidence": _clean(position.evidence)[:500],
            }
        )

    if not component_indexes:
        return positions, warnings, debug

    purchase_items: list[TenderPosition] = []
    for index, position in enumerate(positions, start=1):
        if index in component_indexes:
            continue
        component_lines = components_by_parent.get(index, [])
        if component_lines:
            component_text = (
                "\nTender components included in this purchase item:\n"
                + "\n".join(component_lines[:HIERARCHY_COMPONENT_LIMIT])
            )
            position = position.model_copy(
                update={
                    "requirements": (
                        _clean(position.requirements) + component_text
                    )[-6000:]
                }
            )
        purchase_items.append(position)

    if not purchase_items:
        warnings.append(
            "Product hierarchy review marked every position as a component; "
            "the original list was retained."
        )
        return positions, warnings, debug

    debug.update(
        {
            "applied": True,
            "purchaseItemCount": len(purchase_items),
            "componentCount": len(component_indexes),
        }
    )
    warnings.append(
        f"{len(component_indexes)} component positions were excluded from "
        "catalog search and coverage; they remain in hierarchy diagnostics."
    )
    return purchase_items, warnings, debug


def resolve_product_hierarchy(
    llm: LlmClient,
    positions: list[TenderPosition],
) -> tuple[list[TenderPosition], list[str], dict[str, Any]]:
    review_needed = hierarchy_review_needed(positions)
    if not review_needed:
        return positions, [], _base_debug(len(positions), False)
    try:
        response = llm.classify_product_hierarchy(positions)
    except Exception as exc:
        warning = (
            "Optional product hierarchy review failed; all extracted positions "
            f"were retained: {type(exc).__name__}: {exc}"
        )
        return positions, [warning], _base_debug(len(positions), True)
    return apply_product_hierarchy(positions, response)
