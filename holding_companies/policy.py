from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime

from holding_companies.models import HoldingTenderExtraction


DELIVERY_DEADLINE_REASON = "Коммерческие условия. Не проходим по сроку поставки"
REMOTE_TERRITORY_REASON = "Коммерческие условия. Поставка в удалённые территории"
CONSIGNMENT_REASON = "Коммерческие условия. Консигнация / хранение у Покупателя за счёт Поставщика"
HIGH_VOLTAGE_REASON = "Номенклатура. Оборудование 35 кВ и выше"
FREQUENCY_DRIVE_REASON = "Номенклатура. Частотный привод 6–10 кВ"
REPAIR_KIT_REASON = "Номенклатура. Ремкомплект / ЗИП / продукция по чертежу"
MILITARY_ACCEPTANCE_REASON = "Номенклатура. Военная приёмка"
ATOMIC_ACCEPTANCE_REASON = "Номенклатура. Атомная приёмка"
SUPPLY_WORK_REASON = "Номенклатура. Поставка с работами"
INDIVISIBLE_LOT_REASON = "Лот неделимый. Не можем скомплектовать более 20% номенклатуры"

STATUS_REASONS: frozenset[str] = frozenset(
    {
        DELIVERY_DEADLINE_REASON,
        REMOTE_TERRITORY_REASON,
        CONSIGNMENT_REASON,
        HIGH_VOLTAGE_REASON,
        FREQUENCY_DRIVE_REASON,
        REPAIR_KIT_REASON,
        MILITARY_ACCEPTANCE_REASON,
        ATOMIC_ACCEPTANCE_REASON,
        SUPPLY_WORK_REASON,
        INDIVISIBLE_LOT_REASON,
    }
)


@dataclass(frozen=True)
class HoldingDecision:
    status: str
    reason: str
    note: str
    warnings: list[str]


def _parse_date(value: str | None) -> date | None:
    source = str(value or "").strip()
    if not source:
        return None
    normalized = source.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", source)
    if match is None:
        return None
    day, month, year = map(int, match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _delivery_deadline_exceeded(
    extraction: HoldingTenderExtraction,
    received_at: str | None,
) -> bool:
    received = _parse_date(received_at)
    delivery = _parse_date(extraction.deliveryEndDate)
    return bool(received and delivery and delivery > _add_months(received, 18))


def _indivisible_lot_not_covered(
    extraction: HoldingTenderExtraction,
    product_check: dict[str, object],
) -> bool:
    coverage = product_check.get("coveragePercent")
    return (
        extraction.lotDivisible is False
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and coverage < 80
    )


def resolve_status(
    extraction: HoldingTenderExtraction,
    coverage_note: str,
    *,
    product_check: dict[str, object] | None = None,
    received_at: str | None = None,
) -> HoldingDecision:
    proposed_status = extraction.tenderStatus.strip()
    proposed_reason = extraction.tenderStatusReason.strip()
    note_parts = [part.strip() for part in (extraction.tenderStatusNote, coverage_note) if part.strip()]
    warnings: list[str] = []

    if _indivisible_lot_not_covered(extraction, product_check or {}):
        return HoldingDecision(
            status="Отказано",
            reason=INDIVISIBLE_LOT_REASON,
            note=" ".join(dict.fromkeys(note_parts)),
            warnings=warnings,
        )

    if proposed_reason == DELIVERY_DEADLINE_REASON and not _delivery_deadline_exceeded(
        extraction,
        received_at,
    ):
        warnings.append("LLM delivery-deadline rejection was ignored: deterministic 18-month rule is not confirmed.")
        proposed_status, proposed_reason = "Согласовано", ""

    if proposed_status == "Отказано" and proposed_reason in STATUS_REASONS:
        if extraction.statusReasonEvidence.strip():
            note_parts.append(f"Основание: {extraction.statusReasonEvidence.strip()}")
        return HoldingDecision(
            status="Отказано",
            reason=proposed_reason,
            note=" ".join(dict.fromkeys(note_parts)),
            warnings=warnings,
        )

    if proposed_status == "Отказано":
        warnings.append("LLM rejection was ignored: reason is absent from the holding-company directory.")
    return HoldingDecision(
        status="Согласовано",
        reason="",
        note=" ".join(dict.fromkeys(note_parts)),
        warnings=warnings,
    )
