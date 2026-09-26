from __future__ import annotations

from app.models import (
    DocumentCharacteristicSet,
    ProductCharacteristic,
    TenderPosition,
)
from app.services.product_characteristics import (
    associate_characteristic_sets,
    ensure_position_identity,
    finalize_position_characteristics,
    normalize_product_category,
)


def test_position_key_is_stable_and_source_row_specific() -> None:
    first = TenderPosition(
        product="Светильник",
        quantity=2,
        unit="шт",
        sourceReference={"fileName": "ТЗ.xlsx", "sheet": "Лист1", "row": 7},
    )
    same = ensure_position_identity(first, ordinal=99)
    repeated = ensure_position_identity(first, ordinal=1)
    second = ensure_position_identity(
        first.model_copy(
            update={
                "sourceReference": first.sourceReference.model_copy(update={"row": 8})
            }
        )
    )

    assert same.positionKey == repeated.positionKey
    assert same.positionKey.startswith("pos_")
    assert second.positionKey != same.positionKey


def test_characteristics_attach_by_article_with_high_confidence() -> None:
    positions = [
        TenderPosition(product="Светильник", article="A-100"),
        TenderPosition(product="Светильник", article="A-200"),
    ]
    characteristic_set = DocumentCharacteristicSet(
        productHint="Светильник",
        article="A-200",
        characteristics=[
            ProductCharacteristic(name="Мощность", value="40 Вт")
        ],
    )

    associated, unresolved, warnings, debug = associate_characteristic_sets(
        positions,
        [characteristic_set],
    )

    assert not unresolved
    assert not warnings
    assert not associated[0].characteristics
    characteristic = associated[1].characteristics[0]
    assert characteristic.associationMethod == "article"
    assert characteristic.associationConfidence == 0.99
    assert characteristic.targetPositionKey == associated[1].positionKey
    assert debug["attachedCharacteristicCount"] == 1


def test_same_name_does_not_attach_when_multiple_positions_are_possible() -> None:
    positions = [
        TenderPosition(
            product="Лампа настенная",
            sourceReference={"fileName": "ТЗ.docx", "table": "1", "row": 2},
        ),
        TenderPosition(
            product="Лампа настенная",
            sourceReference={"fileName": "ТЗ.docx", "table": "1", "row": 3},
        ),
    ]
    characteristic_set = DocumentCharacteristicSet(
        productHint="Лампа настенная",
        characteristics=[
            ProductCharacteristic(name="Мощность", value="20 Вт")
        ],
    )

    associated, unresolved, warnings, debug = associate_characteristic_sets(
        positions,
        [characteristic_set],
    )

    assert all(not position.characteristics for position in associated)
    assert len(unresolved) == 1
    assert unresolved[0].associationMethod == "unresolved"
    assert warnings
    assert debug["ambiguousSetCount"] == 1


def test_conflicting_values_from_different_documents_are_marked() -> None:
    position = TenderPosition(
        product="Блок питания",
        characteristics=[
            ProductCharacteristic(
                name="Напряжение питания",
                value="220 В",
                sourceReference={"fileName": "ТЗ.docx"},
                associationConfidence=0.98,
                associationMethod="same_row",
                associationStatus="confirmed",
            ),
            ProductCharacteristic(
                name="Напряжение питания",
                value="380 В",
                sourceReference={"fileName": "Договор.pdf"},
                associationConfidence=0.97,
                associationMethod="model",
                associationStatus="confirmed",
            ),
        ],
    )

    finalized, warnings, debug = finalize_position_characteristics([position])

    assert len(finalized[0].characteristicConflicts) == 1
    assert {
        item.associationStatus for item in finalized[0].characteristics
    } == {"conflicting"}
    assert warnings
    assert debug["conflictCount"] == 1


def test_category_normalization_covers_distant_assortment_groups() -> None:
    assert normalize_product_category("", "Болт М10") == "fasteners"
    assert normalize_product_category("", "Антифриз красный") == "lubricants_fluids"
    assert normalize_product_category("", "Ноутбук промышленный") == "it_multimedia"
    assert normalize_product_category("", "Каска защитная") == "ppe_workwear"
