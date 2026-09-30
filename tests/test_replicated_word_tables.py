from app.models import ProductSourceReference, TenderPosition
from app.services.products import merge_positions


def test_merges_identified_single_row_duplicate_word_tables() -> None:
    positions = [
        TenderPosition(
            product="\u041f\u0440\u043e\u0432\u043e\u0434 \u0421\u0418\u041f-4 4\u044525-0,6/1",
            quantity=0.4,
            unit="\u043a\u043c",
            sourceReference=ProductSourceReference(
                table="\u0422\u0430\u0431\u043b\u0438\u0446\u0430 Word 1",
                row=2,
            ),
        ),
        TenderPosition(
            product="\u041f\u0440\u043e\u0432\u043e\u0434 \u0421\u0418\u041f-4 4\u044525-0,6/1",
            quantity=0.4,
            unit="\u043a\u043c",
            sourceReference=ProductSourceReference(
                table="\u0422\u0430\u0431\u043b\u0438\u0446\u0430 Word 2",
                row=2,
            ),
        ),
    ]

    merged, warnings = merge_positions(positions, None, [])

    assert [(item.product, item.quantity, item.unit) for item in merged] == [
        ("\u041f\u0440\u043e\u0432\u043e\u0434 \u0421\u0418\u041f-4 4\u044525-0,6/1", 0.4, "\u043a\u043c")
    ]
    assert any("replicated table" in warning for warning in warnings)
