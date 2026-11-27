from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.services.catalog import CatalogMatcher
from app.services.coverage import summarize_product_coverage
from app.services.llm import LlmClient
from app.services.product_matching_export import build_product_matching_workbook
from app.services.product_pipeline import process_products
from holding_companies.models import HoldingTenderExtraction, HoldingTenderInput, HoldingTenderResult
from holding_companies.policy import resolve_status
from holding_companies.prompt import build_extraction_prompt


@dataclass(frozen=True)
class HoldingCompanyRun:
    result: HoldingTenderResult
    workbook: bytes


def _combined_text(payload: HoldingTenderInput, max_chars: int) -> tuple[str, str]:
    email_text = payload.emailText[:max_chars]
    remaining = max(0, max_chars - len(email_text))
    attachment_text = payload.attachmentText[:remaining]
    return email_text, attachment_text


def _status_note(product_check: dict[str, object]) -> str:
    coverage = str(product_check.get("statusNoteSummary") or "").strip()
    estimated_total = product_check.get("supplyTotalPriceFormatted")
    complete = product_check.get("priceEvaluationComplete") is True
    if complete and estimated_total:
        price = f"Ориентировочная сумма по медианным ценам: {estimated_total}."
        return " ".join(part for part in (coverage, price) if part)
    return coverage


class HoldingCompanyService:
    """One-message holding-tender extraction followed by the existing catalog flow."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, payload: HoldingTenderInput) -> HoldingCompanyRun:
        email_text, attachment_text = _combined_text(
            payload,
            self.settings.max_combined_text_chars,
        )
        llm = LlmClient(self.settings, attempt=1)
        extraction = llm.json_call(
            system=(
                "Ты извлекаешь данные закрытой закупки холдинга и её товарные позиции. "
                "Работай только с переданным текстом. Не добавляй факты от себя."
            ),
            prompt=build_extraction_prompt(
                email_text=email_text,
                attachment_name=payload.attachmentName,
                attachment_text=attachment_text,
            ),
            schema=HoldingTenderExtraction,
            operation="holding_company_extract",
        )

        warnings = list(extraction.warnings)
        matches = []
        catalog = CatalogMatcher(self.settings, llm)
        try:
            if extraction.products:
                processed = process_products(
                    llm,
                    catalog,
                    self.settings,
                    extraction.products,
                    warnings,
                )
                matches = processed.matches
                warnings.extend(
                    [
                        *processed.validation.get("warnings", []),
                        *processed.search.get("warnings", []),
                        *processed.final_validation.get("warnings", []),
                    ]
                )
            product_check = summarize_product_coverage(
                matches,
                supply_value_threshold_enabled=False,
                lot_divisible=None,
                source_position_count=len(extraction.products),
                position_limit=self.settings.catalog_match_max_positions,
            )
            workbook = build_product_matching_workbook(
                product_check,
                selected_catalog_params=[item.match.catalog_params for item in matches],
            )
        finally:
            catalog.close()

        decision = resolve_status(
            extraction,
            _status_note(product_check),
            product_check=product_check,
            received_at=payload.receivedAt,
        )
        warnings.extend(decision.warnings)
        warnings = list(dict.fromkeys(str(item).strip() for item in warnings if str(item).strip()))
        fields: dict[str, object] = {
            "proposalStartDate": extraction.proposalStartDate,
            "proposalEndDate": extraction.proposalEndDate,
            "holdingCompany": extraction.holdingCompany,
            "supplyConditions": extraction.supplyConditions,
            "sourceUrl": extraction.sourceUrl,
            "paymentDelay": extraction.paymentDelay,
            "tenderStatus": decision.status,
            "tenderStatusReason": decision.reason,
            "tenderStatusNote": decision.note,
            "deliveryNote": extraction.deliveryNote,
        }
        result = HoldingTenderResult(
            tenderId=payload.tenderId,
            fields=fields,
            productCheck=product_check,
            warnings=warnings,
        )
        return HoldingCompanyRun(result=result, workbook=workbook)
