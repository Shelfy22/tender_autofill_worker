from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobDispatch(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    job_record_key: str = Field(alias="jobRecordKey", min_length=1)
    batch_id: str = Field(alias="batchId", min_length=1)
    report_id: int | None = Field(default=None, alias="reportId")
    seldon_id: str | int | None = Field(default=None, alias="seldonId")
    etp_id: str | None = Field(default=None, alias="etpId")
    report_fields: dict[str, Any] = Field(default_factory=dict, alias="reportFields")

    @field_validator("job_record_key", "batch_id")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class BatchDispatchRequest(BaseModel):
    jobs: list[JobDispatch] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_record_keys(self) -> "BatchDispatchRequest":
        keys = [job.job_record_key for job in self.jobs]
        if len(keys) != len(set(keys)):
            raise ValueError("jobs must have unique jobRecordKey values")
        return self


class AcceptedJob(BaseModel):
    jobRecordKey: str
    taskId: str


class RejectedJob(BaseModel):
    jobRecordKey: str
    reason: str


class BatchDispatchResponse(BaseModel):
    status: Literal["accepted", "partially_accepted", "rejected"]
    accepted: int
    rejected: int
    jobs: list[AcceptedJob]
    rejectedJobs: list[RejectedJob] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    postgres: bool
    redis: bool
    version: str


class FieldValue(BaseModel):
    value: Any = None
    confidence: Literal["low", "medium", "high"] = "low"
    source: str | None = None
    evidence: str | None = None


class ExtractedFieldsResponse(BaseModel):
    fields: dict[str, FieldValue | Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_fields_list(cls, value: Any) -> Any:
        """Accept the legacy LLM list shape without weakening final validation.

        Some providers return ``fields`` as [{fieldName, value, ...}] despite the
        requested object schema. Convert that representation deterministically;
        downstream field allow-list validation remains unchanged.
        """
        if not isinstance(value, dict) or not isinstance(value.get("fields"), list):
            return value

        data = dict(value)
        normalized: dict[str, Any] = {}
        for item in data["fields"]:
            if not isinstance(item, dict):
                continue
            field_name = next(
                (
                    str(item[key]).strip()
                    for key in ("fieldName", "field_name", "name", "key")
                    if item.get(key) is not None and str(item[key]).strip()
                ),
                "",
            )
            if not field_name or field_name in normalized:
                continue

            field = dict(item)
            for key in ("fieldName", "field_name", "name", "key"):
                field.pop(key, None)
            if "value" not in field and "fieldValue" in field:
                field["value"] = field.pop("fieldValue")
            normalized[field_name] = field

        data["fields"] = normalized
        return data


def _parse_money_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) >= 0 else None
    text = str(value).replace("\u00a0", " ").strip()
    text = "".join(char for char in text if char.isdigit() or char in ",.-").rstrip(".,")
    if not text:
        return None
    comma, dot = text.rfind(","), text.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal = "," if comma > dot else "."
        text = text.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif comma >= 0:
        decimals = len(text) - comma - 1
        text = text.replace(",", "." if 0 < decimals <= 2 else "")
    elif dot >= 0 and not (0 < len(text) - dot - 1 <= 2):
        text = text.replace(".", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number >= 0 else None


class DocumentPriceSource(BaseModel):
    fileName: str = ""
    sheet: str = ""
    row: int | None = None
    unitPriceColumn: str = ""
    lineTotalColumn: str = ""
    unitPriceHeader: str = ""
    lineTotalHeader: str = ""
    extractionMethod: Literal["excel_deterministic", "llm"] = "llm"

    @field_validator("extractionMethod", mode="before")
    @classmethod
    def normalize_extraction_method(cls, value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text in {
            "excel_deterministic",
            "excel",
            "xls",
            "xlsx",
            "spreadsheet",
            "deterministic",
        }:
            return "excel_deterministic"
        if text in {"llm", "ai", "model", "openai", "gpt", "gemini"}:
            return "llm"
        # Unknown values in this field come from an LLM response. Keep the job
        # running and record the conservative source classification instead of
        # failing all retries on a diagnostic-only field.
        return "llm"


class ProductSourceReference(BaseModel):
    """Coordinates of the cells that created a tender product candidate."""

    fileName: str = ""
    sheet: str = ""
    row: int | None = Field(default=None, ge=1)
    productColumn: str = ""
    quantityColumn: str = ""
    unitColumn: str = ""
    productHeader: str = ""
    quantityHeader: str = ""
    unitHeader: str = ""
    extractionMethod: Literal[
        "excel_deterministic",
        "seldon_structured",
        "llm",
    ] = "llm"

    @field_validator("extractionMethod", mode="before")
    @classmethod
    def normalize_extraction_method(cls, value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text in {"excel", "xls", "xlsx", "spreadsheet", "deterministic"}:
            return "excel_deterministic"
        if text in {"seldon", "seldon_api", "structured"}:
            return "seldon_structured"
        return text if text in {"excel_deterministic", "seldon_structured", "llm"} else "llm"


class TenderPosition(BaseModel):
    product: str
    productQuery: str | None = None
    brand: str = ""
    article: str = ""
    quantity: float | None = None
    unit: str = ""
    analogsAllowed: bool | None = None
    evidence: str = ""
    requirements: str = ""
    source: str = "llm"
    documentUnitPriceRub: float | None = None
    documentLineTotalRub: float | None = None
    documentCurrency: str | None = None
    documentPriceEvidence: str = ""
    documentPriceSource: DocumentPriceSource | None = None
    sourceReference: ProductSourceReference | None = None

    @field_validator("documentPriceSource", mode="before")
    @classmethod
    def normalize_empty_document_price_source(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        meaningful_fields = (
            "fileName",
            "sheet",
            "row",
            "unitPriceColumn",
            "lineTotalColumn",
            "unitPriceHeader",
            "lineTotalHeader",
        )
        if not any(
            value.get(field) is not None and value.get(field) != ""
            for field in meaningful_fields
        ):
            return None
        return value

    @field_validator("documentUnitPriceRub", "documentLineTotalRub", mode="before")
    @classmethod
    def normalize_document_money(cls, value: Any) -> float | None:
        return _parse_money_value(value)

    @field_validator("documentCurrency", mode="before")
    @classmethod
    def normalize_document_currency(cls, value: Any) -> str | None:
        text = str(value or "").strip().upper()
        if not text:
            return None
        return "RUB" if text in {"RUR", "РУБ", "РУБ.", "₽"} else text


class TenderPositionsResponse(BaseModel):
    products: list[TenderPosition] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list)


class ProductHierarchyAssignment(BaseModel):
    positionIndex: int = Field(ge=1)
    role: Literal["purchase_item", "component", "ambiguous"] = "ambiguous"
    parentPositionIndex: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


class ProductHierarchyResponse(BaseModel):
    assignments: list[ProductHierarchyAssignment] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list)


class ProductCandidateAssignment(BaseModel):
    positionIndex: int = Field(ge=1)
    role: Literal[
        "purchase_item",
        "component",
        "characteristic",
        "address",
        "service",
        "header",
        "duplicate",
        "ambiguous",
    ] = "ambiguous"
    duplicateOf: int | None = Field(default=None, ge=1)
    parentPositionIndex: int | None = Field(default=None, ge=1)
    canonicalName: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


class ProductCandidateAuditResponse(BaseModel):
    assignments: list[ProductCandidateAssignment] = Field(
        default_factory=list,
        max_length=100,
    )
    warnings: list[str] = Field(default_factory=list)


class ProductMatch(BaseModel):
    article: str | None = Field(default=None, alias="Артикул")
    link: str | None = Field(default=None, alias="Ссылка")
    name: str | None = Field(default=None, alias="Наименование")
    manufacturer: str | None = Field(default=None, alias="Производитель")
    median_price: float | None = Field(default=None, alias="Медианная цена")
    currency: str | None = Field(default=None, alias="Валюта")
    price_source: str = Field(default="", alias="Источник цены")
    rationale: str = Field(default="", alias="Обоснование")
    correspondence: Literal["Полное соответствие", "Аналог", "Товар не найден"] = Field(
        default="Товар не найден", alias="Соответствие"
    )
    qdrant_point_id: str | None = Field(default=None, alias="Qdrant point ID")
    product_id: str | None = Field(default=None, alias="ID товара")
    price_source_field: str = Field(default="", alias="Поле цены")
    price_aggregation: str = Field(default="", alias="Метод цены")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_catalog_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        canonical_price = data.get("Медианная цена")
        python_price = data.get("median_price")
        if (canonical_price is None or canonical_price == "") and (
            python_price is None or python_price == ""
        ):
            for key in ("Медианная цена, руб.", "Цена", "medianPrice", "price"):
                candidate = data.get(key)
                if candidate is not None and candidate != "":
                    data["Медианная цена"] = candidate
                    break
        if data.get("Валюта") is None or data.get("Валюта") == "":
            for key in ("currency", "currencyId", "currency_id"):
                candidate = data.get(key)
                if candidate is not None and candidate != "":
                    data["Валюта"] = candidate
                    break
        if data.get("Источник цены") is None or data.get("Источник цены") == "":
            for key in ("priceSource", "price_source"):
                candidate = data.get(key)
                if candidate is not None and candidate != "":
                    data["Источник цены"] = candidate
                    break
        return data

    @field_validator("median_price", mode="before")
    @classmethod
    def normalize_median_price(cls, value: Any) -> float | None:
        return _parse_money_value(value)


class CatalogSelection(BaseModel):
    selected_point_id: str | None = Field(default=None, alias="selectedPointId")
    correspondence: Literal["Полное соответствие", "Аналог", "Товар не найден"] = (
        "Товар не найден"
    )
    rationale: str = ""

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_selection_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "correspondence" not in data and "Соответствие" in data:
            data["correspondence"] = data["Соответствие"]
        if "rationale" not in data and "Обоснование" in data:
            data["rationale"] = data["Обоснование"]
        return data

    @field_validator("selected_point_id", mode="before")
    @classmethod
    def normalize_point_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class ProductMatchItem(BaseModel):
    positionIndex: int
    product: str
    productQuery: str
    brand: str = ""
    article: str = ""
    quantity: float | None = None
    unit: str = ""
    analogsAllowed: bool | None = None
    evidence: str = ""
    requirements: str = ""
    documentUnitPriceRub: float | None = None
    documentLineTotalRub: float | None = None
    documentCurrency: str | None = None
    documentPriceEvidence: str = ""
    documentPriceSource: DocumentPriceSource | None = None
    sourceReference: ProductSourceReference | None = None
    match: ProductMatch

    @field_validator("documentUnitPriceRub", "documentLineTotalRub", mode="before")
    @classmethod
    def normalize_document_money(cls, value: Any) -> float | None:
        """A malformed diagnostic price must not fail the whole tender job."""
        return _parse_money_value(value)

    @field_validator("documentCurrency", mode="before")
    @classmethod
    def normalize_document_currency(cls, value: Any) -> str | None:
        text = str(value or "").strip().upper()
        if not text:
            return None
        return "RUB" if text in {"RUR", "РУБ", "РУБ.", "₽"} else text


class DecisionReason(BaseModel):
    reason: str
    evidence: str = ""
    confidence: Literal["low", "medium", "high"] = "low"


class LlmDecision(BaseModel):
    decision: Literal["approve", "reject"]
    primaryReason: str | None = None
    detectedReasons: list[DecisionReason] = Field(default_factory=list)
    note: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"


class JobClaim(BaseModel):
    record_key: str
    batch_id: str
    attempt: int
    input_json: dict[str, Any]
    report_fields: dict[str, Any]
    report_id: int | None = None
    seldon_id: str | None = None
    etp_id: str | None = None


class NormalizedJob(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_record_key: str
    batch_id: str
    batch_date: str | None = None
    row_number: int | None = None
    report_id: int
    purchase_type: str | None = None
    seldon_id: str | None = None
    etp_id: str | None = None
    to_code: str | None = None
    law_code: str | None = None
    section_name: str | None = None
    filter_name: str | None = None
    remaining_days: float | None = None
    report_fields: dict[str, Any] = Field(default_factory=dict)
    seldon_purchase: dict[str, Any] = Field(default_factory=dict)
    tender_url: str | None = None
    source_file: str | None = None
    seldon_token: str | None = None
    attempt: int = 1


class ParsedDocument(BaseModel):
    documentIndex: int
    documentUrl: str = ""
    fileName: str
    originalFileName: str = ""
    documentKind: str = "document"
    fileExtension: str = ""
    mimeType: str = ""
    fileSize: int = 0
    parserRoute: str = ""
    extractedFromArchive: bool = False
    parentArchiveFileName: str = ""
    text: str = ""
    textLength: int = 0
    textQualityOk: bool = False
    textPreview: str = ""
    parserStatus: str = "not_parsed"
    parserWarning: str = ""
    parserError: str = ""


class TenderResult(BaseModel):
    fields: dict[str, Any]
    meta: dict[str, Any]
    productCheck: dict[str, Any] | None
    decision: dict[str, Any] | None
    warnings: list[str]
    logs: list[dict[str, Any]]
    debug: dict[str, Any] | None
    reportId: int | None
    seldonId: str | None
    etpId: str | None
    purchaseType: str | None
    purchaseNumber: str | None
    tenderUrl: str | None
    batchId: str | None
    batchDate: str | None
    rowNumber: int | None
    jobRecordKey: str | None
    remainingDays: float | None
    toCode: str | None
    lawCode: str | None
    sectionName: str | None
    filterName: str | None
    reportFields: dict[str, Any] | None
    sourceTender: dict[str, Any]
    processedAt: datetime


JsonObject = dict[str, Any]
DateLike = date | datetime | str
