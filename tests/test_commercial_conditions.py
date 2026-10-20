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


def test_commercial_context_keeps_conditional_unguaranteed_volume_request() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Гарантированный объём поставляется с 01.02.2027 по 28.02.2027.\n\n"
                "Количество товара из негарантированного объёма поставляются исключительно "
                "по письменному запросу Заказчика. Негарантированный объём: "
                "с 01.02.2027 по 15.12.2027."
            )
        ],
    )

    assert "targets: delivery; anchor: strong" in context
    assert "негарантированного объёма" in context
    assert "письменному запросу Заказчика" in context


def test_commercial_context_prioritizes_request_delivery_over_generic_deadlines() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Срок поставки указан в спецификации.\n\n"
                "Срок поставки согласуется сторонами.\n\n"
                "Срок поставки действует до 31.12.2027.\n\n"
                "Товар поставляется отдельными партиями с учетом потребностей Покупателя "
                "на основании заявок Покупателя."
            )
        ],
    )

    assert "отдельными партиями" in context
    assert "на основании заявок Покупателя" in context


def test_commercial_context_supports_procurement_wording_variants() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Расчеты производятся в течение 30 календарных дней с даты приемки.\n\n"
                "Поставка единовременно одной партией не позднее 15.12.2026.\n\n"
                "Срок гарантии на оборудование составляет 60 месяцев.\n\n"
                "Минимальная обязательная доля закупок товаров российского происхождения "
                "установлена по ПП РФ № 1875.\n\n"
                "Рассмотрение заявок, оценка и сопоставление заявок (подведение итогов) "
                "проводится 01.11.2026."
            )
        ],
    )

    assert "targets: paymentDelayDays; anchor: strong" in context
    assert "targets: delivery; anchor: strong" in context
    assert "targets: warrantyMonths; anchor: strong" in context
    assert "targets: nationalRegime; anchor: strong" in context
    assert "targets: resultDate; anchor: strong" in context


def test_commercial_context_limits_a_field_and_keeps_other_field_evidence() -> None:
    payment_paragraphs = "\n\n".join(
        f"Отсрочка платежа по части {number} составляет {number} календарных дней."
        for number in range(1, 8)
    )
    context = build_commercial_conditions_context(
        "",
        [
            document(
                f"{payment_paragraphs}\n\n"
                "Минимальная обязательная доля закупок установлена по ПП РФ № 1875."
            )
        ],
        max_fragments=4,
        max_fragments_per_field=1,
    )

    assert context.count("targets: paymentDelayDays") == 1
    assert "targets: nationalRegime" in context


def test_commercial_context_does_not_select_a_bare_payment_word() -> None:
    context = build_commercial_conditions_context(
        "",
        [document("Оплата указана в бухгалтерской справке. Поставка согласована отдельно.")],
    )

    assert "targets: paymentDelayDays" not in context


def test_commercial_context_keeps_contextual_fallbacks_for_common_contract_phrases() -> None:
    context = build_commercial_conditions_context(
        "",
        [
            document(
                "Оплата фактически принятого товара производится в течение 45 дней.\n\n"
                "Поставка товара осуществляется после подписания договора.\n\n"
                "Гарантия со дня ввода оборудования в эксплуатацию составляет 12 месяцев."
            )
        ],
    )

    assert "targets: paymentDelayDays; anchor: supporting" in context
    assert "targets: delivery; anchor: supporting" in context
    assert "targets: warrantyMonths; anchor: supporting" in context
