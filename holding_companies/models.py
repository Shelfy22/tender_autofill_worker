from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.models import TenderPosition


class HoldingTenderInput(BaseModel):
    """Text already extracted by n8n from an e-mail and its single attachment."""

    tenderId: str = Field(min_length=1, max_length=128)
    emailText: str = Field(min_length=1, max_length=1_000_000)
    attachmentText: str = Field(default="", max_length=1_000_000)
    attachmentName: str = Field(default="", max_length=512)
    receivedAt: str | None = Field(default=None, max_length=64)

    @field_validator("tenderId", "emailText", "attachmentText", "attachmentName", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return str(value or "").strip()


class HoldingTenderExtraction(BaseModel):
    """The single first-pass LLM response for a holding-company tender."""

    proposalStartDate: str | None = Field(default=None, max_length=64)
    proposalEndDate: str | None = Field(default=None, max_length=64)
    holdingCompany: str = Field(default="", max_length=512)
    supplyConditions: str = Field(default="", max_length=4_000)
    sourceUrl: str | None = Field(default=None, max_length=4_000)
    paymentDelay: str = Field(default="", max_length=1_000)
    # Internal decision facts. They are not report columns but make the two
    # deterministic holding-company rules auditable.
    deliveryEndDate: str | None = Field(default=None, max_length=64)
    lotDivisible: bool | None = None
    tenderStatus: str = Field(default="Согласовано", max_length=64)
    tenderStatusReason: str = Field(default="", max_length=1_000)
    statusReasonEvidence: str = Field(default="", max_length=2_000)
    tenderStatusNote: str = Field(default="", max_length=4_000)
    deliveryNote: str = Field(default="", max_length=4_000)
    products: list[TenderPosition] = Field(default_factory=list, max_length=500)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class HoldingTenderResult(BaseModel):
    tenderId: str
    fields: dict[str, object]
    productCheck: dict[str, object]
    warnings: list[str] = Field(default_factory=list)
