from __future__ import annotations

import re
from typing import Any, Iterable

from app.models import JobClaim, NormalizedJob


REPORT_HEADERS: dict[int, list[str]] = {
    1: [
        "SeldonID", "0", "Осталось дней", "Статус закупки", "Электронная площадка",
        "Способ закупки", "Наименование", "Организатор", "Начальная цена", "Конечная цена",
        "Валюта", "Дата начала приёма заявок", "Дата окончания приёма заявок", "Дата изменения",
        "№ извещения", "Ссылка на тендер", "Регион заказчика/организатора",
        "ИНН заказчика/организатора", "Дата проведения аукциона", "Размер обеспечения заявки",
        "Код ТО", "Код ФЗ",
    ],
    2: [
        "SeldonID", "Степень завершения", "Осталось дней", "Статус закупки", "№ извещения",
        "Способ закупки", "Наименование", "Название заказчика", "Начальная цена", "Конечная цена",
        "Валюта", "Дата начала приёма заявок", "Дата окончания приёма заявок", "Дата изменения",
        "Ссылка на тендер", "Регион заказчика/организатора", "ИНН заказчика",
        "Размер обеспечения заявки", "Дата проведения аукциона", "Размер обеспечения контракта",
        "Торговая площадка", "Код ТО", "Код ФЗ",
    ],
    3: [
        "SeldonID", "0", "Степень завершения", "Осталось дней", "Статус закупки",
        "Способ закупки", "Источник", "Наименование", "Название заказчика", "Начальная цена",
        "Валюта", "Дата начала приёма заявок", "Дата окончания приёма заявок", "Дата изменения",
        "Ссылка на тендер", "Регион заказчика/организатора", "ИНН заказчика", "Код ТО", "Код ФЗ",
    ],
}


def first_defined(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_number(
    value: Any, *, positive: bool = False, allow_negative: bool = False
) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if (number < 0 and not allow_negative) or (positive and number <= 0):
            return None
        return number
    text = str(value or "").replace("\xa0", " ").strip()
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    number = float(match.group(0).replace(",", "."))
    if (number < 0 and not allow_negative) or (positive and number <= 0):
        return None
    return number


def resolve_report_id(payload: dict[str, Any], fallback: int | None = None) -> int:
    explicit = first_defined(
        payload.get("reportId"), payload.get("report_id"), payload.get("ReportId"), fallback
    )
    try:
        report_id = int(explicit)
    except (TypeError, ValueError):
        report_id = 0
    if report_id in {1, 2, 3, 4, 5}:
        return report_id

    kind = str(first_defined(payload.get("purchaseType"), payload.get("tenderType"), ""))
    kind = re.sub(r"\s+", " ", kind.lower().replace("ё", "е").replace("—", "-").strip())
    aliases = {
        "1": 1, "223": 1, "223-фз": 1, "223 фз": 1,
        "2": 2, "44": 2, "94": 2, "44-фз": 2, "94-фз": 2, "44/94-фз": 2,
        "3": 3, "коммерческие закупки": 3, "коммерческие": 3, "commercial": 3, "kom": 3,
        "4": 4, "контракты": 4, "контракт": 4, "contracts": 4,
        "5": 5, "международные закупки": 5, "международные": 5, "international": 5,
    }
    if kind in aliases:
        return aliases[kind]
    law = str(first_defined(payload.get("lawCode"), payload.get("Код ФЗ"), "")).lower()
    if "223" in law:
        return 1
    if "44" in law or "94" in law:
        return 2
    if "commercial" in law or "ком" in law:
        return 3
    raise ValueError("Не удалось определить reportId по типу закупки")


def build_flat_purchase(payload: dict[str, Any], report_id: int) -> dict[str, Any]:
    report_fields = payload.get("reportFields") or {}
    source = {**report_fields, **payload}
    organizer = {
        "name": first_defined(source.get("Организатор"), source.get("Название заказчика")),
        "inn": first_defined(source.get("ИНН заказчика/организатора"), source.get("ИНН заказчика")),
        "kpp": first_defined(source.get("КПП заказчика/организатора"), source.get("КПП заказчика")),
        "region": source.get("Регион заказчика/организатора"),
    }
    currency = source.get("Валюта")
    first_lot = {
        "lotNumber": 1,
        "subject": source.get("Наименование"),
        "price": first_defined(source.get("Начальная цена"), source.get("initialPrice")),
        "currency": {"code": str(currency), "name": str(currency)} if currency else None,
        "productsList": source.get("productsList") or source.get("products") or [],
        "customersList": [{"organization": organizer}] if any(organizer.values()) else [],
    }
    return {
        "seldonId": first_defined(source.get("seldonId"), source.get("SeldonID"), source.get("ID")),
        "reportId": report_id,
        "notificationNumber": first_defined(source.get("etpId"), source.get("№ извещения")),
        "purchaseLink": first_defined(source.get("tenderUrl"), source.get("Ссылка на тендер")),
        "publishDate": first_defined(source.get("publishDate"), source.get("Дата изменения")),
        "subject": source.get("Наименование"),
        "purchasePrice": first_defined(source.get("purchasePrice"), source.get("Начальная цена")),
        "currency": {"code": str(currency), "name": str(currency)} if currency else None,
        "epName": first_defined(source.get("Электронная площадка"), source.get("Торговая площадка")),
        "startDate": source.get("Дата начала приёма заявок"),
        "endDate": source.get("Дата окончания приёма заявок"),
        "holdingDate": source.get("Дата проведения аукциона"),
        "changeDate": source.get("Дата изменения"),
        "organizer": organizer,
        "lotsList": source.get("lotsList") or [first_lot],
        "toCode": first_defined(source.get("toCode"), source.get("Код ТО")),
        "lawCode": first_defined(source.get("lawCode"), source.get("Код ФЗ")),
    }


def normalize_job_payload(claim: JobClaim) -> NormalizedJob:
    payload = dict(claim.input_json or {})
    report_id = resolve_report_id(payload, claim.report_id)
    if report_id == 4:
        raise ValueError(
            "Тип «Контракты» требует Contracts/Get + ContractsDocuments/Get; active workflow uses Purchases"
        )

    report_fields = dict(claim.report_fields or {})
    supplied_fields = payload.get("reportFields")
    if isinstance(supplied_fields, dict):
        report_fields.update(supplied_fields)
    for header in REPORT_HEADERS.get(report_id, []):
        report_fields.setdefault(header, payload.get(header, ""))

    raw_seldon_id = first_defined(payload.get("seldonId"), payload.get("SeldonID"), claim.seldon_id)
    raw_etp_id = first_defined(payload.get("etpId"), payload.get("№ извещения"), claim.etp_id)
    seldon_id = string_or_none(raw_seldon_id)
    etp_id = None if seldon_id else string_or_none(raw_etp_id)
    if bool(seldon_id) == bool(etp_id):
        raise ValueError("Должен быть заполнен ровно один из seldonId/etpId")

    to_code = string_or_none(first_defined(payload.get("toCode"), report_fields.get("Код ТО")))
    law_code = string_or_none(
        first_defined(
            payload.get("lawCode"), report_fields.get("Код ФЗ"),
            "223" if report_id == 1 else "44" if report_id == 2 else "Commercial" if report_id == 3 else None,
        )
    )
    if to_code:
        report_fields["Код ТО"] = to_code
    if law_code:
        report_fields["Код ФЗ"] = law_code
    remaining_days = parse_number(
        first_defined(payload.get("remainingDays"), report_fields.get("Осталось дней")),
        allow_negative=True,
    )
    if remaining_days is not None:
        report_fields["Осталось дней"] = str(int(remaining_days) if remaining_days.is_integer() else remaining_days)

    purchase = first_defined(
        payload.get("seldonPurchase"), payload.get("purchase"), payload.get("rawPurchase")
    )
    if not isinstance(purchase, dict):
        purchase = build_flat_purchase(payload, report_id)
    purchase = dict(purchase)
    purchase.setdefault("reportId", report_id)
    purchase.setdefault("seldonId", seldon_id)
    purchase.setdefault("notificationNumber", etp_id)
    purchase.setdefault("toCode", to_code)
    purchase.setdefault("lawCode", law_code)

    return NormalizedJob(
        job_record_key=claim.record_key,
        batch_id=claim.batch_id,
        batch_date=string_or_none(payload.get("batchDate")),
        row_number=int(payload["rowNumber"]) if str(payload.get("rowNumber", "")).isdigit() else None,
        report_id=report_id,
        purchase_type=string_or_none(payload.get("purchaseType")),
        seldon_id=seldon_id,
        etp_id=etp_id,
        to_code=to_code,
        law_code=law_code,
        section_name=string_or_none(payload.get("sectionName")),
        filter_name=string_or_none(payload.get("filterName")),
        remaining_days=remaining_days,
        report_fields=report_fields,
        seldon_purchase=purchase,
        tender_url=string_or_none(
            first_defined(
                payload.get("tenderUrl"),
                payload.get("tender_url"),
                purchase.get("urlSource"),
                purchase.get("purchaseUrl"),
                purchase.get("tenderUrl"),
                purchase.get("purchaseLink"),
                purchase.get("url"),
                purchase.get("link"),
                purchase.get("sourceUrl"),
                purchase.get("href"),
            )
        ),
        source_file=string_or_none(payload.get("sourceFile")),
        seldon_token=string_or_none(
            first_defined(payload.get("seldonToken"), payload.get("token"), payload.get("apiToken"))
        ),
        attempt=claim.attempt,
    )


def deduplicate_strings(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
