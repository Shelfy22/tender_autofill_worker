from __future__ import annotations

from typing import Any, Iterable

from app.models import ProductMatchItem
from app.services.normalization import parse_number


def round_money(value: float) -> float:
    return round(value + 1e-12, 2)


def format_rub(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " руб."


def _real_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"null", "undefined", "товар не найден"}:
        return None
    return text


def summarize_product_coverage(items: Iterable[ProductMatchItem | dict[str, Any]]) -> dict[str, Any]:
    normalized = [item if isinstance(item, ProductMatchItem) else ProductMatchItem.model_validate(item) for item in items]
    details: list[dict[str, Any]] = []

    for index, item in enumerate(normalized, start=1):
        match = item.match
        article = _real_value(match.article)
        link = _real_value(match.link)
        if not link and article:
            link = f"https://www.etm.ru/cat/nn/{article}"
        has_catalog_evidence = bool(article or link)
        full_match = match.correspondence == "Полное соответствие" and has_catalog_evidence
        analog_match = match.correspondence == "Аналог" and has_catalog_evidence
        analog_accepted = analog_match and item.analogsAllowed is not False
        supplied = full_match or analog_accepted
        unit_price = parse_number(match.median_price) if supplied else None
        quantity = parse_number(item.quantity, positive=True)
        position_total = (
            round_money(unit_price * quantity)
            if supplied and unit_price is not None and quantity is not None
            else None
        )
        details.append(
            {
                "positionIndex": item.positionIndex or index,
                "sourceProduct": item.product,
                "productQuery": item.productQuery,
                "brand": item.brand,
                "article": article or "",
                "sourceQuantity": item.quantity,
                "quantity": quantity,
                "analogsAllowed": item.analogsAllowed,
                "supplied": supplied,
                "fullMatch": full_match,
                "analogMatch": analog_match,
                "analogAccepted": analog_accepted,
                "hasCatalogEvidence": has_catalog_evidence,
                "medianUnitPriceRub": unit_price,
                "positionTotalPriceRub": position_total,
                "priceSource": match.price_source,
                "priceCurrency": match.currency if unit_price is not None else None,
                "result": match.model_dump(by_alias=True),
            }
        )

    total = len(details)
    supplied_details = [detail for detail in details if detail["supplied"]]
    supplied_count = len(supplied_details)
    full_match_count = sum(bool(detail["fullMatch"]) for detail in details)
    analog_count = sum(bool(detail["analogAccepted"]) for detail in details)
    rejected_analog_count = sum(
        bool(detail["analogMatch"] and not detail["analogAccepted"]) for detail in details
    )
    coverage = round(supplied_count / total * 100, 2) if total else None
    coverage_approved = total > 0 and coverage is not None and coverage > 50
    assortment_reject = total > 0 and coverage is not None and coverage <= 50

    priced_count = sum(detail["medianUnitPriceRub"] is not None for detail in supplied_details)
    quantity_known_count = sum(detail["quantity"] is not None for detail in supplied_details)
    fully_calculated_count = sum(detail["positionTotalPriceRub"] is not None for detail in supplied_details)
    median_sum = round_money(sum(detail["medianUnitPriceRub"] or 0 for detail in supplied_details))
    quantity_total = round_money(sum(detail["positionTotalPriceRub"] or 0 for detail in supplied_details))
    median_complete = supplied_count > 0 and priced_count == supplied_count
    price_complete = supplied_count > 0 and fully_calculated_count == supplied_count
    threshold_applicable = coverage_approved and price_complete
    value_reject = threshold_applicable and quantity_total < 1_000_000

    if not total:
        price_summary = "Сумма медианных цен не определена: товарные позиции для проверки не извлечены."
        coverage_summary = "Товарные позиции для проверки ассортимента не извлечены."
    elif not supplied_count:
        price_summary = (
            "Сумма медианных цен поставляемого ассортимента: 0,00 руб.; "
            "точные совпадения и допустимые аналоги не найдены."
        )
        coverage_summary = (
            f"Покрытие ассортимента {coverage}% (0 из {total}); порог согласования не пройден."
        )
    else:
        median_part = (
            f"Сумма медианных цен поставляемых позиций: {format_rub(median_sum)} "
            f"(цена найдена для {priced_count} из {supplied_count})."
        )
        if price_complete:
            quantity_part = (
                "Расчётная сумма с учётом количества (медианная цена единицы × количество): "
                f"{format_rub(quantity_total)}."
            )
        else:
            missing = supplied_count - fully_calculated_count
            quantity_part = (
                f"Для {missing} из {supplied_count} поставляемых позиций нет цены и/или количества, "
                "поэтому автоматический порог 1 млн руб. не применяется."
            )
        price_summary = f"{median_part} {quantity_part}"
        coverage_summary = (
            f"Покрытие ассортимента {coverage}% ({supplied_count} из {total}); "
            f"точных совпадений {full_match_count}, допустимых аналогов {analog_count}, "
            f"не закрыто {total - supplied_count}. "
            + ("Порог больше 50% пройден." if coverage_approved else "Порог больше 50% не пройден.")
        )

    return {
        "status": "evaluated" if total else "not_evaluated",
        "total": total,
        "suppliedCount": supplied_count,
        "fullMatchCount": full_match_count,
        "analogMatchCount": analog_count,
        "rejectedAnalogCount": rejected_analog_count,
        "notFoundCount": max(0, total - supplied_count),
        "coveragePercent": coverage,
        "coverageApproved": coverage_approved,
        "approvalThresholdExclusive": 50,
        "hardReject": assortment_reject,
        "hardRejectReason": "Непоставляемый ассортимент" if assortment_reject else None,
        "pricedSuppliedCount": priced_count,
        "quantityKnownSuppliedCount": quantity_known_count,
        "fullyCalculatedSuppliedCount": fully_calculated_count,
        "medianPriceSumRub": median_sum,
        "medianPriceSumFormatted": format_rub(median_sum),
        "medianPriceEvaluationComplete": median_complete,
        "quantityAdjustedTotalRub": quantity_total,
        "quantityAdjustedTotalFormatted": format_rub(quantity_total),
        "supplyTotalPriceRub": quantity_total,
        "supplyTotalPriceFormatted": format_rub(quantity_total),
        "priceEvaluationComplete": price_complete,
        "supplyValueThresholdRub": 1_000_000,
        "supplyValueThresholdApplicable": threshold_applicable,
        "supplyValueHardReject": value_reject,
        "supplyValueHardRejectReason": (
            "Коммерческие условия. НМЦК менее 1 млн руб." if value_reject else None
        ),
        "priceSummary": price_summary,
        "summary": f"{coverage_summary} {price_summary}",
        "details": details,
    }
