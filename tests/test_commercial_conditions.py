from app.models import ParsedDocument
from app.services.commercial_conditions import build_commercial_conditions_context


def document(text: str) -> ParsedDocument:
    return ParsedDocument(documentIndex=1, fileName="Проект договора.docx", text=text)


def test_commercial_context_returns_payment_paragraph_not_value() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Срок поставки составляет 7 дней.\n\n"
                "Оплата продукции производится покупателем с отсрочкой платежа "
                "не менее 30 и не более 60 календарных дней с даты поставки."
            )
        ],
    )

    assert "targets: paymentDelayDays" in context
    assert "не менее 30 и не более 60 календарных дней" in context
    assert "Проект договора.docx" in context
    assert "paymentDelayDays\": 60" not in context


def test_commercial_context_keeps_table_like_national_regime_and_delivery_evidence() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Применение национального режима: запрет установлен, преимущество "
                "установлено согласно ПП РФ № 1875.\n\n"
                "Поставка по заявкам заказчика до 15.12.2026. Каждая партия в течение "
                "30 дней после заявки."
            )
        ],
    )

    assert "ПП РФ № 1875" in context
    assert "Поставка по заявкам" in context
    assert "до 15.12.2026" in context
