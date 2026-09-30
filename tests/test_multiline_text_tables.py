from app.services.products import extract_deterministic_positions


def test_pdf_table_continuation_inherits_headers_but_not_price_columns() -> None:
    text = "\n".join(
        [
            "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 PDF 1.1",
            "\u0421\u0442\u0440\u043e\u043a\u0430 1: A: \u2116 | C: \u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 | I: \u0415\u0434. \u0438\u0437\u043c. | L: \u041a\u043e\u043b-\u0432\u043e",
            "\u0421\u0442\u0440\u043e\u043a\u0430 2: A: 1 | C: \u041a\u0430\u0431\u0435\u043b\u044c | G: \u0421\u0435\u0447\u0435\u043d\u0438\u0435: 3x2.5 | I: \u043c | L: 5",
            "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 PDF 2.1",
            "\u0421\u0442\u0440\u043e\u043a\u0430 1: A: 2 | C: \u041f\u0440\u043e\u0432\u043e\u0434 | G: \u0421\u0435\u0447\u0435\u043d\u0438\u0435: 1x2.5 | I: \u043c | L: 7",
            "\u0422\u0430\u0431\u043b\u0438\u0446\u0430 PDF 3.1",
            "\u0421\u0442\u0440\u043e\u043a\u0430 1: A: \u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 | C: \u0426\u0435\u043d\u0430 \u0437\u0430 \u0435\u0434\u0438\u043d\u0438\u0446\u0443",
            "\u0421\u0442\u0440\u043e\u043a\u0430 2: A: \u041a\u0430\u0431\u0435\u043b\u044c | C: 100",
        ]
    )

    positions = extract_deterministic_positions(text)

    assert [(item.product, item.quantity, item.unit) for item in positions] == [
        ("\u041a\u0430\u0431\u0435\u043b\u044c", 5.0, "\u043c"),
        ("\u041f\u0440\u043e\u0432\u043e\u0434", 7.0, "\u043c"),
    ]
    assert positions[1].characteristics[0].value == "1x2.5"
