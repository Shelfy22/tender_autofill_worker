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
        document_unit_price = parse_number(item.documentUnitPriceRub, positive=True)
        document_line_total = parse_number(item.documentLineTotalRub, positive=True)
        document_currency = str(item.documentCurrency or "").strip().upper() or None
        currency_comparable = document_currency in {None, "RUB", "RUR"}
        document_derived_unit_price = (
            round_money(document_line_total / quantity)
            if document_unit_price is None
            and document_line_total is not None
            and quantity is not None
            else None
        )
        document_comparable_unit_price = document_unit_price or document_derived_unit_price
        document_calculated_line_total = (
            round_money(document_unit_price * quantity)
            if document_unit_price is not None and quantity is not None
            else None
        )
        document_line_total_difference_percent = (
            round(
                (document_calculated_line_total - document_line_total)
                / document_line_total
                * 100,
                2,
            )
            if document_calculated_line_total is not None and document_line_total is not None
            else None
        )
        document_line_total_consistent = (
            abs(document_calculated_line_total - document_line_total)
            <= max(1.0, document_line_total * 0.01)
            if document_calculated_line_total is not None and document_line_total is not None
            else None
        )
        document_catalog_difference_rub = (
            round_money(unit_price - document_comparable_unit_price)
            if unit_price is not None
            and document_comparable_unit_price is not None
            and currency_comparable
            else None
        )
        document_catalog_difference_percent = (
            round(document_catalog_difference_rub / document_comparable_unit_price * 100, 2)
            if document_catalog_difference_rub is not None
            and document_comparable_unit_price is not None
            else None
        )
        if document_unit_price is None and document_line_total is None:
            document_validation_status = "not_available"
        elif not currency_comparable:
            document_validation_status = "currency_not_comparable"
        elif document_line_total_consistent is True:
            document_validation_status = "unit_times_quantity_matches_line_total"
        elif document_line_total_consistent is False:
            document_validation_status = "unit_times_quantity_mismatch"
        else:
            document_validation_status = "diagnostic_only"
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
                "selectedPointId": match.qdrant_point_id,
                "productId": match.product_id,
                "priceSourceField": match.price_source_field,
                "priceAggregation": match.price_aggregation,
                "priceCurrency": match.currency if unit_price is not None else None,
                "documentUnitPriceRub": document_unit_price,
                "documentLineTotalRub": document_line_total,
                "documentCurrency": document_currency,
                "documentCalculatedUnitPriceRub": document_derived_unit_price,
                "documentCalculatedLineTotalRub": document_calculated_line_total,
                "documentComparableUnitPriceRub": document_comparable_unit_price,
                "documentUnitPriceDerivedFromLineTotal": document_derived_unit_price is not None,
                "documentLineTotalDifferencePercent": document_line_total_difference_percent,
                "documentLineTotalConsistent": document_line_total_consistent,
                "documentVsCatalogDifferenceRub": document_catalog_difference_rub,
                "documentVsCatalogDifferencePercent": document_catalog_difference_percent,
                "documentPriceValidationStatus": document_validation_status,
                "documentPriceUsedForSupplyValue": False,
                "documentPriceEvidence": item.documentPriceEvidence,
                "documentPriceSource": (
                    item.documentPriceSource.model_dump()
                    if item.documentPriceSource is not None
                    else None
                ),
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
    document_priced_count = sum(
        detail["documentUnitPriceRub"] is not None or detail["documentLineTotalRub"] is not None
        for detail in details
    )
    document_unit_price_count = sum(
        detail["documentUnitPriceRub"] is not None for detail in details
    )
    document_line_total_count = sum(
        detail["documentLineTotalRub"] is not None for detail in details
    )
    document_catalog_comparable_count = sum(
        detail["documentVsCatalogDifferencePercent"] is not None for detail in details
    )
    document_line_checked_count = sum(
        detail["documentLineTotalConsistent"] is not None for detail in details
    )
    document_line_mismatch_count = sum(
        detail["documentLineTotalConsistent"] is False for detail in details
    )

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

    if document_priced_count:
        document_price_summary = (
            f"Цена из документа извлечена для {document_priced_count} из {total} позиций "
            f"(цена единицы: {document_unit_price_count}, сумма строки: {document_line_total_count}); "
            "она сохранена только для диагностики и не используется при расчёте порога 1 млн руб."
        )
        if document_line_mismatch_count:
            document_price_summary += (
                f" Для {document_line_mismatch_count} из {document_line_checked_count} проверенных "
                "строк цена единицы × количество не совпадает с суммой строки в пределах 1%."
            )
    else:
        document_price_summary = "Цена товарных позиций в документах не извлечена."

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
        "priceBasis": "qdrant_selected_product",
        "supplyValueThresholdRub": 1_000_000,
        "supplyValueThresholdApplicable": threshold_applicable,
        "supplyValueHardReject": value_reject,
        "supplyValueHardRejectReason": (
            "Коммерческие условия. НМЦК менее 1 млн руб." if value_reject else None
        ),
        "priceSummary": price_summary,
        "documentPriceSummary": document_price_summary,
        "documentPriceDiagnostics": {
            "mode": "diagnostic_only",
            "usedForSupplyValueThreshold": False,
            "positionsWithDocumentPrice": document_priced_count,
            "positionsWithDocumentUnitPrice": document_unit_price_count,
            "positionsWithDocumentLineTotal": document_line_total_count,
            "positionsComparableWithCatalog": document_catalog_comparable_count,
            "lineTotalsConsistencyChecked": document_line_checked_count,
            "lineTotalsMismatch": document_line_mismatch_count,
        },
        "summary": f"{coverage_summary} {price_summary} {document_price_summary}",
        "details": details,
    }
