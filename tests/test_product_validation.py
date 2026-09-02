from app.models import (
    ProductCandidateAssignment,
    ProductCandidateAuditResponse,
    ProductSourceReference,
    TenderPosition,
)
from app.services.product_validation import (
    apply_product_candidate_audit,
    validate_product_candidates,
)


def _position(
    product: str,
    *,
    quantity: float | None = 1,
    unit: str = "шт",
    requirements: str = "",
) -> TenderPosition:
    return TenderPosition(
        product=product,
        productQuery=product,
        quantity=quantity,
        unit=unit,
        requirements=requirements,
    )


def test_high_confidence_characteristic_is_removed_before_catalog() -> None:
    positions = [
        _position("3640400.1.1", quantity=220),
        _position("Электропечь ПЭТ-4 1.6кВт 220В", quantity=220),
    ]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="characteristic",
                confidence=0.99,
                rationale="Код характеристики без товарного наименования",
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="purchase_item",
                confidence=0.99,
            ),
        ]
    )

    validated, warnings, debug = apply_product_candidate_audit(positions, response)

    assert [item.product for item in validated] == ["Электропечь ПЭТ-4 1.6кВт 220В"]
    assert debug["rejectedPositionCount"] == 1
    assert debug["requiresManualReview"] is False
    assert any("не являющаяся товаром" in item for item in warnings)


def test_contacts_repeated_with_and_without_quantity_are_merged() -> None:
    positions = [
        _position("Контактор КТЭ 630А 230В", quantity=None),
        _position("Контактор КТЭ 630А 230В", quantity=26),
    ]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.98,
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="duplicate",
                duplicateOf=1,
                confidence=0.98,
                rationale="То же требование повторено с заполненным количеством",
            ),
        ]
    )

    validated, _, debug = apply_product_candidate_audit(positions, response)

    assert len(validated) == 1
    assert validated[0].quantity == 26
    assert debug["duplicateCount"] == 1
    assert debug["requiresManualReview"] is False


def test_compact_model_code_confirms_short_and_long_name_duplicate() -> None:
    positions = [
        _position("Портативный анализатор спектра FPL1014", quantity=None),
        _position(
            "Анализатор спектра Rohde & Schwarz FPL1014 для испытаний ЭМС",
            quantity=2,
        ),
    ]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.98,
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="duplicate",
                duplicateOf=1,
                confidence=0.98,
                rationale="Одна модель FPL1014 приведена коротким и полным названием",
            ),
        ]
    )

    validated, _, debug = apply_product_candidate_audit(positions, response)

    assert len(validated) == 1
    assert validated[0].product == "Портативный анализатор спектра FPL1014"
    assert validated[0].quantity == 2
    assert debug["duplicateCount"] == 1
    assert debug["requiresManualReview"] is False


def test_conflicting_duplicate_quantities_require_manual_review() -> None:
    positions = [
        _position("Кабель АВБШв-1 4х120", quantity=1642, unit="м"),
        _position("Кабель силовой бронированный АВБШв-1 4х120", quantity=1612, unit="м"),
    ]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.99,
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="duplicate",
                duplicateOf=1,
                confidence=0.99,
            ),
        ]
    )

    validated, warnings, debug = apply_product_candidate_audit(positions, response)

    assert len(validated) == 2
    assert debug["requiresManualReview"] is True
    assert "конфликтующие количества" in debug["unresolved"][0]["reason"]
    assert any("расчёт покрытия продолжен" in item for item in warnings)


def test_ktp_component_is_removed_only_with_parent_context() -> None:
    positions = [
        _position("Комплектная трансформаторная подстанция КТП-1000"),
        _position("Трансформатор ТМГ-1000", quantity=1),
    ]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.99,
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="component",
                parentPositionIndex=1,
                confidence=0.96,
                rationale="Входит в состав КТП",
            ),
        ]
    )

    validated, _, debug = apply_product_candidate_audit(positions, response)

    assert [item.product for item in validated] == [
        "Комплектная трансформаторная подстанция КТП-1000"
    ]
    assert "Трансформатор ТМГ-1000" in validated[0].requirements
    assert debug["componentCount"] == 1
    assert debug["requiresManualReview"] is False


def test_missing_audit_assignment_keeps_position_and_requires_review() -> None:
    positions = [_position("Выключатель"), _position("Рубильник")]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.99,
            )
        ]
    )

    validated, _, debug = apply_product_candidate_audit(positions, response)

    assert validated == positions
    assert debug["requiresManualReview"] is True
    assert debug["unresolved"][0]["positionIndex"] == 2


def test_excel_candidate_without_source_coordinates_requires_review() -> None:
    position = _position("Кабель силовой", quantity=12, unit="м")
    position = position.model_copy(
        update={"evidence": "Строка 2: A: 1 | B: Кабель силовой | D: м | E: 12"}
    )
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.99,
            )
        ]
    )

    validated, _, debug = apply_product_candidate_audit([position], response)

    assert validated == [position]
    assert debug["requiresManualReview"] is True
    assert "отсутствуют координаты" in debug["unresolved"][0]["reason"]


def test_excel_candidate_with_matching_product_column_passes_source_check() -> None:
    position = _position("Кабель силовой", quantity=12, unit="м").model_copy(
        update={
            "evidence": "Строка 2: A: 1 | B: Кабель силовой | D: м | E: 12",
            "sourceReference": ProductSourceReference(
                fileName="spec.xlsx",
                sheet="Лист1",
                row=2,
                productColumn="B",
                quantityColumn="E",
                unitColumn="D",
                extractionMethod="excel_deterministic",
            ),
        }
    )
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="purchase_item",
                confidence=0.99,
            )
        ]
    )

    validated, _, debug = apply_product_candidate_audit([position], response)

    assert validated == [position]
    assert debug["requiresManualReview"] is False


def test_standalone_analogs_phrase_is_removed_before_llm_audit() -> None:
    class AuditLlm:
        seen: list[TenderPosition] = []

        def audit_product_candidates(
            self,
            positions: list[TenderPosition],
        ) -> ProductCandidateAuditResponse:
            self.seen = positions
            return ProductCandidateAuditResponse(
                assignments=[
                    ProductCandidateAssignment(
                        positionIndex=1,
                        role="purchase_item",
                        confidence=0.99,
                    )
                ]
            )

    llm = AuditLlm()
    validated, warnings, debug = validate_product_candidates(
        llm,  # type: ignore[arg-type]
        [
            _position("Аналоги рассматриваются", quantity=162),
            _position("Термостат комнатный механический", quantity=162),
        ],
    )

    assert [item.product for item in llm.seen] == ["Термостат комнатный механический"]
    assert [item.product for item in validated] == ["Термостат комнатный механический"]
    assert debug["rejectedPositionCount"] == 1
    assert any("служебная строка" in item for item in warnings)


def test_headers_characteristics_and_preference_cells_are_removed_before_audit() -> None:
    class AuditLlm:
        seen: list[TenderPosition] = []

        def audit_product_candidates(
            self,
            positions: list[TenderPosition],
        ) -> ProductCandidateAuditResponse:
            self.seen = positions
            return ProductCandidateAuditResponse(
                assignments=[
                    ProductCandidateAssignment(
                        positionIndex=1,
                        role="purchase_item",
                        confidence=0.99,
                    )
                ]
            )

    llm = AuditLlm()
    validated, _, debug = validate_product_candidates(
        llm,  # type: ignore[arg-type]
        [
            _position("Технические характеристики"),
            _position("Количество полюсов", quantity=3),
            _position("Преимущество"),
            _position("Ограничение установлено; преимущество не установлено"),
            _position("Выключатель автоматический", quantity=10),
        ],
    )

    assert [item.product for item in llm.seen] == ["Выключатель автоматический"]
    assert [item.product for item in validated] == ["Выключатель автоматический"]
    assert debug["rejectedPositionCount"] == 4


def test_structured_excel_cells_are_sent_to_llm_audit() -> None:
    class AuditLlm:
        seen: list[TenderPosition] = []

        def audit_product_candidates(
            self,
            positions: list[TenderPosition],
        ) -> ProductCandidateAuditResponse:
            self.seen = positions
            return ProductCandidateAuditResponse(
                assignments=[
                    ProductCandidateAssignment(
                        positionIndex=1,
                        role="purchase_item",
                        confidence=0.99,
                    )
                ]
            )

    position = _position(
        "Выключатель автоматический",
        quantity=10,
        unit="шт",
    ).model_copy(
        update={
            "sourceCells": {
                "A": "1",
                "B": "Выключатель автоматический",
                "C": "шт",
                "D": "10",
                "E": "Количество полюсов: 3",
            },
            "sourceReference": ProductSourceReference(
                fileName="spec.xlsx",
                sheet="Товары",
                row=2,
                productColumn="B",
                unitColumn="C",
                quantityColumn="D",
                extractionMethod="excel_deterministic",
            ),
        }
    )
    llm = AuditLlm()

    validated, _, debug = validate_product_candidates(
        llm,  # type: ignore[arg-type]
        [position],
    )

    assert llm.seen[0].sourceCells["B"] == "Выключатель автоматический"
    assert llm.seen[0].sourceCells["D"] == "10"
    assert validated == [position]
    assert debug["requiresManualReview"] is False


def test_only_non_product_cells_do_not_return_to_coverage_denominator() -> None:
    class AuditMustNotRun:
        def audit_product_candidates(
            self,
            positions: list[TenderPosition],
        ) -> ProductCandidateAuditResponse:
            raise AssertionError("LLM-аудит не должен запускаться без товарных кандидатов")

    validated, warnings, debug = validate_product_candidates(
        AuditMustNotRun(),  # type: ignore[arg-type]
        [
            _position("Технические характеристики"),
            _position("Количество полюсов"),
            _position("Преимущество"),
        ],
    )

    assert validated == []
    assert debug["requiresManualReview"] is True
    assert debug["rejectedPositionCount"] == 3
    assert any("не переданы" in item for item in warnings)


def test_llm_removing_every_position_does_not_restore_false_products() -> None:
    positions = [_position("Адрес поставки"), _position("Компонент комплекта")]
    response = ProductCandidateAuditResponse(
        assignments=[
            ProductCandidateAssignment(
                positionIndex=1,
                role="address",
                confidence=0.99,
            ),
            ProductCandidateAssignment(
                positionIndex=2,
                role="header",
                confidence=0.99,
            ),
        ]
    )

    validated, warnings, debug = apply_product_candidate_audit(positions, response)

    assert validated == []
    assert debug["requiresManualReview"] is True
    assert debug["rejectedPositionCount"] == 2
    assert any("не переданы" in item for item in warnings)


def test_llm_audit_failure_blocks_automatic_decision_without_dropping_positions() -> None:
    class BrokenLlm:
        def audit_product_candidates(
            self,
            positions: list[TenderPosition],
        ) -> ProductCandidateAuditResponse:
            raise RuntimeError("provider unavailable")

    positions = [_position("Кабель силовой", quantity=10, unit="м")]
    validated, warnings, debug = validate_product_candidates(
        BrokenLlm(),  # type: ignore[arg-type]
        positions,
    )

    assert validated == positions
    assert debug["requiresManualReview"] is True
    assert any("расчёт покрытия продолжен" in item for item in warnings)
