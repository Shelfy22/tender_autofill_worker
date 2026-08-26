from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.models import LlmDecision, NormalizedJob


REASONS = [
    "Дубль",
    "Коммерческие условия. НМЦК менее 1 млн руб.",
    "Коммерческие условия. НМЦК менее фактической стоимости",
    "Коммерческие условия. Не проходим по сроку поставки",
    "Коммерческие условия. Оплата Покупателем после оплаты Генподрядчиком / Госзаказчиком",
    "Коммерческие условия. Отсрочка платежа 90 дней и более",
    "Коммерческие условия. Поставка в удаленные территории",
    "Коммерческие условия. Консигнация / Хранение у Покупателя за счет Поставщика",
    "Номенклатура. Лот неделимый. Не можем скомплектовать более 20% номенклатуры",
    "Номенклатура. Оборудование 35 кВ и выше",
    "Номенклатура. Частотный привод 6–10 кВ",
    "Номенклатура. Ремкомплект / ЗИП / Продукция по чертежу",
    "Номенклатура. Военная приемка",
    "Номенклатура. Атомная приемка",
    "Номенклатура. Поставка с работами",
    "Оргвопросы. На момент согласования менее 3 рабочих дней до подачи заявки",
    "Оргвопросы. МОПП подается самостоятельно (подача без ЭЦП)",
    "Оргвопросы. Тендер ХК. Нет УРКК. Договоры с филиалами. Отгрузка по всей стране.",
    "Оргвопросы. Отсутствует ТЗ / Нет документации / Некорректная ссылка",
    "Оргвопросы. Закрытый тендер / Не прошли квалификацию",
    "Оргвопросы. Отказ организатора от проведения тендера",
    "Оргвопросы. Отпуск или Болезнь МОПП",
    "Оргвопросы. Опрос рынка / Мониторинг / Анализ рынка / Анонс / КИМ",
    "Непоставляемый ассортимент",
    "Прочее",
]

DEADLINE_REASON = "Оргвопросы. На момент согласования менее 3 рабочих дней до подачи заявки"
ASSORTMENT_REASON = "Непоставляемый ассортимент"
INDIVISIBLE_REASON = "Номенклатура. Лот неделимый. Не можем скомплектовать более 20% номенклатуры"
COVERAGE_REASON = INDIVISIBLE_REASON
PRICE_REASON = "Коммерческие условия. НМЦК менее 1 млн руб."
ACTUAL_COST_REASON = "Коммерческие условия. НМЦК менее фактической стоимости"
REMOTE_TERRITORY_REASON = "Коммерческие условия. Поставка в удаленные территории"
PAYMENT_DELAY_REASON = "Коммерческие условия. Отсрочка платежа 90 дней и более"
DOCUMENTATION_REASON = "Оргвопросы. Отсутствует ТЗ / Нет документации / Некорректная ссылка"
MARKET_RESEARCH_REASON = "Оргвопросы. Опрос рынка / Мониторинг / Анализ рынка / Анонс / КИМ"

HIGH_VOLTAGE_REASON = "Номенклатура. Оборудование 35 кВ и выше"
_VOLTAGE_COMPONENT = r"\d{1,4}(?:[.,]\d+)?"
_POWER_SUFFIX_GUARD = r"(?!\s*(?:[·∙*×xх]\s*)?[аa]\b)"
_COMPOUND_VOLTAGE_PATTERN = re.compile(
    rf"\b(?P<values>{_VOLTAGE_COMPONENT}(?:\s*/\s*{_VOLTAGE_COMPONENT})+)"
    rf"\s*(?:кв|kv)\b{_POWER_SUFFIX_GUARD}",
    re.IGNORECASE,
)
_SINGLE_VOLTAGE_PATTERN = re.compile(
    rf"\b(?P<value>{_VOLTAGE_COMPONENT})\s*(?:кв|kv)\b{_POWER_SUFFIX_GUARD}",
    re.IGNORECASE,
)
_VOLTAGE_COMPONENT_PATTERN = re.compile(_VOLTAGE_COMPONENT)
_VOLTAGE_CONTEXT_PATTERN = re.compile(
    r"\b(?:напряжени[еяю]|"
    r"класс(?:а|у|ом)?\s+напряжения|вн|нн)\b",
    re.IGNORECASE,
)
_TRANSFORMER_MODEL_PREFIX_PATTERN = re.compile(
    r"\b(?:\u0442\u043c\u0433\d*|\u0442\u0434\u043d\d*|\u0442\u0434\u0442\u043d\d*|\u0442\u0440\u0434\u043d\d*|"
    r"\u0442\u0441\u0437\u043b?\d*|\u0442\u043c\u043d\d*|\u0442\u043c\u0437\d*|\u0442\u0434\u0446\d*)\s*[-\u2013\u2014]?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HardReason:
    reason: str
    evidence: str
    priority: int

    def as_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "evidence": self.evidence[:1000], "priority": self.priority}


def unwrap(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def parse_money(value: Any) -> float | None:
    value = unwrap(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value >= 0 else None
    text = str(value or "").lower().replace("\xa0", " ").strip()
    if not text:
        return None
    multiplier = 1.0
    if re.search(r"\bмлрд", text):
        multiplier = 1_000_000_000
    elif re.search(r"\bмлн", text):
        multiplier = 1_000_000
    cleaned = re.sub(r"[^0-9,.-]", "", text.replace(" ", "")).rstrip(".,")
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    comma, dot = cleaned.rfind(","), cleaned.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal = "," if comma > dot else "."
        thousands = "." if decimal == "," else ","
        cleaned = cleaned.replace(thousands, "").replace(decimal, ".")
    elif comma >= 0:
        decimals = len(cleaned) - comma - 1
        cleaned = cleaned.replace(",", "." if 0 < decimals <= 2 else "")
    elif dot >= 0 and not (0 < len(cleaned) - dot - 1 <= 2):
        cleaned = cleaned.replace(".", "")
    try:
        number = float(cleaned) * multiplier
    except ValueError:
        return None
    return number if number >= 0 else None


def parse_date(value: Any) -> date | None:
    value = unwrap(value)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _snippet(text: str, match: re.Match[str], radius: int = 180) -> str:
    return re.sub(r"\s+", " ", text[max(0, match.start() - radius): match.end() + radius]).strip()



def _voltage_text_snippet(text: str, start: int, end: int, radius: int = 120) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - radius): end + radius]).strip()


def _extract_voltage_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    compound_spans: list[tuple[int, int]] = []
    for match in _COMPOUND_VOLTAGE_PATTERN.finditer(text):
        compound_spans.append(match.span())
        components = _VOLTAGE_COMPONENT_PATTERN.findall(match.group("values"))
        prefix = text[max(0, match.start() - 80): match.start()]
        if len(components) >= 3 and _TRANSFORMER_MODEL_PREFIX_PATTERN.search(prefix):
            # Transformer model names commonly encode apparent power first:
            # TMG-1000/10/0.4 kV means 1000 kVA and voltages 10/0.4 kV.
            components = components[1:]
        for component in components:
            mentions.append(
                {
                    "voltageKv": float(component.replace(",", ".")),
                    "matchedText": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                }
            )

    for match in _SINGLE_VOLTAGE_PATTERN.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in compound_spans):
            continue
        mentions.append(
            {
                "voltageKv": float(match.group("value").replace(",", ".")),
                "matchedText": match.group(0),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return mentions


def _collect_product_voltage_mentions(product_check: dict[str, Any]) -> list[dict[str, Any]]:
    details = product_check.get("details")
    if not isinstance(details, list):
        return []

    collected: list[dict[str, Any]] = []
    seen: set[tuple[int, str, float, str]] = set()
    primary_fields = {"sourceProduct", "productQuery", "sourceRequirements"}
    for fallback_index, detail in enumerate(details, start=1):
        if not isinstance(detail, dict):
            continue
        position_index = detail.get("positionIndex") or fallback_index
        source_product = str(detail.get("sourceProduct") or "").strip()
        seen_texts: set[str] = set()
        for source_field in (
            "sourceProduct",
            "productQuery",
            "sourceRequirements",
            "sourceEvidence",
        ):
            source_text = str(detail.get(source_field) or "").strip()
            normalized_text = re.sub(r"\s+", " ", source_text).casefold()
            if not source_text or normalized_text in seen_texts:
                continue
            seen_texts.add(normalized_text)
            for mention in _extract_voltage_mentions(source_text):
                context = _voltage_text_snippet(
                    source_text,
                    int(mention["start"]),
                    int(mention["end"]),
                )
                if source_field not in primary_fields and not _VOLTAGE_CONTEXT_PATTERN.search(context):
                    continue
                key = (
                    int(position_index),
                    source_field,
                    float(mention["voltageKv"]),
                    str(mention["matchedText"]).casefold(),
                )
                if key in seen:
                    continue
                seen.add(key)
                collected.append(
                    {
                        "positionIndex": position_index,
                        "sourceProduct": source_product,
                        "sourceField": source_field,
                        "voltageKv": mention["voltageKv"],
                        "matchedText": mention["matchedText"],
                        "evidence": context,
                    }
                )
    return collected


def _add(reasons: list[HardReason], reason: str, evidence: str, priority: int) -> None:
    if not any(item.reason == reason for item in reasons):
        reasons.append(HardReason(reason, evidence, priority))


def _field(fields: dict[str, Any], name: str) -> Any:
    return unwrap(fields.get(name))


def calculate_hard_reasons(
    job: NormalizedJob,
    fields: dict[str, Any],
    product_check: dict[str, Any],
    all_text: str,
    *,
    document_context: dict[str, Any] | None = None,
) -> tuple[list[HardReason], dict[str, Any]]:
    reasons: list[HardReason] = []
    document_context = dict(document_context or {})

    coverage = product_check.get("coveragePercent")
    coverage_number = float(coverage) if isinstance(coverage, (int, float)) else None
    if product_check.get("hardReject") is True:
        _add(
            reasons,
            COVERAGE_REASON,
            product_check.get("summary") or "Проверка комплектования лота не пройдена.",
            5,
        )

    if document_context.get("documentationMissing") is True:
        _add(
            reasons,
            DOCUMENTATION_REASON,
            str(document_context.get("documentationNote") or "").strip()
            or (
                "Seldon не выдал документы после запроса: "
                f"code={document_context.get('apiCode')}; "
                f"{document_context.get('apiDescription') or 'документация отсутствует'}."
            ),
            25,
        )

    total = product_check.get("total")
    products_not_evaluated = (
        isinstance(total, (int, float)) and not isinstance(total, bool) and total <= 0
    ) or coverage_number is None
    if products_not_evaluated:
        _add(
            reasons,
            DOCUMENTATION_REASON,
            "Товарные позиции для проверки ассортимента не извлечены; "
            "coverage не рассчитан. Автоматическое согласование запрещено.",
            25,
        )

    duplicate = re.search(r"\b(дубль|дубликат|повторная\s+карточка|уже\s+загружен[ао]?)\b", all_text, re.I)
    if duplicate:
        _add(reasons, "Дубль", _snippet(all_text, duplicate), 10)

    if job.remaining_days is not None and job.remaining_days < 3:
        _add(
            reasons,
            DEADLINE_REASON,
            f"Колонка «Осталось дней»: {job.remaining_days}. Условие: remainingDays < 3.",
            15,
        )

    initial_price = parse_money(_field(fields, "initialPrice"))
    regulated_purchase = job.report_id in {1, 2}
    commercial_purchase = job.report_id == 3
    if (
        regulated_purchase
        and initial_price is not None
        and initial_price > 0
        and initial_price < 1_000_000
    ):
        _add(reasons, PRICE_REASON, f"НМЦК: {_field(fields, 'initialPrice')}", 20)

    total_price = product_check.get("supplyTotalPriceRub")
    if (
        commercial_purchase
        and product_check.get("supplyValueHardReject") is True
        and product_check.get("supplyValueThresholdApplicable") is True
        and product_check.get("priceEvaluationComplete") is True
        and product_check.get("coverageApproved") is True
        and isinstance(total_price, (int, float))
        and total_price < 1_000_000
    ):
        _add(reasons, PRICE_REASON, product_check.get("priceSummary") or str(total_price), 21)

    patterns: list[tuple[str, str, int]] = [
        (
            r"(?:оплат[а-я]*|расч[её]т[а-я]*)[\s\S]{0,100}(?:после|по\s+мере)[\s\S]{0,100}(?:генподрядчик|госзаказчик)",
            "Коммерческие условия. Оплата Покупателем после оплаты Генподрядчиком / Госзаказчиком", 30,
        ),
        (
            r"\b(республик[а-я]*\s+саха(?:\s*\(якутия\))?|якут(?:ия|ск[а-я]*)|"
            r"дагестан(?:ск[а-я]*)?|калининград(?:ск[а-я]*)?)\b",
            REMOTE_TERRITORY_REASON,
            40,
        ),
        (r"\b(консигнац|ответственн(?:ое|ого)\s+хранени|хранени[ея]\s+у\s+покупателя)[\s\S]{0,140}(?:за\s+сч[её]т\s+поставщика|поставщик)",
         "Коммерческие условия. Консигнация / Хранение у Покупателя за счет Поставщика", 45),
        (r"(?:частотн[а-я]*\s+(?:привод|преобразователь)|пч)[\s\S]{0,120}\b(?:6|10|6\s*[-–]\s*10)\s*к\s*в\b",
         "Номенклатура. Частотный привод 6–10 кВ", 60),
        (r"\b(ремкомплект|ремонтн[а-я]*\s+комплект|зип|запасн[а-я]*\s+част|изготовлени[ея]\s+по\s+чертеж|продукци[яи]\s+по\s+чертеж)\b",
         "Номенклатура. Ремкомплект / ЗИП / Продукция по чертежу", 65),
        (r"\b(военн[а-я]*\s+при[её]мк[а-я]*|при[её]мк[а-я]*\s+военн[а-я]*\s+представитель|контрол[ья]\s+военн[а-я]*\s+представитель)\b",
         "Номенклатура. Военная приемка", 70),
        (r"\b(атомн[а-я]*\s+при[её]мк[а-я]*|при[её]мк[а-я]*\s+для\s+о(?:бъект|иаэ)|класс\s+безопасности\s+[1-4])\b",
         "Номенклатура. Атомная приемка", 75),
        (r"(?:мопп|менеджер)[\s\S]{0,120}(?:пода[её]т|подача)[\s\S]{0,100}(?:самостоятельно|без\s+эцп)",
         "Оргвопросы. МОПП подается самостоятельно (подача без ЭЦП)", 16),
        (r"\b(закрыт(?:ый|ая)\s+(?:тендер|закупка)|не\s+прошли\s+квалификац)\b",
         "Оргвопросы. Закрытый тендер / Не прошли квалификацию", 90),
        (r"\b(отказ\s+организатора|закупка\s+отменена|отказался\s+от\s+проведения)\b",
         "Оргвопросы. Отказ организатора от проведения тендера", 91),
    ]
    for pattern, reason, priority in patterns:
        match = re.search(pattern, all_text, re.I)
        if match:
            _add(reasons, reason, _snippet(all_text, match), priority)

    market_research_match = re.search(
        r"\b(опрос\s+рынка|мониторинг\s+рынка|анализ\s+рынка|"
        r"маркетингов(?:ое|ого)\s+исследовани[ея]|исследование\s+рынка|"
        r"анонс\s+закупки|ким)\b",
        all_text,
        re.I,
    )
    if job.report_id == 3 and market_research_match:
        _add(
            reasons,
            MARKET_RESEARCH_REASON,
            _snippet(all_text, market_research_match),
            18,
        )

    voltage_mentions = _collect_product_voltage_mentions(product_check)
    high_voltage_mention = next(
        (item for item in voltage_mentions if item["voltageKv"] >= 35),
        None,
    )
    if high_voltage_mention:
        voltage = float(high_voltage_mention["voltageKv"])
        voltage_text = f"{voltage:g}".replace(".", ",")
        source_product = high_voltage_mention.get("sourceProduct") or ""
        product_label = f" «{source_product}»" if source_product else ""
        evidence = (
            f"Товарная позиция {high_voltage_mention['positionIndex']}"
            f"{product_label}: "
            f"в поле {high_voltage_mention['sourceField']} найдено "
            f"напряжение {voltage_text} кВ. "
            f"Фрагмент: {high_voltage_mention['evidence']}"
        )
        _add(reasons, HIGH_VOLTAGE_REASON, evidence, 55)

    for match in re.finditer(
        r"(?:отсрочк[а-я]*|оплат[а-яё\s]{0,120}?в\s+течение)"
        r"[^\d]{0,80}(\d+)\s*(?:(рабоч|календарн)[а-я]*)?\s*д",
        all_text,
        re.I,
    ):
        number_of_days = int(match.group(1))
        if number_of_days >= 90:
            _add(reasons, PAYMENT_DELAY_REASON, match.group(0), 35)

    created = parse_date(_field(fields, "dateCreated")) or date.today()
    delivery = parse_date(_field(fields, "deliveryDate"))
    delivery_days = parse_money(_field(fields, "deliveryDays"))
    if delivery is None and delivery_days and delivery_days > 0:
        delivery = created + timedelta(days=int(delivery_days + 0.999999))
    delivery_limit = add_months(created, 18)
    if delivery and delivery > delivery_limit:
        _add(
            reasons,
            "Коммерческие условия. Не проходим по сроку поставки",
            f"Дата заведения: {created}; предел 18 месяцев: {delivery_limit}; поставка: {delivery}",
            32,
        )

    work_match = re.search(
        r"(?:монтаж|установк[а-я]*|пусконаладк[а-я]*|ввод\s+в\s+эксплуатацию)[\s\S]{0,100}(?:поставщиком|подрядчиком|силами\s+поставщика|входит\s+в\s+(?:предмет|стоимость|объ[её]м))",
        all_text,
        re.I,
    )
    if work_match:
        context = _snippet(all_text, work_match, 120)
        if not re.search(r"не\s+требуется|не\s+входит|заказчик|без\s+монтажа", context, re.I):
            _add(reasons, "Номенклатура. Поставка с работами", context, 80)

    reasons.sort(key=lambda item: item.priority)
    checks = {
        "initialPriceNumeric": initial_price,
        "priceThresholdCheck": {
            "reportId": job.report_id,
            "mode": (
                "initial_price_only"
                if regulated_purchase
                else "calculated_qdrant_price_times_quantity"
                if commercial_purchase
                else "not_configured"
            ),
            "initialPriceNumeric": initial_price,
            "calculatedSupplyValueRub": product_check.get("supplyTotalPriceRub"),
            "calculatedValueInformationalOnly": regulated_purchase,
            "thresholdRub": 1_000_000,
        },
        "submissionDeadlineCheck": {
            "source": "Seldon / колонка «Осталось дней»",
            "remainingDays": job.remaining_days,
            "thresholdExclusive": 3,
            "comparison": "remainingDays < 3",
            "triggered": job.remaining_days is not None and job.remaining_days < 3,
        },
        "productCoverageCheck": {
            "coveragePercent": coverage_number,
            "coverageRule": product_check.get("coverageRule"),
            "lotDivisible": product_check.get("lotDivisible"),
            "approvalThresholdInclusive": product_check.get("approvalThresholdInclusive"),
            "minimumSuppliedPositions": product_check.get("minimumSuppliedPositions"),
            "approved": product_check.get("coverageApproved") is True,
            "triggered": product_check.get("hardReject") is True,
            "quantityAdjustedTotalComplete": product_check.get("priceEvaluationComplete") is True,
        },
        "documentationCheck": {
            **document_context,
            "productsExtracted": not products_not_evaluated,
            "automaticApprovalAllowed": not products_not_evaluated
            and document_context.get("documentationMissing") is not True,
        },
        "highVoltageCheck": {
            "source": "productCheck.details / structured tender positions",
            "thresholdInclusiveKv": 35,
            "parsedVoltages": voltage_mentions[:100],
            "triggered": high_voltage_mention is not None,
        },
        "marketResearchCheck": {
            "reportId": job.report_id,
            "commercialOnly": True,
            "detectedInText": market_research_match is not None,
            "rejectionApplicable": job.report_id == 3,
            "triggered": job.report_id == 3 and market_research_match is not None,
        },
        "deliveryDeadlineCheck": {
            "createdDate": created.isoformat(),
            "deliveryDate": delivery.isoformat() if delivery else None,
            "maximumPeriodMonths": 18,
            "limitDate": delivery_limit.isoformat(),
            "exceedsMaximum": bool(delivery and delivery > delivery_limit),
        },
    }
    return reasons, checks


def apply_final_decision(
    *,
    fields: dict[str, Any],
    meta: dict[str, Any],
    product_check: dict[str, Any],
    hard_reasons: list[HardReason],
    counterparty_lookup: dict[str, Any],
    llm_decision: LlmDecision | None,
    report_id: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    fields = dict(fields)
    meta = dict(meta)
    counterparty_requires_work = counterparty_lookup.get("status") != "matched"
    market_research_suppressed = report_id in {1, 2}
    hard = sorted(
        (
            item
            for item in hard_reasons
            if not (
                market_research_suppressed
                and item.reason == MARKET_RESEARCH_REASON
            )
        ),
        key=lambda item: item.priority,
    )
    coverage_value = product_check.get("coveragePercent")
    product_total = product_check.get("total")
    if (
        coverage_value is None
        or (
            isinstance(product_total, (int, float))
            and not isinstance(product_total, bool)
            and product_total <= 0
        )
    ) and not any(item.reason == DOCUMENTATION_REASON for item in hard):
        hard.append(
            HardReason(
                DOCUMENTATION_REASON,
                "Товарные позиции не извлечены либо coverage не рассчитан. "
                "Автоматическое согласование запрещено.",
                25,
            )
        )
        hard.sort(key=lambda item: item.priority)
    hard_non_coverage = [
        item
        for item in hard
        if item.reason not in {ASSORTMENT_REASON, COVERAGE_REASON}
    ]
    coverage = product_check.get("coveragePercent")

    # Controlled reasons cannot be reintroduced by the LLM.
    allowed_detected = []
    if llm_decision:
        for item in llm_decision.detectedReasons:
            if item.reason not in REASONS or item.reason in {
                DEADLINE_REASON,
                ASSORTMENT_REASON,
                INDIVISIBLE_REASON,
                HIGH_VOLTAGE_REASON,
                PRICE_REASON,
                ACTUAL_COST_REASON,
            }:
                continue
            if market_research_suppressed and item.reason == MARKET_RESEARCH_REASON:
                continue
            if (
                product_check.get("coverageApproved") is True
                and re.search(r"ассортимент|номенклатур", item.evidence, re.I)
            ):
                continue
            allowed_detected.append(item)

    llm_primary = llm_decision.primaryReason if llm_decision else None
    if llm_primary not in REASONS or llm_primary in {
        DEADLINE_REASON,
        ASSORTMENT_REASON,
        INDIVISIBLE_REASON,
        HIGH_VOLTAGE_REASON,
        PRICE_REASON,
        ACTUAL_COST_REASON,
    } or (market_research_suppressed and llm_primary == MARKET_RESEARCH_REASON):
        llm_primary = allowed_detected[0].reason if allowed_detected else None

    # primaryReason is kept first, followed by the complete detectedReasons list.
    # The deterministic coverage reason remains a fallback primary reason: another
    # confirmed LLM reason becomes primary while coverage is retained in the note.
    llm_reason_candidates: list[dict[str, str]] = []

    def add_llm_reason_candidate(reason: str | None, evidence: str, confidence: str) -> None:
        if (
            not reason
            or reason not in REASONS
            or reason in {DEADLINE_REASON, ASSORTMENT_REASON, INDIVISIBLE_REASON, HIGH_VOLTAGE_REASON}
            or (market_research_suppressed and reason == MARKET_RESEARCH_REASON)
            or any(item["reason"] == reason for item in llm_reason_candidates)
        ):
            return
        llm_reason_candidates.append(
            {
                "reason": reason,
                "evidence": re.sub(r"\s+", " ", evidence or "").strip(),
                "confidence": confidence if confidence in {"low", "medium", "high"} else "medium",
                "source": "LLM",
            }
        )

    if llm_decision and llm_primary:
        primary_detected = next(
            (item for item in allowed_detected if item.reason == llm_primary),
            None,
        )
        add_llm_reason_candidate(
            llm_primary,
            primary_detected.evidence if primary_detected else "",
            primary_detected.confidence if primary_detected else llm_decision.confidence,
        )
    for item in allowed_detected:
        add_llm_reason_candidate(item.reason, item.evidence, item.confidence)

    preferred_llm_alternative = llm_reason_candidates[0] if llm_reason_candidates else None
    note_parts: list[str] = []
    summary = re.sub(r"\s+", " ", str(product_check.get("summary") or "")).strip()
    if summary:
        note_parts.append(summary)
    reason_origin = "none"

    if hard:
        status = "Отказано КУ ЦП"
        if hard_non_coverage:
            # Existing priority order is preserved among all deterministic reasons
            # except coverage, which is only the fallback primary reason.
            reason, confidence = hard_non_coverage[0].reason, "high"
            reason_origin = "deterministic"
        elif preferred_llm_alternative:
            reason = preferred_llm_alternative["reason"]
            confidence = preferred_llm_alternative["confidence"]
            reason_origin = "llm_alternative_over_coverage"
        else:
            reason, confidence = hard[0].reason, "high"
            reason_origin = "deterministic"
        note_parts.append(f"Основная причина отказа: {reason}.")
        documentation_reason = next(
            (item for item in hard if item.reason == DOCUMENTATION_REASON),
            None,
        )
        if documentation_reason and documentation_reason.evidence:
            note_parts.append(documentation_reason.evidence)
    elif llm_decision and llm_decision.decision == "reject" and llm_primary:
        status, reason, confidence = "Отказано КУ ЦП", llm_primary, llm_decision.confidence
        reason_origin = "llm"
        note_parts.append(llm_decision.note or f"Подтверждён критерий отказа: {llm_primary}.")
    elif counterparty_requires_work:
        status, reason, confidence = "Проработка контрагента", "Прочее", "medium"
        reason_origin = "counterparty"
        note_parts.append(
            counterparty_lookup.get("reason") or "Контрагент с указанным ИНН не найден в IPro."
        )
    elif llm_decision and llm_decision.decision == "reject":
        status, reason, confidence = "Согласовано КУ ЦП", None, llm_decision.confidence
        note_parts.append(
            "LLM reject содержал только запрещённую/неподтверждённую controlled-причину."
        )
    elif llm_decision and llm_decision.decision == "approve":
        status, reason, confidence = "Согласовано КУ ЦП", None, llm_decision.confidence
        note_parts.append(llm_decision.note or "Критерии отказа не подтверждены.")
    else:
        status, reason, confidence = "Загружен Seldon", "Прочее", "low"
        reason_origin = "fallback"
        note_parts.append("LLM решения по статусу не вернул валидный JSON; обязательные критерии не сработали.")

    counterparty_advisory_only = (
        status == "Отказано КУ ЦП" and counterparty_requires_work
    )
    if counterparty_advisory_only:
        counterparty_evidence = (
            counterparty_lookup.get("reason")
            or "Контрагент с указанным ИНН не найден в IPro."
        )
        note_parts.append(
            "Дополнительная информация по контрагенту: "
            f"{counterparty_evidence} Статус «Проработка контрагента» не применяется, "
            "поскольку тендер уже имеет подтверждённую причину отказа."
        )

    additional_reasons: list[dict[str, str]] = []

    def add_additional_reason(
        additional_reason: str,
        evidence: str,
        source: str,
        item_confidence: str,
    ) -> None:
        if (
            not additional_reason
            or additional_reason == reason
            or any(item["reason"] == additional_reason for item in additional_reasons)
        ):
            return
        additional_reasons.append(
            {
                "reason": additional_reason,
                "evidence": re.sub(r"\s+", " ", evidence or "").strip(),
                "source": source,
                "confidence": item_confidence,
            }
        )

    if status == "Отказано КУ ЦП":
        for item in hard:
            add_additional_reason(item.reason, item.evidence, "Детерминированное правило", "high")
        for item in llm_reason_candidates:
            add_additional_reason(
                item["reason"], item["evidence"], item["source"], item["confidence"]
            )
        if additional_reasons:
            formatted_items = []
            for item in additional_reasons:
                formatted = item["reason"]
                if item["evidence"]:
                    formatted += f" — {item['evidence']}"
                formatted_items.append(formatted)
            formatted_reasons = " | ".join(formatted_items)
            note_parts.append(f"Дополнительные подтверждённые причины: {formatted_reasons}")

    normalized_note_parts: list[str] = []
    for part in note_parts:
        normalized = re.sub(r"\s+", " ", str(part or "")).strip()
        if normalized and normalized not in normalized_note_parts:
            normalized_note_parts.append(normalized)
    note = " ".join(normalized_note_parts)[:4000]
    fields["tenderStatus"] = status
    fields["tenderStatusNote"] = note
    if reason:
        fields["tenderStatusReason"] = reason
    else:
        fields.pop("tenderStatusReason", None)
    status_source = (
        "Проверка контрагента/МОПП"
        if reason_origin == "counterparty"
        else "Детерминированные правила согласования"
        if reason_origin == "deterministic"
        else "LLM: решение КУ ЦП"
    )
    meta["tenderStatus"] = {
        "source": status_source,
        "confidence": confidence,
        "evidence": note[:1200],
    }
    meta["tenderStatusNote"] = {
        "source": "Ветка согласования тендера",
        "confidence": confidence,
        "evidence": note[:1200],
    }
    if reason:
        reason_source = {
            "counterparty": "Проверка контрагента/МОПП",
            "deterministic": "Детерминированные правила согласования",
            "llm_alternative_over_coverage": (
                "LLM: альтернативная причина при обязательном отказе по комплектованию лота"
            ),
            "llm": "LLM: классификация причины",
            "fallback": "Ветка согласования тендера",
        }.get(reason_origin, "Ветка согласования тендера")
        meta["tenderStatusReason"] = {
            "source": reason_source,
            "confidence": confidence,
            "evidence": note[:1200],
        }
    else:
        meta.pop("tenderStatusReason", None)

    decision = {
        "status": status,
        "reason": reason,
        "note": note,
        "confidence": confidence,
        "reasonOrigin": reason_origin,
        "counterpartyRequiresWork": counterparty_requires_work,
        "counterpartyAdvisoryOnly": counterparty_advisory_only,
        "hardReasons": [item.as_dict() for item in hard],
        # Keep the historical JSON key for Finalizer compatibility. Its value now
        # means deterministic reasons other than the coverage/lot reason.
        "hardNonAssortmentReasons": [item.as_dict() for item in hard_non_coverage],
        "llmReasonCandidates": llm_reason_candidates,
        "additionalReasons": additional_reasons,
        "llmDecision": llm_decision.model_dump() if llm_decision else None,
        "marketResearchReasonSuppressed": market_research_suppressed,
    }
    return fields, meta, decision


def build_decision_prompt(
    *, fields: dict[str, Any], hard_reasons: list[HardReason], checks: dict[str, Any],
    product_check: dict[str, Any], all_text: str, maximum_text_chars: int,
    report_id: int | None = None,
) -> str:
    market_research_suppressed = report_id in {1, 2}
    llm_reasons = [
        reason
        for reason in REASONS
        if reason not in {
            DEADLINE_REASON,
            ASSORTMENT_REASON,
            INDIVISIBLE_REASON,
            HIGH_VOLTAGE_REASON,
            PRICE_REASON,
            ACTUAL_COST_REASON,
        }
        and not (market_research_suppressed and reason == MARKET_RESEARCH_REASON)
    ]
    available_reasons = [
        reason
        for reason in REASONS
        if reason not in {
            DEADLINE_REASON,
            ASSORTMENT_REASON,
            INDIVISIBLE_REASON,
            HIGH_VOLTAGE_REASON,
            PRICE_REASON,
            ACTUAL_COST_REASON,
        }
        and not (market_research_suppressed and reason == MARKET_RESEARCH_REASON)
    ]
    context = {
        "rulesVersion": "2026-06 / пользовательский справочник причин",
        "reasonOptions": available_reasons,
        "productCheck": product_check,
        "hardReasons": [reason.as_dict() for reason in hard_reasons],
        **checks,
    }
    return f"""
Ты принимаешь решение о согласовании или отклонении тендера. Только JSON.
Допустимые причины: {llm_reasons}

Правила:
- Проверь каждый пункт справочника по тексту документации и извлечённым фактам.
- Не придумывай основание; каждое основание требует evidence.
- hardReasons рассчитаны кодом и не могут быть отменены; их можно только дополнить.
- Наличие hardReasons не означает, что анализ можно закончить. Даже если уже есть обязательный отказ
  по комплектованию лота, обязательно проверь все остальные причины справочника.
- detectedReasons должен содержать полный список всех подтверждённых недетерминированных причин,
  а не только основную. Если причин несколько, верни их все с evidence.
- Если вместе с детерминированной причиной «{COVERAGE_REASON}» найдена другая подтверждённая причина,
  верни её в detectedReasons и используй как primaryReason среди причин, доступных LLM.
  Код сам сохранит причину комплектования лота как дополнительную.
- Не останавливай проверку после анализа товарного ассортимента: независимо проверь остальные основания
  отказа по документации.
- Причина «{PRICE_REASON}» полностью детерминирована; LLM запрещено выбирать её.
- Для reportId 1 (223-ФЗ) и reportId 2 (гос/44-ФЗ) порог 1 млн проверяется только по
  initialPrice. Расчётная стоимость productCheck носит справочный характер и не является причиной отказа.
- Для reportId 3 (коммерция) initialPrice не используется для порога 1 млн. Порог применяется
  только к расчётной сумме Qdrant-цена × количество при productCheck.coverageApproved=true и
  priceEvaluationComplete=true.
- Нулевая/пустая/null начальная цена сама по себе не является отказом.
- Отсрочка оплаты является причиной отказа при 90 днях и более (`>= 90`). Правило применяется
  к рабочим, календарным и дням без уточнения типа.
- Причина «{COVERAGE_REASON}» полностью детерминирована; LLM запрещено выбирать её.
- Причина «{HIGH_VOLTAGE_REASON}» полностью детерминирована по товарным позициям; LLM запрещено выбирать её.
- Для неделимого лота проверка проходит при coverage не менее 80% (`>= 80`).
- Для делимого лота проверка проходит, если найдена хотя бы одна поставляемая позиция.
- Если делимость лота не подтверждена, применяется безопасное правило неделимого лота: coverage >= 80%.
- Причина менее 3 дней полностью детерминирована только по remainingDays < 3; значение 3 проходит.
- Упоминание Росатома/АЭС не равно атомной приёмке без прямой формулировки.
- Упоминание 275-ФЗ/Минобороны не равно военной приёмке без прямой формулировки.
- Одиночное слово «монтаж» не является поставкой с работами; нужна обязанность поставщика.
- Удалённые территории: Калининград/Калининградская область, Республика Дагестан и
  Республика Саха (Якутия).
- Ошибка парсинга не равна отсутствию документации.
- Причина «{MARKET_RESEARCH_REASON}» применяется только для коммерческих закупок (reportId=3).
  Для 223-ФЗ (reportId=1) и 44/94-ФЗ (reportId=2) маркетинговое исследование само по себе
  не является причиной отказа и не должно возвращаться в primaryReason/detectedReasons.
  Если документы отсутствуют, действует отдельная детерминированная причина отсутствия документации.

Поля: {fields}
Контекст: {context}
Текст: {all_text[:maximum_text_chars]}
""".strip()
