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


class TenderPositionsResponse(BaseModel):
    products: list[TenderPosition] = Field(default_factory=list, max_length=100)
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

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("median_price", mode="before")
    @classmethod
    def normalize_median_price(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
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
        return float(text)


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
    match: ProductMatch


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
