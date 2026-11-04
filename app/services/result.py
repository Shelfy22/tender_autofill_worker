from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import NormalizedJob, TenderResult
from app.services.field_policy import (
    disabled_field_headers,
    is_tender_field_enabled,
    remove_disabled_tender_fields,
)


REPORT_OMITTED_FIELD_KEYS = frozenset(
    {
        "federalLaw",
        "applicationSecurity",
        # Source-only Daily/Seldon columns; they must not be duplicated by AI.
        "submissionDeadlineDate",
        "submissionDeadlineTime",
        "tenderUrlSource",
        "initialPrice",
        "finalPrice",
        # Retained internally for calculations but not emitted to Daily.
        "dateCreated",
        "tenderGroup",
        "counterpartyCode",
        "counterpartyName",
        "counterpartyInn",
        "counterpartyKpp",
        "counterpartyCkg",
        "counterpartyPotential",
        "deal",
        "contract",
        "counterpartyNote",
        "op",
        "legalEntity",
        "tenderWonDate",
        # Legacy aliases must not reintroduce customer data in outward JSON.
        "counterparty",
        "inn",
        "kpp",
    }
)

REPORT_OMITTED_HEADERS = frozenset(
    {
        "Дата заведения",
        "Тендерная группа",
        "Обеспечение заявки",
        "Обеспечение контракта",
        "Обеспечение исполнения контракта",
        "Федеральный закон",
        "Код контрагента",
        "Название контрагента",
        "ИНН контрагента",
        "КПП контрагента",
        "ЦКГ",
        "Потенциал",
        "Сделка",
        "Договор",
        "Примечание к контрагенту",
        "ОП",
        "Юридическое лицо ЭТМ",
        "Тендер выигран",
    }
)


def _lot_divisible_report_value(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "true", "1", "да", "делимый"}:
        return "yes"
    if normalized in {"no", "false", "0", "нет", "неделимый"}:
        return "no"
    return ""


def _yes_no_code_value(value: Any) -> Any:
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "true", "1", "да"}:
        return "yes"
    if normalized in {"no", "false", "0", "нет"}:
        return "no"
    return value


# Processing rules keep readable Russian labels.  The outward Daily payload is
# imported into a system whose dictionaries accept these stable reference codes.
_TENDER_STATUS_CODES = {
    "Загружен Seldon": "110",
    "Согласовано ИИ": "120",
    "Отказано ИИ": "130",
}

_TENDER_STATUS_REASON_CODES = {
    "Дубль": "cp_double",
    # The real threshold remains 1 mln rubles; the target dictionary key has a
    # historical ls300 suffix and must nevertheless be used for the import.
    "Коммерческие условия. НМЦК менее 1 млн руб.": "ku_nmck_ls300",
    "Коммерческие условия. НМЦК менее фактической стоимости": "ku_nmck_lsfp",
    "Коммерческие условия. Не проходим по сроку поставки": "cp_ku_delivery_notime",
    "Коммерческие условия. Оплата Покупателем после оплаты Генподрядчиком / Госзаказчиком": "ku_payment_aftgosgen",
    "Коммерческие условия. Отсрочка платежа 90 дней и более": "ku_postpay_mr120",
    "Коммерческие условия. Поставка в удаленные территории": "cp_ku_delivery_far",
    "Коммерческие условия. Консигнация / Хранение у Покупателя за счет Поставщика": "cp_ku_storage_bbs",
    "Номенклатура. Лот неделимый. Не можем скомплектовать более 20% номенклатуры": "nom_nedelimlot_1s20",
    "Номенклатура. Оборудование 35 кВ и выше": "nom_equip_mr35",
    "Номенклатура. Частотный привод 6–10 кВ": "nom_fp_610",
    "Номенклатура. Ремкомплект / ЗИП / Продукция по чертежу": "nom_rgzipdesign",
    "Номенклатура. Военная приёмка": "cp_nom_accept_military",
    "Номенклатура. Атомная приёмка": "cp_nom_accept_atomic",
    "Номенклатура. Поставка с работами": "cp_nom_delivery_wl",
    "Оргвопросы. На момент согласования менее 3 рабочих дней до подачи заявки": "org_term_ls3",
    "Оргвопросы. Отказ организатора от проведения тендера": "cp_org_tender_no",
    "Оргвопросы. Опрос рынка / Мониторинг / Анализ рынка / Анонс / КИМ": "cp_org_analiz",
    "Прочее": "cp_other",
}


def _tender_status_code(value: Any) -> Any:
    raw = str(value or "").strip()
    return _TENDER_STATUS_CODES.get(raw, value)


def _tender_status_reason_code(value: Any) -> Any:
    raw = str(value or "").strip()
    normalized = raw.casefold().replace("ё", "е")
    return next(
        (code for label, code in _TENDER_STATUS_REASON_CODES.items()
         if label.casefold().replace("ё", "е") == normalized),
        value,
    )


def _department_code(value: Any) -> str | None:
    """Format the target-system department dictionary value once at export."""
    raw = str(value or "").strip()
    if not raw:
        return None
    return raw if raw.casefold().startswith("dep_") else f"Dep_{raw}"


def _national_regime_display_value(value: Any) -> Any:
    labels = {
        "ban": "запрет",
        "restriction": "ограничение",
        "preference": "преимущество",
        "no": "Нет",
    }
    raw = str(value or "").strip()
    if not raw:
        return value
    parts = [part.strip() for part in raw.split(";")]
    return "; ".join(labels.get(part.casefold(), part) for part in parts if part)


def build_result_json(
    job: NormalizedJob,
    *,
    fields: dict[str, Any],
    meta: dict[str, Any],
    product_check: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    warnings: list[str],
    logs: list[dict[str, Any]],
    debug: dict[str, Any] | None,
) -> dict[str, Any]:
    result_fields = {
        key: value
        for key, value in fields.items()
        if key not in REPORT_OMITTED_FIELD_KEYS
    }
    result_meta = {
        key: value
        for key, value in meta.items()
        if key not in REPORT_OMITTED_FIELD_KEYS
    }
    exported_to_code = _department_code(job.to_code)
    result_fields["toCode"] = exported_to_code
    # Keep readable values inside the processing pipeline, then expose only
    # target-system dictionary codes in the outward Daily payload.
    lot_divisible_label = _lot_divisible_report_value(fields.get("lotDivisible"))
    if lot_divisible_label:
        result_fields["lotDivisible"] = lot_divisible_label
    if "stateDefenseOrder" in fields:
        result_fields["stateDefenseOrder"] = _yes_no_code_value(
            fields.get("stateDefenseOrder")
        )
    if "nationalRegime" in fields and is_tender_field_enabled("nationalRegime"):
        result_fields["nationalRegime"] = _national_regime_display_value(
            fields.get("nationalRegime")
        )
    if "tenderStatus" in fields:
        result_fields["tenderStatus"] = _tender_status_code(fields.get("tenderStatus"))
    if "tenderStatusReason" in fields:
        result_fields["tenderStatusReason"] = _tender_status_reason_code(
            fields.get("tenderStatusReason")
        )
    remove_disabled_tender_fields(result_fields, result_meta)
    report_fields = {
        key: value
        for key, value in job.report_fields.items()
        if key not in REPORT_OMITTED_HEADERS
        and key not in disabled_field_headers()
        and is_tender_field_enabled(key)
    }
    if exported_to_code:
        report_fields["Код ТО"] = exported_to_code
        report_fields["toCode"] = exported_to_code
    if lot_divisible_label:
        # Keep both original Daily aliases synchronized with the dictionary code.
        report_fields["Лот делимый"] = lot_divisible_label
        report_fields["lotDivisible"] = lot_divisible_label
    result = TenderResult(
        fields=result_fields,
        meta=result_meta,
        productCheck=product_check,
        decision=decision,
        warnings=list(dict.fromkeys(str(warning) for warning in warnings if str(warning).strip())),
        logs=logs,
        debug=debug,
        reportId=job.report_id,
        seldonId=job.seldon_id,
        etpId=job.etp_id,
        purchaseType=job.purchase_type,
        purchaseNumber=str(job.seldon_purchase.get("notificationNumber") or job.etp_id or "") or None,
        tenderUrl=job.tender_url,
        batchId=job.batch_id,
        batchDate=job.batch_date,
        rowNumber=job.row_number,
        jobRecordKey=job.job_record_key,
        remainingDays=job.remaining_days,
        toCode=exported_to_code,
        lawCode=job.law_code,
        sectionName=job.section_name,
        filterName=job.filter_name,
        reportFields=report_fields,
        sourceTender=job.seldon_purchase,
        processedAt=datetime.now(timezone.utc),
    )
    return result.model_dump(mode="json")
