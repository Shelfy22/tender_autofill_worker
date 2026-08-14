from app.models import DocumentPriceSource, TenderPosition, TenderPositionsResponse
from app.services.products import extract_deterministic_positions, merge_positions, parse_quantity


def test_quantity_parsing() -> None:
    assert parse_quantity("16 шт.") == 16
    assert parse_quantity("2,5") == 2.5
    assert parse_quantity(None) is None


def test_excel_like_position_extraction_preserves_quantity() -> None:
    positions = extract_deterministic_positions(
        "№ п/п Наименование товара Ед. изм. Кол-во 1 Моноблок штука 16 тип моноблок"
    )
    assert len(positions) == 1
    assert positions[0].product == "Моноблок"
    assert positions[0].quantity == 16
    assert positions[0].unit.lower() == "штука"


def test_merge_preserves_deterministic_document_price_on_llm_position() -> None:
    deterministic = [
        TenderPosition(
            product="Кабель",
            productQuery="Кабель",
            quantity=5,
            unit="шт",
            documentUnitPriceRub=100,
            documentLineTotalRub=500,
            documentCurrency="RUB",
            documentPriceSource=DocumentPriceSource(
                fileName="spec.xlsx",
                sheet="Лист1",
                row=2,
                unitPriceColumn="E",
                lineTotalColumn="F",
                extractionMethod="excel_deterministic",
            ),
        )
    ]
    llm = TenderPositionsResponse(
        products=[TenderPosition(product="Кабель", productQuery="Кабель", quantity=5, unit="шт")]
    )

    merged, warnings = merge_positions(deterministic, llm)

    assert not warnings
    assert len(merged) == 1
    assert merged[0].documentUnitPriceRub == 100
    assert merged[0].documentLineTotalRub == 500
    assert merged[0].documentPriceSource is not None
    assert merged[0].documentPriceSource.extractionMethod == "excel_deterministic"
