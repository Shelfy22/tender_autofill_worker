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

_SECURITY_FIELD_PATTERNS = {
    "applicationSecurity": re.compile(
        r"(?:размер\s+)?обеспечени[ея]\s+заявк[ие]\s*(?:составляет|[-:–—])?\s*([^\n;]{1,180})",
        re.IGNORECASE,
    ),
    "contractSecurity": re.compile(
        r"(?:размер\s+)?обеспечени[ея]\s+(?:исполнени[ея]\s+)?(?:контракт[а-яё]*|договор[а-яё]*)\s*(?:составляет|[-:–—])?\s*([^\n;]{1,180})",
        re.IGNORECASE,
    ),
    "warrantySecurity": re.compile(
        r"(?:размер\s+)?обеспечени[ея]\s+гарантийн[а-яё\s]{0,50}(?:обязательств[а-яё]*)?\s*(?:составляет|[-:–—])?\s*([^\n;]{1,180})",
        re.IGNORECASE,
    ),
}
_WARRANTY_MONTHS_PATTERN = re.compile(
    r"гарантийн[а-яё]*\s+срок[^\n;]{0,100}?(\d{1,3})\s*(?:месяц[а-яё]*|мес\.?)(?:\b|\s)",
    re.IGNORECASE,
)
_DELIVERY_DAYS_PATTERN = re.compile(
    r"(?:срок[а-яё\s]{0,35}(?:поставк[аи]|доставк[аи])|"
    r"(?:поставк[а-яё]*|доставк[а-яё]*)[\s\S]{0,80}?(?:в\s+течение))"
    r"[\s\S]{0,80}?(\d{1,4})\s*(?:рабоч(?:их|ие)?|календарн(?:ых|ые)?)?\s*"
    r"(?:дн(?:ей|я)?|сут(?:ок|ки)?)\b",
    re.IGNORECASE,
)
_DELIVERY_DATE_PATTERN = re.compile(
    r"(?:срок[а-яё\s]{0,35}(?:поставк[аи]|доставк[аи])|"
    r"(?:поставк[а-яё]*|доставк[а-яё]*)[\s\S]{0,80}?(?:до|не\s+позднее|по))"
    r"[\s\S]{0,80}?(\d{1,2}\.\d{1,2}\.\d{4})",
    re.IGNORECASE,
)
_PAYMENT_DELAY_PATTERN = re.compile(
    r"(?:отсрочк[а-яё]*\s+(?:платеж[а-яё]*|оплат[а-яё]*)|"
    r"оплат[а-яё\s]{0,100}?в\s+течение)"
    r"[\s\S]{0,80}?(\d{1,4})\s*(?:рабоч(?:их|ие)?|календарн(?:ых|ые)?)?\s*д",
    re.IGNORECASE,
)
_SECURITY_VALUE_PATTERN = re.compile(r"\d")
_NON_NUMERIC_SECURITY_VALUE_PATTERN = re.compile(
    r"^(?:не\s+(?:требуется|установлен[ао]?|предусмотрен[ао]?)|отсутствует|без\s+обеспечения)\b",
    re.IGNORECASE,
)


def _explicit_security_fields(text: str) -> dict[str, tuple[str, str]]:
    """Extract direct security and warranty terms if a document unit missed them."""
    result: dict[str, tuple[str, str]] = {}
    normalized = re.sub(r"\s+", " ", re.sub(r"[\r\n]+", "; ", str(text or ""))).strip()
    for field, pattern in _SECURITY_FIELD_PATTERNS.items():
        match = pattern.search(normalized)
        if match is None:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;–—-")
        if not value:
            continue
        if (
            _NON_NUMERIC_SECURITY_VALUE_PATTERN.search(value)
            or not _SECURITY_VALUE_PATTERN.search(value)
        ):
            continue
        result[field] = (value[:300], match.group(0)[:500])

    warranty_match = _WARRANTY_MONTHS_PATTERN.search(normalized)
    if warranty_match is not None:
        result["warrantyMonths"] = (warranty_match.group(1), warranty_match.group(0)[:500])
    return result


def _explicit_commercial_fields(text: str) -> dict[str, tuple[Any, str]]:
    """Capture only direct delivery and payment facts that survive LLM chunking."""
    result: dict[str, tuple[Any, str]] = {}
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    patterns: tuple[tuple[str, re.Pattern[str], Any], ...] = (
        ("deliveryDays", _DELIVERY_DAYS_PATTERN, lambda match: int(match.group(1))),
        ("deliveryDate", _DELIVERY_DATE_PATTERN, lambda match: _normalize_date(match.group(1))),
        ("paymentDelayDays", _PAYMENT_DELAY_PATTERN, lambda match: int(match.group(1))),
    )
    for field, pattern, value_factory in patterns:
        match = pattern.search(normalized)
        if match is not None:
            result[field] = (value_factory(match), match.group(0)[:500])
    return result


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


def _structured_submission_deadline(job: NormalizedJob) -> tuple[str | None, str | None, str]:
    """Read the application deadline from Seldon/Daily, never from document AI."""
    purchase = job.seldon_purchase if isinstance(job.seldon_purchase, dict) else {}
    values = (
        purchase.get("endDate"),
        purchase.get("dateEnd"),
        purchase.get("submissionDeadline"),
        purchase.get("submissionDeadlineDate"),
        purchase.get("applicationEndDate"),
        job.report_fields.get("Дата окончания приёма заявок"),
        job.report_fields.get("Дата окончания приема заявок"),
    )
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        if isinstance(value, datetime):
            return value.date().isoformat(), value.strftime("%H:%M:%S"), str(value)
        if isinstance(value, date):
            return value.isoformat(), None, str(value)

        text = str(value).strip()
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.date().isoformat(), parsed.strftime("%H:%M:%S"), text

        match = re.search(
            r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+|T)?(\d{1,2}):(\d{2})(?::(\d{2}))?",
            text,
        )
        if match:
            day, month, year, hour, minute, second = match.groups()
            return (
                f"{year}-{month.zfill(2)}-{day.zfill(2)}",
                f"{hour.zfill(2)}:{minute}:{second or '00'}",
                text,
            )
        match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if match:
            day, month, year = match.groups()
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}", None, text
    return None, None, ""


def _structured_result_date(job: NormalizedJob) -> tuple[str | None, str]:
    """Take the result date only from Seldon/Daily, not document LLM output."""
    purchase = job.seldon_purchase if isinstance(job.seldon_purchase, dict) else {}
    values = (
        purchase.get("resultDate"),
        purchase.get("dateResult"),
        purchase.get("summarizingDate"),
        purchase.get("dateSummingUp"),
        purchase.get("protocolDate"),
        purchase.get("protocolPublishDate"),
        job.report_fields.get("Дата подведения итогов"),
    )
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        if isinstance(value, datetime):
            return value.date().isoformat(), str(value)
        if isinstance(value, date):
            return value.isoformat(), str(value)
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat(), text
        except ValueError:
            normalized = _normalize_date(text)
            if isinstance(normalized, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
                return normalized, text
    return None, ""


def _normalize_delivery_type(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").casefold().replace("ё", "е")).strip()
    if not text:
        return None
    if text in {"by_requests", "single_date", "during_period"}:
        return text
    if re.search(r"по\s+заявк|парт|график|разнаряд", text):
        return "by_requests"
    if re.search(r"к\s+дат|единовременно\s+(?:до|на\s+дат)", text):
        return "single_date"
    if re.search(r"в\s+течение|срок", text):
        return "during_period"
    return None


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

    # The autofill columns must contain only an explicitly stated amount or
    # percentage. Do not turn an absent requirement into "not required".
    for key in ("applicationSecurity", "contractSecurity", "warrantySecurity"):
        value = str(fields.get(key) or "").strip()
        if value and (
            _NON_NUMERIC_SECURITY_VALUE_PATTERN.search(value)
            or not _SECURITY_VALUE_PATTERN.search(value)
        ):
            fields.pop(key, None)
            meta.pop(key, None)

    for key, (value, evidence) in _explicit_security_fields(deterministic_text).items():
        if not fields.get(key):
            _set(
                fields,
                meta,
                key,
                value,
                "Документы / прямое условие обеспечения или гарантии",
                "high",
                evidence,
            )

    for key, (value, evidence) in _explicit_commercial_fields(deterministic_text).items():
        if not fields.get(key):
            _set(
                fields,
                meta,
                key,
                value,
                "Документы / прямое условие поставки или оплаты",
                "high",
                evidence,
            )

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
    # These dates have a deterministic Seldon/Daily source and must not be
    # guessed by an LLM from a contract or a document template.
    fields.pop("resultDate", None)
    meta.pop("resultDate", None)
    fields.pop("contractDate", None)
    meta.pop("contractDate", None)
    deadline_date, deadline_time, deadline_evidence = _structured_submission_deadline(job)
    if deadline_date:
        _set(
            fields,
            meta,
            "submissionDeadlineDate",
            deadline_date,
            "Seldon/Daily / дата окончания подачи",
            "high",
            deadline_evidence,
        )
    result_date, result_evidence = _structured_result_date(job)
    if result_date:
        _set(
            fields,
            meta,
            "resultDate",
            result_date,
            "Seldon/Daily / дата подведения итогов",
            "high",
            result_evidence,
        )
        contract_date = date.fromisoformat(result_date) + timedelta(days=14)
        _set(
            fields,
            meta,
            "contractDate",
            contract_date.isoformat(),
            "Расчёт / дата подведения итогов + 14 календарных дней",
            "high",
            f"{result_date} + 14 календарных дней",
        )
    if deadline_time:
        _set(
            fields,
            meta,
            "submissionDeadlineTime",
            deadline_time,
            "Seldon/Daily / дата окончания подачи",
            "high",
            deadline_evidence,
        )
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
    delivery_type = _normalize_delivery_type(fields.get("deliveryType"))
    if delivery_type is None:
        if re.search(r"по\s+заявк|партиями|график(?:у|а)?\s+поставки|разнаряд", deterministic_text, re.I):
            delivery_type = "by_requests"
        elif fields.get("deliveryDate"):
            delivery_type = "single_date"
        elif fields.get("deliveryDays"):
            delivery_type = "during_period"
    if delivery_type:
        _set(
            fields,
            meta,
            "deliveryType",
            delivery_type,
            "Документы / условия отгрузки",
            "high" if "deliveryType" not in meta else meta["deliveryType"].get("confidence", "medium"),
            str(meta.get("deliveryType", {}).get("evidence") or "Условие поставки"),
        )

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
