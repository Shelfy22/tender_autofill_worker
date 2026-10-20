from __future__ import annotations

import re
from math import ceil
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
_PAYMENT_TERM_CONTEXT_PATTERN = re.compile(
    r"(?:\u043e\u0442\u0441\u0440\u043e\u0447\w*\s+(?:\u043f\u043b\u0430\u0442\u0435\u0436\w*|\u043e\u043f\u043b\u0430\u0442\w*)|"
    r"(?:\u043e\u043f\u043b\u0430\u0442\w*|\u043f\u043b\u0430\u0442\u0451\u0436\w*|\u0440\u0430\u0441\u0447\u0451\u0442\w*)(?:(?![.;\n]).){0,360}?"
    r"(?:\u0432\s+\u0442\u0435\u0447\u0435\u043d\u0438\u0435|\u0432\s+\u0441\u0440\u043e\u043a\s+\u043d\u0435\s+\u0431\u043e\u043b\u0435\u0435|\u043d\u0435\s+\u0431\u043e\u043b\u0435\u0435|\u043d\u0435\s+\u043f\u043e\u0437\u0434\u043d\u0435\u0435|\u043f\u043e\u0441\u043b\u0435))"
    r"[^.\n]{0,180}",
    re.IGNORECASE,
)
_PAYMENT_DELAY_VALUE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:(?:-|\u2013|\u2014|\u0434\u043e)\s*(\d{1,4}))?\s*"
    r"(?:\([^)]{1,80}\)\s*)?"
    r"(\u0440\u0430\u0431\u043e\u0447\w*|\u043a\u0430\u043b\u0435\u043d\u0434\u0430\u0440\u043d\w*|\u0431\u0430\u043d\u043a\u043e\u0432\u0441\u043a\w*)?\s*"
    r"\u0434\u043d(?:\u0435\u0439|\u044f)?\b",
    re.IGNORECASE,
)
_SME_PAYMENT_PATTERN = re.compile(
    r"\b(?:мсп|субъект\w*\s+малого\s+и\s+среднего\s+предпринимательств\w*|"
    r"самозанят\w*)\b",
    re.IGNORECASE,
)
_SME_ONLY_PROCUREMENT_PATTERN = re.compile(
    r"(?:закупк\w*|участи\w*)[^.\n]{0,120}?"
    r"(?:только|исключительно)[^.\n]{0,160}?"
    r"(?:смсп|мсп|субъект\w*\s+малого\s+и\s+среднего\s+предпринимательств\w*)|"
    r"(?:закупк\w*[^.\n]{0,80}?(?:провод\w*|осуществля\w*)[^.\n]{0,100}?\sсреди\s+)"
    r"(?:смсп|мсп|субъект\w*\s+малого\s+и\s+среднего\s+предпринимательств\w*)|"
    r"участник\w*\s+закупк\w*[^.\n]{0,100}?могут\s+быть\s+только[^.\n]{0,120}?"
    r"(?:смсп|мсп|субъект\w*\s+малого\s+и\s+среднего\s+предпринимательств\w*)",
    re.IGNORECASE,
)
_CONTRACT_EXPIRY_DATE_PATTERN = re.compile(
    r"(?:\u0441\u0440\u043e\u043a\s+\u0434\u0435\u0439\u0441\u0442\u0432\w*\s+\u0434\u043e\u0433\u043e\u0432\u043e\u0440\w*|"
    r"\u0434\u043e\u0433\u043e\u0432\u043e\u0440\w*[^.\n]{0,120}?\u0434\u0435\u0439\u0441\u0442\u0432\w*)"
    r"[^.\n]{0,180}?(?:\u0434\u043e|\u043f\u043e)\s*(\d{1,2}\.\d{1,2}\.\d{4})",
    re.IGNORECASE,
)
_NATIONAL_REGIME_CONTEXT_PATTERN = re.compile(
    r"(?:\u043d\u0430\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\w*\s+\u0440\u0435\u0436\u0438\u043c|"
    r"\u043f\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\w*\s*№?\s*1875|"
    r"\u043f\u043f\s*№?\s*1875)",
    re.IGNORECASE,
)
_NATIONAL_REGIME_PATTERNS = (
    ("ban", re.compile(r"\u0437\u0430\u043f\u0440\u0435\u0442\w*", re.IGNORECASE)),
    ("restriction", re.compile(r"\u043e\u0433\u0440\u0430\u043d\u0438\u0447\w*", re.IGNORECASE)),
    ("preference", re.compile(r"\u043f\u0440\u0435\u0438\u043c\u0443\u0449\u0435\u0441\u0442\u0432\w*", re.IGNORECASE)),
)
_NATIONAL_REGIME_TABLE_HEADER_PATTERN = re.compile(
    r"\u0432\u0438\u0434\s+\u0437\u0430\u0449\u0438\u0442\u043d\w*\s+\u043c\u0435\u0440\w*",
    re.IGNORECASE,
)
_NATIONAL_REGIME_PRODUCT_COLUMN_PATTERN = re.compile(
    r"(?:\u0432\u0438\u0434\s+\u0437\u0430\u0449\u0438\u0442\u043d\w*\s+\u043c\u0435\u0440\w*|"
    r"\u043d\u0430\u0446\u0438\u043e\u043d\u0430\u043b\u044c\u043d\w*\s+\u0440\u0435\u0436\u0438\u043c)",
    re.IGNORECASE,
)
_PRODUCT_TABLE_HEADER_PATTERN = re.compile(
    r"(?:\u043d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\w*\s+(?:\u043f\u043e\u0437\u0438\u0446\u0438\w*|\u0442\u043e\u0432\u0430\u0440\w*)|"
    r"\u043d\u043e\u043c\u0435\u043d\u043a\u043b\u0430\u0442\u0443\u0440\w*)",
    re.IGNORECASE,
)
_NATIONAL_REGIME_APPLICATION_HEADER_PATTERN = re.compile(
    r"(?:применени\w*\s+национальн\w*\s+режим|"
    r"установлен\w*\s+заказчик\w*|заказчик\w*\s+установлен\w*)",
    re.IGNORECASE,
)
_NATIONAL_REGIME_NOT_APPLIED_PATTERN = re.compile(
    r"(?:не\s+(?:установлен\w*|применя\w*)|не\s+применя\w*|"
    r"не\s+установлено|нет)",
    re.IGNORECASE,
)
_NATIONAL_REGIME_APPLIED_PATTERN = re.compile(
    r"(?:\bда\b|\bустановлен\w*|\bприменя\w*)",
    re.IGNORECASE,
)
_NATIONAL_MINIMUM_SHARE_PATTERN = re.compile(
    r"(?:минимальн\w*\s+обязательн\w*\s+дол\w*|"
    r"minimum\s+(?:mandatory\s+)?share)",
    re.IGNORECASE,
)
_GOZ_DIRECT_PATTERN = re.compile(
    r"(?:\b\u0433\u043e\u0437\b|\u0433\u043e\u0441\u043e\u0431\u043e\u0440\u043e\u043d\u0437\u0430\u043a\u0430\u0437|"
    r"\u0433\u043e\u0441\u0443\u0434\u0430\u0440\u0441\u0442\u0432\u0435\u043d\u043d\w*\s+\u043e\u0431\u043e\u0440\u043e\u043d\u043d\w*\s+\u0437\u0430\u043a\u0430\u0437|"
    r"\b275\s*[-\u2011\u2013\u2014]?\s*\u0444\u0437\b)",
    re.IGNORECASE,
)
_SPECIAL_ACCOUNT_TERM_PATTERN = re.compile(
    r"(?:\u0441\u043f\u0435\u0446\s*\u0441\u0447[её]т|"
    r"\u0441\u043f\u0435\u0446\u0438\u0430\u043b\u044c\u043d\w*\s+\u0431\u0430\u043d\u043a\u043e\u0432\u0441\u043a\w*\s+\u0441\u0447[её]т)",
    re.IGNORECASE,
)
_APPLICATION_SECURITY_CONTEXT_PATTERN = re.compile(
    r"\u043e\u0431\u0435\u0441\u043f\u0435\u0447\u0435\u043d\u0438\w*\s+\u0437\u0430\u044f\u0432\u043a\w*",
    re.IGNORECASE,
)
_APPLICATION_SECURITY_ABSENT_PATTERN = re.compile(
    r"\u043e\u0431\u0435\u0441\u043f\u0435\u0447\u0435\u043d\u0438\w*\s+\u0437\u0430\u044f\u0432\u043a\w*[^.\n]{0,160}?"
    r"(?:\u043d\u0435\s+(?:\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\w*|\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f|\u043f\u0440\u0435\u0434\u0443\u0441\u043c\u043e\u0442\u0440\u0435\u043d\w*)|\u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u0435\u0442|0\s*(?:\u0440\u0443\u0431|\u0440\u0443\u0431\u043b))",
    re.IGNORECASE,
)
_SPECIAL_ACCOUNT_REQUIREMENT_PATTERN = re.compile(
    r"(?:\u043e\u0431\u044f\u0437\u0430\u043d\w*|\u0434\u043e\u043b\u0436\u0435\u043d\w*|\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f|\u0432\u043d\u043e\u0441\u0438\u0442\w*|\u043f\u0435\u0440\u0435\u0447\u0438\u0441\u043b\u044f\u0435\u0442\w*)",
    re.IGNORECASE,
)
_RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_RUSSIAN_DATE_PATTERN = re.compile(
    r"[«\"]?\s*(\d{1,2})\s*[»\"]?\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+"
    r"(\d{4})\s*(?:г\.?|года)?",
    re.IGNORECASE,
)
_RESULT_DATE_PATTERN = re.compile(
    r"(?:дата\s+подведения\s+итогов(?:\s+закупки)?|"
    r"подведение\s+итогов(?:\s+закупки)?|"
    r"дата\s+рассмотрения[^.\n]{0,100}?подведения\s+итогов)"
    r"[\s\S]{0,100}?"
    r"(\d{1,2}\.\d{1,2}\.\d{4}|[«\"]?\s*\d{1,2}\s*[»\"]?\s+"
    r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+\d{4}(?:\s*года)?)",
    re.IGNORECASE,
)
_RESULT_STAGE_END_DATE_PATTERN = re.compile(
    r"(?:\u044d\u0442\u0430\u043f\s*[\u00ab\"]?\s*\u043f\u043e\u0434\u0432\u0435\u0434\u0435\u043d\w*\s+\u0438\u0442\u043e\u0433\u043e\u0432[\s\S]{0,1200}?"
    r"\u0434\u0430\u0442\u0430\s+\u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f\s+\u044d\u0442\u0430\u043f\u0430\s*[-:\u2013\u2014]?\s*)"
    r"(\d{1,2}\.\d{1,2}\.\d{4})",
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
    )
    for field, pattern, value_factory in patterns:
        match = pattern.search(normalized)
        if match is not None:
            result[field] = (value_factory(match), match.group(0)[:500])
    return result


def _payment_delay_from_text(
    text: str,
    *,
    prefer_sme_term: bool = False,
) -> tuple[int, str] | None:
    """Return the applicable payment delay in calendar days.

    A separate SME/self-employed term is an exception, not the main term for
    an open procurement.  It wins only when participation is expressly limited
    to SME/self-employed suppliers.  Otherwise retain the established
    conservative rule: choose the longest applicable general term.  The
    resulting column is expressed in calendar days: source terms in calendar
    days stay unchanged; working, banking and unspecified days use
    ``ceil(days * 1.4)``.
    """
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    candidates: list[tuple[int, str]] = []
    sme_candidates: list[tuple[int, str]] = []
    for context_match in _PAYMENT_TERM_CONTEXT_PATTERN.finditer(normalized):
        clause_start = max(
            normalized.rfind(".", 0, context_match.start()),
            normalized.rfind(";", 0, context_match.start()),
            normalized.rfind("\n", 0, context_match.start()),
        ) + 1
        evidence = normalized[clause_start:context_match.end()].strip()
        for value_match in _PAYMENT_DELAY_VALUE_PATTERN.finditer(evidence):
            lower, upper, unit = value_match.groups()
            source_days = max(int(lower), int(upper or lower))
            normalized_days = _normalize_payment_days(source_days, unit)
            if _SME_PAYMENT_PATTERN.search(evidence):
                sme_candidates.append((normalized_days, evidence[:500]))
            else:
                candidates.append((normalized_days, evidence[:500]))
    if prefer_sme_term and sme_candidates:
        return min(sme_candidates, key=lambda candidate: candidate[0])
    if candidates:
        return max(candidates, key=lambda candidate: candidate[0])
    return min(sme_candidates, key=lambda candidate: candidate[0]) if sme_candidates else None


def _procurement_is_sme_only(text: str) -> bool:
    """True only for a tender explicitly restricted to SME/self-employed bidders."""
    return bool(_SME_ONLY_PROCUREMENT_PATTERN.search(str(text or "")))


def _payment_delay_from_field(value: Any, evidence: Any) -> tuple[int, str] | None:
    """Convert a numeric LLM value when the document text was fragmented.

    It is a safety net for documents where an OCR/parser breaks a payment
    sentence into fragments and the full-text expression is unavailable.
    """
    text = re.sub(r"\s+", " ", str(evidence or "")).strip()
    match = re.search(r"\d{1,4}", str(value or ""))
    if match is None:
        return None
    source_days = int(match.group(0))
    unit: str | None = None
    for value_match in _PAYMENT_DELAY_VALUE_PATTERN.finditer(text):
        lower, upper, candidate_unit = value_match.groups()
        if source_days in {int(lower), int(upper or lower)}:
            unit = candidate_unit
            break
    return _normalize_payment_days(source_days, unit), text[:500]


def _normalize_payment_days(source_days: int, unit: str | None) -> int:
    """Normalize a source payment term to calendar days for the output column."""
    if str(unit or "").lower().startswith("календар"):
        return source_days
    # Working, banking and unspecified days are converted by the agreed rule.
    return ceil(source_days * 1.4)


def _contract_expiry_date(text: str) -> tuple[str, str] | None:
    """Find the contract expiry date for the delivery-date fallback."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    match = _CONTRACT_EXPIRY_DATE_PATTERN.search(normalized)
    if match is None:
        return None
    return _normalize_date(match.group(1)), match.group(0)[:500]


def _national_regime_from_text(text: str) -> tuple[str, str] | None:
    """Resolve mixed national-regime tables by the required priority."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not _NATIONAL_REGIME_CONTEXT_PATTERN.search(normalized):
        return None
    for regime, pattern in _NATIONAL_REGIME_PATTERNS:
        match = pattern.search(normalized)
        if match is not None:
            return regime, match.group(0)[:500]
    return None


def _table_regime_value(value: Any) -> str | None:
    normalized = re.sub(r"\s+", " ", str(value or "").casefold().replace("ё", "е")).strip()
    if not normalized:
        return None
    if _NATIONAL_MINIMUM_SHARE_PATTERN.search(normalized):
        return "no"
    for regime, pattern in _NATIONAL_REGIME_PATTERNS:
        # Product columns often contain a full description such as
        # "Запрет иностранных товаров, работ, услуг", not just the one-word
        # measure.  The column header has already established its meaning.
        if pattern.search(normalized):
            return regime
    return None


def _highest_national_regime(measures: set[str]) -> str | None:
    """Return the single card value agreed for several established measures."""
    return next(
        (regime for regime in ("ban", "restriction", "preference") if regime in measures),
        None,
    )


def _established_regimes_from_application_row(row_text: Any) -> set[str]:
    """Read an explicit ``Да/Нет`` national-regime row, never legal boilerplate."""
    text = re.sub(r"\s+", " ", str(row_text or "")).strip()
    if not text or _NATIONAL_REGIME_NOT_APPLIED_PATTERN.search(text):
        return set()
    if not _NATIONAL_REGIME_APPLIED_PATTERN.search(text):
        return set()
    return {
        regime
        for regime, pattern in _NATIONAL_REGIME_PATTERNS
        if pattern.search(text)
    }


def _national_regime_from_application_tables(
    documents: list[ParsedDocument],
    deterministic_text: str,
) -> tuple[str, str, str] | None:
    """Find a direct PP 1875 application table with a buyer's Yes/No choice.

    Unlike a generic legal section, a table headed ``Применение национального
    режима`` and ``Установлены заказчиком`` expressly says which measure the
    buyer selected. It is a valid source when there is no more granular
    product protection table.
    """
    measures: set[str] = set()
    sources: list[str] = []
    samples: list[str] = []

    def add_rows(source: str, rows: list[str]) -> None:
        joined = "\n".join(rows)
        if not _NATIONAL_REGIME_APPLICATION_HEADER_PATTERN.search(joined):
            return
        for row in rows:
            found = _established_regimes_from_application_row(row)
            if not found:
                continue
            measures.update(found)
            if source not in sources:
                sources.append(source)
            if len(samples) < 3:
                samples.append(re.sub(r"\s+", " ", row).strip()[:500])

    for document in documents:
        for table in document.spreadsheetTables:
            rows = [
                " | ".join(str(value) for value in row.cells.values() if value)
                for row in table.rows
            ]
            source = document.fileName or table.fileName or "Таблица применения национального режима"
            if table.sheet:
                source = f"{source}, лист {table.sheet}"
            add_rows(source, rows)

        # Word/PDF tables may be retained only as text. Restrict the scan to a
        # bounded vicinity of the table header so an unrelated 'да' cannot
        # establish a measure from generic PP 1875 prose elsewhere.
        text = str(document.text or "")
        for header in _NATIONAL_REGIME_APPLICATION_HEADER_PATTERN.finditer(text):
            fragment = text[header.start() : header.start() + 6_000]
            rows = [line for line in fragment.splitlines() if line.strip()]
            add_rows(document.fileName or "Документация", rows)

    if not measures and deterministic_text:
        for header in _NATIONAL_REGIME_APPLICATION_HEADER_PATTERN.finditer(deterministic_text):
            fragment = deterministic_text[header.start() : header.start() + 6_000]
            add_rows("Текст документации", [line for line in fragment.splitlines() if line.strip()])

    value = _highest_national_regime(measures)
    if value is None:
        return None
    return value, "; ".join(sources), "; ".join(samples)


def _national_regime_from_product_tables(
    documents: list[ParsedDocument],
) -> tuple[str, str, str] | None:
    """Return a measure explicitly set in a product-level protection table.

    A procurement template often lists every possible PP 1875 measure in
    prose.  A table whose column is explicitly named ``Вид защитной меры`` is
    tender-specific evidence: its row values take priority over that prose.
    """
    measures: set[str] = set()
    samples: list[str] = []
    sources: list[str] = []
    for document in documents:
        for table in document.spreadsheetTables:
            measure_column = ""
            header = ""
            for row in table.rows:
                found_header = next(
                    (
                        (column, value)
                        for column, value in row.cells.items()
                        if _NATIONAL_REGIME_PRODUCT_COLUMN_PATTERN.search(str(value))
                    ),
                    None,
                )
                if found_header is not None:
                    # ``Национальный режим`` can also appear in a sheet that
                    # merely documents allowed values for an Excel form.  It
                    # is product evidence only when that same row is a real
                    # product-table header.  The dedicated "Вид защитной
                    # меры" header remains sufficient by itself.
                    header_text = " | ".join(str(value) for value in row.cells.values())
                    if (
                        not _NATIONAL_REGIME_TABLE_HEADER_PATTERN.search(str(found_header[1]))
                        and not _PRODUCT_TABLE_HEADER_PATTERN.search(header_text)
                    ):
                        continue
                    measure_column, header = found_header
                    continue
                if not measure_column:
                    continue
                regime = _table_regime_value(row.cells.get(measure_column))
                if regime is None:
                    continue
                measures.add(regime)
                source = document.fileName or table.fileName or "Таблица закупки"
                source_label = f"{source}, лист {table.sheet}"
                if source_label not in sources:
                    sources.append(source_label)
                if len(samples) < 3:
                    position = row.cells.get("A") or row.cells.get("B") or f"строка {row.row}"
                    samples.append(
                        f"{header}: позиция {position} — {row.cells.get(measure_column)}"
                    )

    if not measures:
        return None
    value = _highest_national_regime(measures) or "no"
    return value, "; ".join(sources), "; ".join(samples)


def _is_generic_national_regime_list(evidence: Any) -> bool:
    """True for a boilerplate enumeration of all PP 1875 measures."""
    text = re.sub(r"\s+", " ", str(evidence or "").casefold().replace("ё", "е"))
    has_all_measures = all(
        marker in text for marker in ("запрет", "ограничен", "преимуществ")
    )
    return has_all_measures and bool(
        re.search(r"(?:\bа\)|\bб\)|\bв\)|далее\s*[-–—])", text)
    )


def _direct_goz_evidence(text: str) -> str:
    match = _GOZ_DIRECT_PATTERN.search(text or "")
    return match.group(0) if match is not None else ""


def _has_required_special_account(text: str) -> bool:
    """Recognize a bid-security special account, not a treasury target account."""
    normalized = str(text or "")
    if _APPLICATION_SECURITY_ABSENT_PATTERN.search(normalized):
        return False
    for match in _SPECIAL_ACCOUNT_TERM_PATTERN.finditer(normalized):
        start = max(0, match.start() - 260)
        end = min(len(normalized), match.end() + 260)
        fragment = normalized[start:end]
        if not _APPLICATION_SECURITY_CONTEXT_PATTERN.search(fragment):
            continue
        # A conditional boilerplate clause "если предусмотрено требование" is
        # not proof that this tender actually requires a special account.
        if re.search(r"\bесли\b|в\s+случае", fragment, re.IGNORECASE):
            continue
        if _SPECIAL_ACCOUNT_REQUIREMENT_PATTERN.search(fragment):
            return True
    return False


def _normalize_national_regime(value: Any, evidence: Any) -> str | None:
    """Keep an absent regime absent; map minimum-share regime to ``no``.

    The tender card treats the minimum mandatory share under PP 1875 as
    "Нет", unlike prohibition/restriction/preference.  This is intentionally
    evidence-bound so a generic mention of PP 1875 cannot overwrite an
    established measure for another position.
    """
    raw = str(value or "").strip()
    normalized = raw.casefold().replace("ё", "е").replace("-", "_").replace(" ", "_")
    source_text = f"{raw} {evidence or ''}"
    if _NATIONAL_MINIMUM_SHARE_PATTERN.search(source_text):
        return "no"
    if normalized in {"no", "нет", "minimum_share", "minimum_mandatory_share"}:
        return "no"
    if normalized in {"", "none", "null", "not_found", "не_найдено"}:
        return None
    established = {
        regime
        for regime, pattern in _NATIONAL_REGIME_PATTERNS
        if pattern.search(raw)
    }
    normalized_tokens = set(re.findall(r"[a-z_]+", normalized))
    established.update(
        regime
        for regime in ("ban", "restriction", "preference")
        if regime in normalized_tokens
    )
    if established:
        return _highest_national_regime(established)
    return raw


def _explicit_result_date(text: str) -> tuple[str, str] | None:
    """Find the published tender-result date in attached documents."""
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    # Procurement notices often encode the result as a long table row: the
    # stage title is followed by both a start and an end date.  In that form
    # the end of the stage is the result date, not the first date in the row.
    match = _RESULT_STAGE_END_DATE_PATTERN.search(normalized)
    if match is None:
        match = _RESULT_DATE_PATTERN.search(normalized)
    if match is None:
        return None
    return _normalize_date(match.group(1)), match.group(0)[:500]


_DIRECT_DELIVERY_REQUEST_PATTERN = re.compile(
    r"\u043f\u043e\s+\u0437\u0430\u044f\u0432\u043a|"
    r"\u0437\u0430\u044f\u0432\u043a\u0430\s+\u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a|"
    r"\u043f\u043e\s+\u0440\u0430\u0437\u043d\u0430\u0440\u044f\u0434",
    re.IGNORECASE,
)
_CONDITIONAL_VOLUME_REQUEST_PATTERN = re.compile(
    r"(?:\u043d\u0435\s*\u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u043d\w*|"
    r"\u043d\u0435\u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u043d\w*)\s+\u043e\u0431\u044a[\u0435\u0451]\u043c\w*"
    r"[\s\S]{0,420}?"
    r"(?:\u043f\u043e\u0441\u0442\u0430\u0432\u043b\u044f\w*|\u043f\u043e\u0441\u0442\u0430\u0432\u043a\w*|\u043e\u0442\u0433\u0440\u0443\u0436\u0430\w*)"
    r"[\s\S]{0,180}?"
    r"(?:\u0438\u0441\u043a\u043b\u044e\u0447\u0438\u0442\u0435\u043b\u044c\u043d\u043e\s+)?\u043f\u043e\s+"
    r"(?:\u043f\u0438\u0441\u044c\u043c\u0435\u043d\u043d\w*\s+)?(?:\u0437\u0430\u043f\u0440\u043e\u0441\w*|\u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\w*)\s+"
    r"(?:\u0437\u0430\u043a\u0430\u0437\u0447\u0438\u043a\w*|\u043f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\w*)",
    re.IGNORECASE,
)
_BATCH_OR_DELIVERY_SCHEDULE_PATTERN = re.compile(
    r"\u043f\u043e\u0441\u0442\u0430\u0432\u043a\w*"
    r"(?:\s+(?:\u0442\u043e\u0432\u0430\u0440\w*|\u043e\u0442\u0434\u0435\u043b\u044c\u043d\w*|\u043d\u0435\u0441\u043a\u043e\u043b\u044c\u043a\w*)){0,3}"
    r"\s+\u043f\u0430\u0440\u0442\w*|"
    r"\u043f\u043e\u0441\u0442\u0430\u0432\u043a\w*\s+\u043f\u043e\s+\u0433\u0440\u0430\u0444\u0438\u043a\w*|"
    r"\u0433\u0440\u0430\u0444\u0438\u043a\w*\s+\u043f\u043e\u0441\u0442\u0430\u0432\u043a\w*|"
    r"\u0441\u0440\u043e\u043a\w*\s+\u043e\u0442\u0434\u0435\u043b\u044c\u043d\w*\s+\u043f\u0430\u0440\u0442\w*",
    re.IGNORECASE,
)
_SINGLE_BATCH_PATTERN = re.compile(
    r"(?:\u043e\u0434\u043d\u043e\u0439|\u0435\u0434\u0438\u043d\u043e\u0439)\s+\u043f\u0430\u0440\u0442\w*",
    re.IGNORECASE,
)
_WORK_SCHEDULE_PATTERN = re.compile(
    r"\u0433\u0440\u0430\u0444\u0438\u043a\w*\s+\u0440\u0430\u0431\u043e\u0442\w*",
    re.IGNORECASE,
)
_BATCH_TERM_PATTERN = re.compile(
    r"(?:\u0432\s+\u0442\u0435\u0447\u0435\u043d\u0438\u0435|\u043d\u0435\s+\u043f\u043e\u0437\u0434\u043d\u0435\u0435)\s+\d+\s+\S{2,20}\s+"
    r"(?:\u0441|\u043f\u043e\u0441\u043b\u0435)\s+[^.]{0,80}\u0437\u0430\u044f\u0432\u043a|"
    r"(?:\u0441\u0440\u043e\u043a\w*\s+\u043f\u043e\u0441\u0442\u0430\u0432\u043a\w*|\u043d\u0430\s+\u043a\u0430\u0436\u0434\w*)"
    r"[^.]{0,80}\u043f\u0430\u0440\u0442\w*[^.]{0,80}\d+",
    re.IGNORECASE,
)


def _delivery_type_has_request_evidence(evidence: Any) -> bool:
    """Recognize the business category "by requests / batches / schedule".

    The report column uses one category for a request mechanism and for a
    documented multi-batch delivery schedule.  A single batch and a warehouse
    work schedule are not evidence of repeated deliveries.
    """
    text = re.sub(r"\s+", " ", str(evidence or "")).casefold()
    if (
        _DIRECT_DELIVERY_REQUEST_PATTERN.search(text)
        or _CONDITIONAL_VOLUME_REQUEST_PATTERN.search(text)
    ):
        return True
    return bool(
        not _SINGLE_BATCH_PATTERN.search(text)
        and not _WORK_SCHEDULE_PATTERN.search(text)
        and _BATCH_OR_DELIVERY_SCHEDULE_PATTERN.search(text)
    )


def _delivery_type_evidence_fragment(*values: Any) -> str:
    """Return a short document fragment proving a repeated delivery mechanism."""
    for raw_value in values:
        text = re.sub(r"\s+", " ", str(raw_value or "")).strip()
        if not text or not _delivery_type_has_request_evidence(text):
            continue
        match = (
            _DIRECT_DELIVERY_REQUEST_PATTERN.search(text)
            or _CONDITIONAL_VOLUME_REQUEST_PATTERN.search(text)
            or _BATCH_OR_DELIVERY_SCHEDULE_PATTERN.search(text)
        )
        if match is None:
            continue
        return text[max(0, match.start() - 160) : match.end() + 340][:500]
    return ""


def _delivery_days_are_batch_term(evidence: Any) -> bool:
    """Whether a numeric delivery term is explicitly for a request or batch."""
    text = re.sub(r"\s+", " ", str(evidence or "")).casefold()
    return bool(text and _BATCH_TERM_PATTERN.search(text))


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
    match = _RUSSIAN_DATE_PATTERN.search(text)
    if match:
        day, month_name, year = match.groups()
        month = _RUSSIAN_MONTHS.get(month_name.casefold())
        if month:
            return f"{year}-{month:02d}-{int(day):02d}"
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
    values: list[Any] = [
        purchase.get("resultDate"),
        purchase.get("dateResult"),
        purchase.get("dateResults"),
        purchase.get("summarizingDate"),
        purchase.get("summarizeDate"),
        purchase.get("dateSummingUp"),
        purchase.get("dateOfSummingUp"),
        purchase.get("summarizationDate"),
        purchase.get("protocolDate"),
        purchase.get("protocolPublishDate"),
        job.report_fields.get("Дата подведения итогов"),
    ]
    # Different Seldon exports use several names for this same date. Scan
    # scalar fields too, while keeping the source restricted to Seldon/Daily.
    for source in (purchase, job.report_fields):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            key_text = str(key).casefold().replace("_", "").replace(" ", "")
            if not any(
                marker in key_text
                for marker in ("result", "summar", "подвед", "итог")
            ):
                continue
            if not isinstance(value, (dict, list, tuple, set)):
                values.append(value)
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

    table_national_regime = _national_regime_from_product_tables(documents)
    application_national_regime: tuple[str, str, str] | None = None
    if table_national_regime is not None:
        table_value, table_source, table_evidence = table_national_regime
        previous_value = fields.get("nationalRegime")
        if previous_value and str(previous_value).strip() != table_value:
            warnings.append(
                "nationalRegime из товарной таблицы заменил конфликтующий кандидат из общего текста."
            )
        _set(
            fields,
            meta,
            "nationalRegime",
            table_value,
            table_source,
            "high",
            table_evidence,
        )
    else:
        application_national_regime = _national_regime_from_application_tables(
            documents,
            deterministic_text,
        )
        if application_national_regime is not None:
            application_value, application_source, application_evidence = application_national_regime
            previous_value = fields.get("nationalRegime")
            if previous_value and str(previous_value).strip() != application_value:
                warnings.append(
                    "nationalRegime из специальной таблицы применения ПП №1875 заменил конфликтующий общий кандидат."
                )
            _set(
                fields,
                meta,
                "nationalRegime",
                application_value,
                application_source,
                "high",
                application_evidence,
            )
    if (
        table_national_regime is None
        and application_national_regime is None
        and "nationalRegime" in fields
        and _is_generic_national_regime_list(
        meta.get("nationalRegime", {}).get("evidence")
        )
    ):
        fields.pop("nationalRegime", None)
        meta.pop("nationalRegime", None)
        warnings.append(
            "nationalRegime не заполнен: evidence перечисляет общие меры ПП №1875 без связи с товарной позицией."
        )

    if "nationalRegime" in fields:
        national_regime = _normalize_national_regime(
            fields.get("nationalRegime"),
            meta.get("nationalRegime", {}).get("evidence"),
        )
        if national_regime is None:
            fields.pop("nationalRegime", None)
            meta.pop("nationalRegime", None)
        else:
            fields["nationalRegime"] = national_regime

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

    # Warranty fields are semantically scoped in procurement documents. They
    # must remain the document LLM's decision; regex fallback is only for the
    # application and contract security columns.
    for key, (value, evidence) in _explicit_security_fields(deterministic_text).items():
        if key in {"warrantySecurity", "warrantyMonths"}:
            continue
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
    # The result date is a document fact. The contract date is derived only
    # after a result date has been found, never accepted from an LLM directly.
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
    explicit_result_date = _explicit_result_date(deterministic_text)
    if explicit_result_date and not fields.get("resultDate"):
        result_date, result_evidence = explicit_result_date
        _set(
            fields,
            meta,
            "resultDate",
            result_date,
            "Документы / дата подведения итогов",
            "high",
            result_evidence,
        )
    result_date = _normalize_date(fields.get("resultDate")) if fields.get("resultDate") else None
    if isinstance(result_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", result_date):
        fields["resultDate"] = result_date
        contract_date = date.fromisoformat(result_date) + timedelta(days=20)
        _set(
            fields,
            meta,
            "contractDate",
            contract_date.isoformat(),
            "Расчёт / дата подведения итогов + 20 календарных дней",
            "high",
            f"{result_date} + 20 календарных дней",
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
    goz_evidence = _direct_goz_evidence(deterministic_text)
    previous_goz = fields.get("stateDefenseOrder")
    if str(previous_goz or "").casefold() in {"yes", "да", "true", "1"} and not goz_evidence:
        warnings.append(
            "stateDefenseOrder=да отклонён: казначейское сопровождение, субсидия, "
            "идентификатор госконтракта или счёт целевых средств не доказывают ГОЗ."
        )
    _set(
        fields,
        meta,
        "stateDefenseOrder",
        "yes" if goz_evidence else "no",
        "Документы / прямой признак ГОЗ",
        "high" if goz_evidence else "medium",
        goz_evidence or "Нет прямого упоминания ГОЗ или 275-ФЗ",
    )

    special_account_required = _has_required_special_account(deterministic_text)
    previous_special_account = fields.get("specialAccount")
    if (
        str(previous_special_account or "").casefold() in {"yes", "да", "true", "1"}
        and not special_account_required
    ):
        warnings.append(
            "specialAccount=да отклонён: не найдено прямого требования спецсчёта "
            "для обеспечения заявки."
        )
    _set(
        fields,
        meta,
        "specialAccount",
        "yes" if special_account_required else "no",
        "Документы / спецсчёт для обеспечения заявки",
        "high" if special_account_required else "medium",
        (
            "Прямое требование спецсчёта для обеспечения заявки"
            if special_account_required
            else "Нет прямого требования спецсчёта для обеспечения заявки"
        ),
    )
    # The SME/self-employed payment term is selected only when this tender is
    # explicitly restricted to those bidders.  In an open tender it is merely
    # an exception; the card retains the longest general payment term.
    llm_payment_delay = _payment_delay_from_field(
        fields.get("paymentDelayDays"),
        meta.get("paymentDelayDays", {}).get("evidence"),
    )
    sme_only_procurement = _procurement_is_sme_only(deterministic_text)
    document_payment_delay = _payment_delay_from_text(
        deterministic_text,
        prefer_sme_term=sme_only_procurement,
    )
    document_uses_sme_term = bool(
        sme_only_procurement
        and document_payment_delay
        and _SME_PAYMENT_PATTERN.search(document_payment_delay[1])
    )
    payment_delay = (
        document_payment_delay
        if document_uses_sme_term
        else max(
            (candidate for candidate in (llm_payment_delay, document_payment_delay) if candidate),
            key=lambda candidate: candidate[0],
            default=None,
        )
    )
    payment_delay_source = (
        "Документы / срок оплаты для закупки только у МСП или самозанятого"
        if document_uses_sme_term
        else "Документы / максимальная отсрочка оплаты в календарных днях"
        if payment_delay == document_payment_delay
        else "LLM evidence / отсрочка переведена по правилу 1.4"
    )
    if payment_delay:
        days, evidence = payment_delay
        _set(
            fields,
            meta,
            "paymentDelayDays",
            days,
            payment_delay_source,
            "high",
            evidence,
        )
    delivery_type = _normalize_delivery_type(fields.get("deliveryType"))
    delivery_evidence = meta.get("deliveryType", {}).get("evidence")
    repeated_delivery_evidence = _delivery_type_evidence_fragment(
        delivery_evidence,
        meta.get("deliveryNote", {}).get("evidence"),
        fields.get("deliveryNote"),
        deterministic_text,
    )
    if repeated_delivery_evidence:
        if delivery_type != "by_requests":
            warnings.append(
                "Тип поставки исправлен на «по заявкам»: найдено подтверждение "
                "поставки партиями, по заявкам или графику."
            )
        delivery_type = "by_requests"
        delivery_evidence = repeated_delivery_evidence
        _set(
            fields,
            meta,
            "deliveryType",
            delivery_type,
            "Документы / поставка партиями, по заявкам или графику",
            "high",
            delivery_evidence,
        )
    if delivery_type == "by_requests" and not _delivery_type_has_request_evidence(
        delivery_evidence
    ):
        # A bare mention of a party still does not prove repeated delivery.
        # Leave the type blank rather than converting it into a false request
        # delivery; the factual text remains available in deliveryNote.
        delivery_date_evidence = meta.get("deliveryDate", {}).get("evidence")
        if fields.get("deliveryDate") and _has_explicit_delivery_deadline_evidence(
            delivery_date_evidence
        ):
            delivery_type = "single_date"
            delivery_evidence = delivery_date_evidence
            warnings.append(
                "Тип поставки исправлен с «по заявкам» на «к дате»: заявка/разнарядка не подтверждена, но есть прямая дата поставки."
            )
        else:
            delivery_type = None
            fields.pop("deliveryType", None)
            meta.pop("deliveryType", None)
            warnings.append(
                "Тип поставки «по заявкам» не подтверждён: упоминание партии без заявки/разнарядки не является основанием."
            )
    if delivery_type:
        _set(
            fields,
            meta,
            "deliveryType",
            delivery_type,
            str(meta.get("deliveryType", {}).get("source") or "AI Agent"),
            str(meta.get("deliveryType", {}).get("confidence") or "medium"),
            str(meta.get("deliveryType", {}).get("evidence") or "Условие поставки"),
        )

    # For deliveries by requests/batches, a term such as "within N days after
    # a request" belongs to the batch column, not to the general delivery term.
    if delivery_type == "by_requests":
        batch_days_evidence = str(meta.get("deliveryBatchDays", {}).get("evidence") or "")
        if fields.get("deliveryBatchDays") and not _delivery_days_are_batch_term(batch_days_evidence):
            fields.pop("deliveryBatchDays", None)
            meta.pop("deliveryBatchDays", None)
            warnings.append(
                "Срок поставки партии не заполнен: число дней не связано прямо с заявкой или отдельной партией."
            )
        if not fields.get("deliveryBatchDays") and fields.get("deliveryDays"):
            delivery_days_meta = meta.get("deliveryDays", {})
            delivery_days_evidence = str(delivery_days_meta.get("evidence") or "")
            if _delivery_days_are_batch_term(delivery_days_evidence):
                _set(
                    fields,
                    meta,
                    "deliveryBatchDays",
                    fields["deliveryDays"],
                    "Документы / срок поставки партии",
                    delivery_days_meta.get("confidence", "high"),
                    delivery_days_evidence,
                )
            else:
                warnings.append(
                    "Общий срок поставки не перенесён в срок партии: нет прямой связи со сроком заявки или отдельной партии."
                )
        fields.pop("deliveryDays", None)
        meta.pop("deliveryDays", None)

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

    if not fields.get("lotDivisible"):
        _set(
            fields,
            meta,
            "lotDivisible",
            "no",
            "Правило автозаполнения / делимость не установлена",
            "medium",
            "В документах нет подтверждения делимости лота",
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
