from app.services.products import extract_deterministic_positions, parse_quantity


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
