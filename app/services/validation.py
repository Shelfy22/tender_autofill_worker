from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from app.models import ExtractedFieldsResponse, NormalizedJob, ParsedDocument
from app.services.normalization import deduplicate_strings


ALLOWED_FIELDS = {
    "dateCreated", "submissionDeadlineDate", "submissionDeadlineTime", "tenderUrlSource",
    "federalLaw", "stateDefenseOrder", "tenderStatus", "tenderStatusNote", "tenderStatusReason",
    "tenderGroup", "initialPrice", "finalPrice", "resultDate", "contractDate", "deliveryType",
    "deliveryBatchDays", "deliveryDays", "deliveryDate", "paymentDelayDays", "lotDivisible",
    "deliveryNote", "counterpartyCode", "counterpartyName", "counterpartyInn", "counterpartyKpp",
    "counterpartyCkg", "counterpartyPotential", "deal", "contract", "counterpartyNote",
    "customerContactPerson", "op", "tenderSubmittedDate", "tenderWonDate", "applicationSecurity",
    "contractSecurity", "warrantySecurity", "warrantyMonths", "nationalRegime", "specialAccount",
    "counterparty", "inn", "kpp",
}


_DELIVERY_DEADLINE_EVIDENCE_PATTERN = re.compile(
    r"(?:срок(?:и|ом)?\s+(?:поставк[иа]|доставк[иа])|"
    r"дат[аы]\s+(?:поставк[и]|доставк[и])|"
    r"поставк[а-яё]*\s+(?:товар[а-яё]*\s+)?(?:осуществля[а-яё]*\s+)?"
    r"(?:до|не\s+позднее|в\s+течение|с|по)|"
    r"доставк[а-яё]*\s+(?:товар[а-яё]*\s+)?(?:осуществля[а-яё]*\s+)?"
    r"(?:до|не\s+позднее|в\s+течение|с|по)|"
    r"(?:поставить|поставляет|доставить|доставляет)[\s\S]{0,100}?"
    r"(?:до|не\s+позднее|в\s+течение))",
    re.IGNORECASE,
)


def _has_explicit_delivery_deadline_evidence(evidence: Any) -> bool:
    text = re.sub(r"\s+", " ", str(evidence or "")).strip()
    return bool(text and _DELIVERY_DEADLINE_EVIDENCE_PATTERN.search(text))


def _normalize_date(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        return f"{match.group(3)}-{match.group(2).zfill(2)}-{match.group(1).zfill(2)}"
    return text


def _set(
    fields: dict[str, Any], meta: dict[str, Any], key: str, value: Any,
    source: str, confidence: str, evidence: str,
) -> None:
    if value is None or str(value).strip() == "":
        return
    fields[key] = value
    meta[key] = {"source": source, "confidence": confidence, "evidence": evidence[:900]}


def validate_fields(
    job: NormalizedJob,
    extracted: ExtractedFieldsResponse | None,
    deterministic_text: str,
    documents: list[ParsedDocument],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    fields: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    warnings: list[str] = []
    if extracted:
        warnings.extend(extracted.warnings)
        for key, raw in extracted.fields.items():
            if key not in ALLOWED_FIELDS:
                continue
            if hasattr(raw, "value"):
                value = raw.value
                source, confidence, evidence = raw.source or "AI Agent", raw.confidence, raw.evidence or ""
            else:
                value, source, confidence, evidence = raw, "AI Agent", "low", ""
            if value is not None and str(value).strip() != "":
                fields[key] = value
                meta[key] = {"source": source, "confidence": confidence, "evidence": evidence}
    else:
        warnings.append("LLM extraction не выполнен; применены deterministic fallbacks.")

    if not fields.get("counterpartyName") and fields.get("counterparty"):
        fields["counterpartyName"] = fields["counterparty"]
    if not fields.get("counterpartyInn") and fields.get("inn"):
        fields["counterpartyInn"] = fields["inn"]
    if not fields.get("counterpartyKpp") and fields.get("kpp"):
        fields["counterpartyKpp"] = fields["kpp"]

    _set(fields, meta, "tenderUrlSource", fields.get("tenderUrlSource") or job.tender_url, "Input tenderUrl", "high", job.tender_url or "")
    _set(fields, meta, "dateCreated", fields.get("dateCreated") or date.today().isoformat(), "Workflow", "medium", "Дата запуска workflow")
    _set(fields, meta, "tenderStatus", fields.get("tenderStatus") or "Загружен Seldon", "Default", "medium", "Исходный статус")
    _set(fields, meta, "finalPrice", fields.get("finalPrice") or "0", "Default", "low", "Конечная цена не найдена")

    purchase = job.seldon_purchase
    if not fields.get("initialPrice"):
        value = purchase.get("purchasePrice") or purchase.get("initialPrice") or purchase.get("price")
        if value is not None and str(value).strip() != "":
            _set(fields, meta, "initialPrice", value, "Seldon structured data", "high", str(value))

    organizer = purchase.get("organizer") if isinstance(purchase.get("organizer"), dict) else {}
    customer_name = organizer.get("name") or job.report_fields.get("Название заказчика") or job.report_fields.get("Организатор")
    customer_inn = organizer.get("inn") or job.report_fields.get("ИНН заказчика") or job.report_fields.get("ИНН заказчика/организатора")
    customer_kpp = organizer.get("kpp") or job.report_fields.get("КПП заказчика") or job.report_fields.get("КПП заказчика/организатора")
    _set(fields, meta, "counterpartyName", fields.get("counterpartyName") or customer_name, "Seldon/Daily", "medium", str(customer_name or ""))
    _set(fields, meta, "counterpartyInn", fields.get("counterpartyInn") or customer_inn, "Seldon/Daily", "medium", str(customer_inn or ""))
    _set(fields, meta, "counterpartyKpp", fields.get("counterpartyKpp") or customer_kpp, "Seldon/Daily", "medium", str(customer_kpp or ""))

    report_lot_divisible = job.report_fields.get("Лот делимый")
    if report_lot_divisible is None:
        report_lot_divisible = job.report_fields.get("lotDivisible")
    report_lot_text = str(report_lot_divisible or "").strip().lower().replace("ё", "е")
    if report_lot_text in {"да", "yes", "true", "1", "делимый", "делим"}:
        _set(
            fields,
            meta,
            "lotDivisible",
            "yes",
            "Daily / колонка «Лот делимый»",
            "high",
            f"Лот делимый: {report_lot_divisible}",
        )
    elif report_lot_text in {"нет", "no", "false", "0", "неделимый", "неделим"}:
        _set(
            fields,
            meta,
            "lotDivisible",
            "no",
            "Daily / колонка «Лот делимый»",
            "high",
            f"Лот делимый: {report_lot_divisible}",
        )

    text_lower = deterministic_text.lower().replace("ё", "е")
    if not fields.get("federalLaw"):
        law = "223" if "223-фз" in text_lower or job.report_id == 1 else "44" if "44-фз" in text_lower or job.report_id == 2 else "commercial" if job.report_id == 3 else None
        _set(fields, meta, "federalLaw", law, "Seldon/документы", "high", f"reportId={job.report_id}")
    goz = any(token in text_lower for token in ("гособоронзаказ", "275-фз", "отдельный банковский счет", "казначейское сопровождение"))
    if not fields.get("stateDefenseOrder"):
        _set(fields, meta, "stateDefenseOrder", "yes" if goz else "no", "Проверка признаков ГОЗ", "high" if goz else "medium", "Признаки ГОЗ найдены" if goz else "Признаки ГОЗ не найдены")
    if not fields.get("specialAccount"):
        special = goz or "спецсчет" in text_lower
        _set(fields, meta, "specialAccount", "yes" if special else "no", "Проверка спецсчёта", "high" if special else "medium", "Признаки спецсчёта найдены" if special else "Признаки не найдены")
    if not fields.get("nationalRegime"):
        if "1875" in text_lower or "национальный режим" in text_lower:
            regime = "preference" if "преимущество" in text_lower else "ban" if "запрет" in text_lower else "restriction"
        else:
            regime = "none"
        _set(fields, meta, "nationalRegime", regime, "Fallback validation", "medium", "Проверка национального режима")

    if not fields.get("paymentDelayDays"):
        match = re.search(r"оплат[а-я\s]{0,80}в\s+течение\s+(\d+)\s*(?:рабоч|календарн)?\s*д", deterministic_text, re.I)
        if match:
            _set(fields, meta, "paymentDelayDays", int(match.group(1)), "Fallback validation", "high", match.group(0))
    if re.search(r"по\s+заявк|партиями|график(?:у|а)?\s+поставки", deterministic_text, re.I):
        _set(fields, meta, "deliveryType", "by_requests", "Fallback validation", "high", "Поставка партиями/по заявкам")
        if not fields.get("deliveryNote"):
            _set(fields, meta, "deliveryNote", "Поставка партиями/по заявкам", "Fallback validation", "high", "Поставка партиями/по заявкам")

    lot_text = f"{fields.get('lotDivisible', '')} {meta.get('lotDivisible', {}).get('evidence', '')}"
    direct_lot = re.search(r"лот\s+неделим|делени[ея]\s+лота\s+не\s+допуска|лот\s+делим|подач[а-я]+\s+на\s+част[ьи]\s+лота", lot_text, re.I)
    trusted_lot_column = (
        meta.get("lotDivisible", {}).get("source")
        == "Daily / колонка «Лот делимый»"
    )
    if fields.get("lotDivisible") and not direct_lot and not trusted_lot_column:
        fields.pop("lotDivisible", None)
        meta.pop("lotDivisible", None)
        warnings.append("Лот делимый не заполнен: нет прямого evidence.")

    for key in ("deliveryDate", "deliveryDays"):
        if not fields.get(key):
            continue
        evidence = str(meta.get(key, {}).get("evidence") or "").strip()
        if _has_explicit_delivery_deadline_evidence(evidence):
            continue
        fields.pop(key, None)
        meta.pop(key, None)
        evidence_preview = re.sub(r"\s+", " ", evidence)[:300]
        warnings.append(
            f"{key} отброшен: в evidence нет прямой связи со сроком поставки. "
            f"Фрагмент: {evidence_preview}"
        )

    for key in ("dateCreated", "submissionDeadlineDate", "resultDate", "contractDate", "deliveryDate", "tenderSubmittedDate", "tenderWonDate"):
        if fields.get(key):
            fields[key] = _normalize_date(fields[key])
    if fields.get("submissionDeadlineTime"):
        match = re.search(r"(\d{1,2})[:.](\d{2})", str(fields["submissionDeadlineTime"]))
        if match:
            fields["submissionDeadlineTime"] = f"{match.group(1).zfill(2)}:{match.group(2)}"
    for key, length in (("counterpartyInn", (10, 12)), ("counterpartyKpp", (9,))):
        if fields.get(key):
            digits = re.sub(r"\D", "", str(fields[key]))
            fields[key] = digits
            if len(digits) not in length:
                warnings.append(f"{key} выглядит некорректно: {digits}")
                meta.setdefault(key, {})["confidence"] = "low"

    fields["toCode"] = job.to_code
    meta["toCode"] = {"source": "Seldon filters / Код ТО", "confidence": "high", "evidence": f"Код ТО: {job.to_code}"}
    fields["legalEntity"] = None
    fields["counterparty"] = fields.get("counterpartyName")
    fields["inn"] = fields.get("counterpartyInn")
    fields["kpp"] = fields.get("counterpartyKpp")
    meta["counterparty"] = meta.get("counterpartyName")
    meta["inn"] = meta.get("counterpartyInn")
    meta["kpp"] = meta.get("counterpartyKpp")
    return fields, meta, deduplicate_strings(warnings)
