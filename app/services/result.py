from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models import NormalizedJob, TenderResult


REPORT_OMITTED_FIELD_KEYS = frozenset(
    {
        "federalLaw",
        "applicationSecurity",
        "contractSecurity",
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
        return "Делимый"
    if normalized in {"no", "false", "0", "нет"}:
        return "Неделимый"
    return ""


def _yes_no_display_value(value: Any) -> Any:
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "true", "1", "да"}:
        return "Да"
    if normalized in {"no", "false", "0", "нет"}:
        return "Нет"
    return value


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
    result_fields["toCode"] = job.to_code
    # Keep normalized machine values while calculating the tender, then expose
    # business labels only in the outward payload consumed by Daily.
    lot_divisible_label = _lot_divisible_report_value(fields.get("lotDivisible"))
    if lot_divisible_label:
        result_fields["lotDivisible"] = lot_divisible_label
    if "stateDefenseOrder" in fields:
        result_fields["stateDefenseOrder"] = _yes_no_display_value(
            fields.get("stateDefenseOrder")
        )
    if "nationalRegime" in fields:
        result_fields["nationalRegime"] = _national_regime_display_value(
            fields.get("nationalRegime")
        )
    report_fields = {
        key: value
        for key, value in job.report_fields.items()
        if key not in REPORT_OMITTED_HEADERS
    }
    if lot_divisible_label:
        # Downstream XLSX generation uses the original Russian Daily headers.
        # Keep that column in sync with the normalized decision field.
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
        toCode=job.to_code,
        lawCode=job.law_code,
        sectionName=job.section_name,
        filterName=job.filter_name,
        reportFields=report_fields,
        sourceTender=job.seldon_purchase,
        processedAt=datetime.now(timezone.utc),
    )
    return result.model_dump(mode="json")
