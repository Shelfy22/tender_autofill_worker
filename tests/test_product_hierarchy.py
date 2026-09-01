from app.models import (
    ProductHierarchyAssignment,
    ProductHierarchyResponse,
    TenderPosition,
)
from app.services.product_hierarchy import (
    apply_product_hierarchy,
    hierarchy_review_needed,
    resolve_product_hierarchy,
)


def _position(
    product: str,
    *,
    quantity: float | None = 1,
    requirements: str = "",
    evidence: str = "",
) -> TenderPosition:
    return TenderPosition(
        product=product,
        productQuery=product,
        quantity=quantity,
        unit="шт",
        requirements=requirements,
        evidence=evidence,
    )


def test_ktp_list_requests_hierarchy_review() -> None:
    positions = [
        _position("Комплектная трансформаторная подстанция КТП-1000"),
        _position("Трансформатор силовой"),
    ]

    assert hierarchy_review_needed(positions) is True


def test_unrelated_independent_goods_do_not_request_review() -> None:
    positions = [_position("Выключатель"), _position("Рубильник")]

    assert hierarchy_review_needed(positions) is False


def test_confident_ktp_components_are_not_purchase_items() -> None:
    positions = [
        _position("КТП-1000", quantity=2),
        _position("КТП-630", quantity=1),
        _position("Трансформатор силовой", quantity=2),
        _position("Шкаф РУНН", quantity=2),
        _position("Разъединитель", quantity=4),
    ]
    response = ProductHierarchyResponse(
        assignments=[
            ProductHierarchyAssignment(
                positionIndex=1, role="purchase_item", confidence=0.99
            ),
            ProductHierarchyAssignment(
                positionIndex=2, role="purchase_item", confidence=0.99
            ),
            ProductHierarchyAssignment(
                positionIndex=3,
                role="component",
                parentPositionIndex=1,
                confidence=0.95,
                rationale="Входит в состав КТП-1000",
            ),
            ProductHierarchyAssignment(
                positionIndex=4,
                role="component",
                parentPositionIndex=1,
                confidence=0.91,
            ),
            ProductHierarchyAssignment(
                positionIndex=5,
                role="component",
                parentPositionIndex=2,
                confidence=0.92,
            ),
        ]
    )

    purchase_items, warnings, debug = apply_product_hierarchy(positions, response)

    assert [item.product for item in purchase_items] == ["КТП-1000", "КТП-630"]
    assert "Трансформатор силовой" in purchase_items[0].requirements
    assert debug["applied"] is True
    assert debug["originalPositionCount"] == 5
    assert debug["purchaseItemCount"] == 2
    assert debug["componentCount"] == 3
    assert len(debug["components"]) == 3
    assert warnings


def test_low_confidence_component_is_retained() -> None:
    positions = [_position("КТП-1000"), _position("Трансформатор")]
    response = ProductHierarchyResponse(
        assignments=[
            ProductHierarchyAssignment(
                positionIndex=2,
                role="component",
                parentPositionIndex=1,
                confidence=0.79,
            )
        ]
    )

    purchase_items, _, debug = apply_product_hierarchy(positions, response)

    assert purchase_items == positions
    assert debug["applied"] is False


def test_component_without_parent_context_is_retained() -> None:
    positions = [_position("Выключатель"), _position("Рубильник")]
    response = ProductHierarchyResponse(
        assignments=[
            ProductHierarchyAssignment(
                positionIndex=2,
                role="component",
                parentPositionIndex=1,
                confidence=0.99,
            )
        ]
    )

    purchase_items, _, debug = apply_product_hierarchy(positions, response)

    assert purchase_items == positions
    assert debug["componentCount"] == 0


def test_hierarchy_llm_failure_keeps_all_positions() -> None:
    class BrokenLlm:
        def classify_product_hierarchy(
            self, positions: list[TenderPosition]
        ) -> ProductHierarchyResponse:
            raise RuntimeError("provider unavailable")

    positions = [_position("КТП-1000"), _position("Трансформатор")]

    purchase_items, warnings, debug = resolve_product_hierarchy(
        BrokenLlm(),  # type: ignore[arg-type]
        positions,
    )

    assert purchase_items == positions
    assert debug["reviewRequested"] is True
    assert debug["applied"] is False
    assert "provider unavailable" in warnings[0]
