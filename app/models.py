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
    table: str = ""
    row: int | None = Field(default=None, ge=1)
    page: int | None = Field(default=None, ge=1)
    lotNumber: str = ""
    positionNumber: str = ""
    sectionRole: str = "other"
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

    @field_validator(
        "fileName",
        "sheet",
        "table",
        "productColumn",
        "quantityColumn",
        "unitColumn",
        "productHeader",
        "quantityHeader",
        "unitHeader",
        "lotNumber",
        "positionNumber",
        "sectionRole",
        mode="before",
    )
    @classmethod
    def normalize_nullable_source_reference_strings(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("extractionMethod", mode="before")
    @classmethod
    def normalize_extraction_method(cls, value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if text in {"excel", "xls", "xlsx", "spreadsheet", "deterministic"}:
            return "excel_deterministic"
        if text in {"seldon", "seldon_api", "structured"}:
            return "seldon_structured"
        return text if text in {"excel_deterministic", "seldon_structured", "llm"} else "llm"


class ProductCharacteristic(BaseModel):
    name: str = ""
    value: str
    normalizedName: str = ""
    evidence: str = ""
    sourceReference: dict[str, Any] = Field(default_factory=dict)
    confidence: Literal["low", "medium", "high"] = "medium"
    targetPositionKey: str = ""
    associationConfidence: float = Field(default=0.0, ge=0.0, le=1.0)
    associationMethod: Literal[
        "same_row",
        "continuation_row",
        "position_key",
        "article",
        "model",
        "lot_position",
        "position_number",
        "exact_product_name",
        "llm_consolidation",
        "unresolved",
    ] = "unresolved"
    associationStatus: Literal[
        "confirmed",
        "unverified",
        "conflicting",
    ] = "unverified"

    @field_validator(
        "name",
        "value",
        "normalizedName",
        "evidence",
        "targetPositionKey",
        mode="before",
    )
    @classmethod
    def normalize_characteristic_text(cls, value: Any) -> str:
        return str(value or "").strip()


class CharacteristicConflictValue(BaseModel):
    value: str
    sourceReference: dict[str, Any] = Field(default_factory=dict)
    confidence: Literal["low", "medium", "high"] = "medium"


class CharacteristicConflict(BaseModel):
    normalizedName: str
    values: list[CharacteristicConflictValue] = Field(default_factory=list, min_length=2)
    resolution: Literal["unresolved", "selected"] = "unresolved"
    selectedValue: str = ""


class TenderPosition(BaseModel):
    candidateId: str = ""
    positionKey: str = ""
    lotNumber: str = ""
    positionNumber: str = ""
    model: str = ""
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
    sourceCells: dict[str, str] = Field(default_factory=dict)
    characteristics: list[ProductCharacteristic] = Field(default_factory=list, max_length=100)
    characteristicConflicts: list[CharacteristicConflict] = Field(default_factory=list, max_length=50)
    searchCharacteristics: list[str] = Field(default_factory=list, max_length=5)
    searchCategory: str = ""
    searchCategoryCode: str = "other"
    searchQueries: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("sourceCells", mode="before")
    @classmethod
    def normalize_source_cells(cls, value: Any) -> dict[str, str]:
        return SpreadsheetRow(row=1, cells=value).cells

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
    products: list[TenderPosition] = Field(default_factory=list, max_length=1000)
    warnings: list[str] = Field(default_factory=list)


class ProductHierarchyAssignment(BaseModel):
    positionIndex: int = Field(ge=1)
    role: Literal["purchase_item", "component", "ambiguous"] = "ambiguous"
    parentPositionIndex: int | None = Field(default=None, ge=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""


class ProductHierarchyResponse(BaseModel):
    assignments: list[ProductHierarchyAssignment] = Field(default_factory=list, max_length=200)
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
        max_length=200,
    )
    warnings: list[str] = Field(default_factory=list)


class SpreadsheetCandidateDecision(BaseModel):
    candidateId: str
    decision: Literal["KEEP", "CORRECT", "REMOVE", "NEW"]
    normalizedProduct: str | None = None
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    duplicateOfCandidateId: str | None = None


class SpreadsheetCandidateReviewResponse(BaseModel):
    decisions: list[SpreadsheetCandidateDecision] = Field(default_factory=list, max_length=1000)
    warnings: list[str] = Field(default_factory=list)


class DocumentFieldCandidate(BaseModel):
    fieldName: str
    value: Any = None
    confidence: Literal["low", "medium", "high"] = "low"
    evidence: str = ""
    sourceReference: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence", mode="before")
    @classmethod
    def trim_evidence(cls, value: Any) -> str:
        return str(value or "").strip()[:500]


class DocumentReasonHit(BaseModel):
    reason: str
    evidence: str = ""
    confidence: Literal["low", "medium", "high"] = "low"
    sourceReference: dict[str, Any] = Field(default_factory=dict)

    @field_validator("evidence", mode="before")
    @classmethod
    def trim_evidence(cls, value: Any) -> str:
        return str(value or "").strip()[:500]


class DocumentCharacteristicSet(BaseModel):
    productHint: str = ""
    brand: str = ""
    article: str = ""
    model: str = ""
    lotNumber: str = ""
    positionNumber: str = ""
    targetPositionKey: str = ""
    associationConfidence: float = Field(default=0.0, ge=0.0, le=1.0)
    associationMethod: str = "unresolved"
    characteristics: list[ProductCharacteristic] = Field(default_factory=list, max_length=100)
    evidence: str = ""
    sourceReference: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "productHint",
        "brand",
        "article",
        "model",
        "lotNumber",
        "positionNumber",
        "targetPositionKey",
        "associationMethod",
        "evidence",
        mode="before",
    )
    @classmethod
    def normalize_characteristic_set_text(cls, value: Any) -> str:
        return str(value or "").strip()


class DocumentAnalysisUnit(BaseModel):
    unitId: str
    sourceType: Literal["seldon_page", "document", "spreadsheet"] = "document"
    documentIndex: int | None = None
    fileName: str = ""
    documentKind: str = "document"
    partIndex: int = Field(default=1, ge=1)
    partTotal: int = Field(default=1, ge=1)
    text: str = ""
    spreadsheetCandidates: list[dict[str, Any]] = Field(default_factory=list)
    batchedDocumentUnits: list["DocumentAnalysisUnit"] = Field(default_factory=list, exclude=True)
    inputSha256: str = ""


class DocumentAnalysisResponse(BaseModel):
    products: list[TenderPosition] = Field(default_factory=list, max_length=1000)
    characteristicSets: list[DocumentCharacteristicSet] = Field(default_factory=list, max_length=1000)
    reasonHits: list[DocumentReasonHit] = Field(default_factory=list, max_length=50)
    fieldCandidates: list[DocumentFieldCandidate] = Field(default_factory=list, max_length=80)
    analysisIncomplete: bool = False
    warnings: list[str] = Field(default_factory=list)


class DocumentAnalysisResult(DocumentAnalysisResponse):
    unitId: str
    inputSha256: str = ""
    sourceType: str = "document"
    fileName: str = ""
    partIndex: int = 1
    partTotal: int = 1


class TenderConsolidationResponse(BaseModel):
    products: list[TenderPosition] = Field(default_factory=list, max_length=1000)
    characteristicSets: list[DocumentCharacteristicSet] = Field(default_factory=list, max_length=1000)
    reasonHits: list[DocumentReasonHit] = Field(default_factory=list, max_length=80)
    fieldCandidates: list[DocumentFieldCandidate] = Field(default_factory=list, max_length=120)
    incompleteUnitIds: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CharacteristicNumeric(BaseModel):
    min: float | None = None
    max: float | None = None
    nominal: float | None = None
    unit: str | None = None
    values: list[str] = Field(default_factory=list, max_length=30)


class SemanticCharacteristic(BaseModel):
    id: str
    name: str = ""
    original_value: str = ""
    semantic_role: Literal[
        "IDENTIFIER",
        "VARIANT_SELECTOR",
        "SIZE_OR_DIMENSION",
        "PACKAGING",
        "PERFORMANCE",
        "INTERFACE_OR_STANDARD",
        "QUALITY_OR_PRECISION",
        "ENVIRONMENT",
        "TEMPORAL",
        "QUANTITY",
        "ENUMERATION",
        "OTHER",
    ] = "OTHER"
    value_form: Literal[
        "EXACT",
        "ACCEPTABLE_RANGE",
        "MINIMUM",
        "MAXIMUM",
        "TOLERANCE",
        "ENUMERATED",
        "INTERFACE_RANGE",
        "TEMPERATURE_RANGE",
        "TEXT",
    ] = "TEXT"
    search_token: str = ""
    already_encoded_in_name: bool = False
    numeric: CharacteristicNumeric = Field(default_factory=CharacteristicNumeric)


class ProductSemanticClassification(BaseModel):
    position_index: int = Field(ge=1)
    normalized_product: str = ""
    category: str = ""
    identifier_strength: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    characteristics: list[SemanticCharacteristic] = Field(default_factory=list, max_length=100)


class ProductSemanticBatchResponse(BaseModel):
    results: list[ProductSemanticClassification] = Field(default_factory=list, max_length=200)
    warnings: list[str] = Field(default_factory=list)


class SkuCharacteristicDecision(BaseModel):
    id: str
    sku_importance: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"] = "NONE"
    usage: Literal[
        "SEARCH_PRIMARY",
        "SEARCH_SECONDARY",
        "VALIDATION_ONLY",
        "IGNORE",
    ] = "IGNORE"
    reason: str = ""


class ProductSkuClassification(BaseModel):
    position_index: int = Field(ge=1)
    decisions: list[SkuCharacteristicDecision] = Field(default_factory=list, max_length=100)
    selected_for_search: list[str] = Field(default_factory=list, max_length=5)


class ProductSkuBatchResponse(BaseModel):
    results: list[ProductSkuClassification] = Field(default_factory=list, max_length=200)
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
    positionKey: str = ""
    product: str
    productQuery: str
    brand: str = ""
    article: str = ""
    quantity: float | None = None
    unit: str = ""
    analogsAllowed: bool | None = None
    evidence: str = ""
    requirements: str = ""
    characteristics: list[ProductCharacteristic] = Field(default_factory=list)
    searchCharacteristics: list[str] = Field(default_factory=list)
    searchCategory: str = ""
    searchCategoryCode: str = "other"
    searchQueries: list[str] = Field(default_factory=list)
    characteristicConflicts: list[CharacteristicConflict] = Field(default_factory=list)
    documentUnitPriceRub: float | None = None
    documentLineTotalRub: float | None = None
    documentCurrency: str | None = None
    documentPriceEvidence: str = ""
    documentPriceSource: DocumentPriceSource | None = None
    sourceReference: ProductSourceReference | None = None
    sourceCells: dict[str, str] = Field(default_factory=dict)
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


class HardReasonReview(BaseModel):
    reason: str
    verdict: Literal["confirm", "dismiss"]
    rationale: str
    confidence: Literal["low", "medium", "high"] = "medium"


class LlmDecision(BaseModel):
    decision: Literal["approve", "reject"]
    primaryReason: str | None = None
    detectedReasons: list[DecisionReason] = Field(default_factory=list)
    hardReasonReviews: list[HardReasonReview] = Field(default_factory=list)
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


class SpreadsheetRow(BaseModel):
    row: int = Field(ge=1)
    cells: dict[str, str] = Field(default_factory=dict)

    @field_validator("cells", mode="before")
    @classmethod
    def normalize_cells(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, str] = {}
        for raw_column, raw_value in value.items():
            column = str(raw_column or "").strip().upper()
            if not column or len(column) > 3 or not column.isalpha():
                continue
            text = (
                str(raw_value or "")
                .replace(chr(13), " ")
                .replace(chr(10), " ")
                .strip()
            )
            if text:
                normalized[column] = text[:4000]
        return normalized


class SpreadsheetTable(BaseModel):
    fileName: str = ""
    sheet: str
    headerRows: list[int] = Field(default_factory=list)
    headerMap: dict[str, str] = Field(default_factory=dict)
    headerLabels: dict[str, str] = Field(default_factory=dict)
    rows: list[SpreadsheetRow] = Field(default_factory=list)
    parserWarnings: list[str] = Field(default_factory=list)


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
    spreadsheetTables: list[SpreadsheetTable] = Field(default_factory=list)


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
