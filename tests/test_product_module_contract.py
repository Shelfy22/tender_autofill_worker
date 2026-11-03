"""Public product imports and representative results must survive module moves."""

from app.models import ProductSourceReference, TenderPosition
from app.services.products import extract_deterministic_positions, infer_spreadsheet_headers, merge_positions


def test_equal_products_in_distinct_source_rows_remain_separate():
    positions = [
        TenderPosition(
            product="Cable VVG 3x2.5", quantity=10, unit="m",
            sourceReference=ProductSourceReference(fileName="technical.xlsx", sheet="Goods", row=row),
        )
        for row in (2, 3)
    ]
    merged, _ = merge_positions(positions, None)
    assert len(merged) == 2
    assert [item.quantity for item in merged] == [10, 10]
    assert [item.sourceReference.row for item in merged] == [2, 3]


def test_empty_public_extraction_contract():
    assert extract_deterministic_positions("") == []
    assert infer_spreadsheet_headers([]) == ([], {}, {})
