from __future__ import annotations

from app.services.document_roles import (
    COMPOSITE_ROLE,
    PRICE_ROLE,
    TECHNICAL_ROLE,
    classify_document_role,
    detect_section_role,
)
from app.services.documents import _classify_document_kind


def test_price_justification_titles_are_recognized_without_nmck_abbreviation() -> None:
    assert detect_section_role("Обоснование цены") == PRICE_ROLE
    assert (
        detect_section_role(
            "СВЕДЕНИЯ О НАЧАЛЬНОЙ (МАКСИМАЛЬНОЙ) ЦЕНЕ "
            "КАЖДОЙ ЕДИНИЦЫ ПРОДУКЦИИ"
        )
        == PRICE_ROLE
    )
    assert classify_document_role("Расчет НМЦД.xlsx") == PRICE_ROLE


def test_technical_and_composite_documents_are_recognized() -> None:
    assert detect_section_role("4. ТЕХНИЧЕСКОЕ ЗАДАНИЕ") == TECHNICAL_ROLE
    text = "\n".join(
        (
            "3. ПРОЕКТ ДОГОВОРА",
            "4. ТЕХНИЧЕСКОЕ ЗАДАНИЕ",
            "5. ОБОСНОВАНИЕ НАЧАЛЬНОЙ МАКСИМАЛЬНОЙ ЦЕНЫ ДОГОВОРА",
        )
    )

    assert classify_document_role("Извещение.doc", text) == COMPOSITE_ROLE
    assert _classify_document_kind("Извещение.doc", "Извещение.doc", text) == "composite"
