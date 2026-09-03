from types import SimpleNamespace

from app.config import Settings
from app.models import (
    DecisionReason,
    DocumentAnalysisResponse,
    DocumentFieldCandidate,
    DocumentReasonHit,
    JobClaim,
    LlmDecision,
    ParsedDocument,
    ProductMatch,
    ProductMatchItem,
    TenderConsolidationResponse,
    TenderPosition,
)
from app.pipeline import TenderPipeline
from app.services.customer import ActualCustomerResponse
from app.services.decision import REPAIR_KIT_REASON


class FakeSeldonDocuments:
    documents: list[dict[str, object]] = []
    warnings: list[str] = []

    def decision_context(self) -> dict[str, object]:
        return {"documentsFound": 1, "documentationUnavailable": False}


class FakeSeldonClient:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def get_token(self, token: str | None) -> str:
        return token or "token"

    def get_purchase_documents(self, *_: object) -> FakeSeldonDocuments:
        return FakeSeldonDocuments()

    def close(self) -> None:
        pass


class FakeDocumentProcessor:
    downloaded_total = 100

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def process_all(self, *_: object) -> tuple[list[ParsedDocument], list[str]]:
        return [
            ParsedDocument(
                documentIndex=1,
                fileName="technical.pdf",
                documentKind="technical",
                text="Поставка анализатора. В комплект поставки входит ЗИП.",
                textQualityOk=True,
            )
        ], []

    def fetch_tender_html(self, *_: object) -> tuple[str, list[str]]:
        return "", []

    def close(self) -> None:
        pass


class FakeIProClient:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def lookup(self, fields: dict[str, object], meta: dict[str, object]) -> tuple[dict[str, object], dict[str, object], dict[str, object], list[str]]:
        return fields, meta, {"status": "matched", "reason": "matched"}, []

    def close(self) -> None:
        pass


class FakeCatalogMatcher:
    def __init__(self, *_: object, **__: object) -> None:
        pass

    def match_all(self, positions: list[TenderPosition]) -> tuple[list[ProductMatchItem], list[str]]:
        return [
            ProductMatchItem(
                positionIndex=index,
                product=position.product,
                productQuery=position.productQuery or position.product,
                quantity=position.quantity,
                unit=position.unit,
                evidence=position.evidence,
                requirements=position.requirements,
                match=ProductMatch(
                    **{
                        "Наименование": position.product,
                        "Артикул": "A-1",
                        "Ссылка": "https://example.test/product",
                        "Медианная цена": 100,
                        "Валюта": "RUB",
                        "Соответствие": "Полное соответствие",
                    }
                ),
            )
            for index, position in enumerate(positions, start=1)
        ], []

    def close(self) -> None:
        pass


class FakeLlmClient:
    def __init__(self, *_: object, **__: object) -> None:
        self.model = "fake-primary"
        self.model_chain = ["fake-primary"]
        self.models_used: list[str] = []
        self.calls: list[str] = []

    def analyze_document_unit(self, unit: object) -> DocumentAnalysisResponse:
        self.calls.append("analyze_document_unit")
        return DocumentAnalysisResponse(
            products=[TenderPosition(product="Анализатор", productQuery="Анализатор", quantity=1, unit="шт")],
            reasonHits=[
                DocumentReasonHit(
                    reason=REPAIR_KIT_REASON,
                    evidence="В комплект поставки входит физический ЗИП",
                    confidence="high",
                )
            ],
            fieldCandidates=[
                DocumentFieldCandidate(fieldName="lotDivisible", value="no", confidence="high", evidence="лот неделимый"),
                DocumentFieldCandidate(fieldName="counterpartyName", value="Заказчик", confidence="high", evidence="Заказчик"),
                DocumentFieldCandidate(fieldName="counterpartyInn", value="1234567890", confidence="high", evidence="ИНН"),
            ],
        )

    def consolidate_document_analysis(self, results: list[dict[str, object]]) -> TenderConsolidationResponse:
        self.calls.append("consolidate_document_analysis")
        return TenderConsolidationResponse(
            products=[TenderPosition(product="Анализатор", productQuery="Анализатор", quantity=1, unit="шт")],
            reasonHits=[
                DocumentReasonHit(
                    reason=REPAIR_KIT_REASON,
                    evidence="В комплект поставки входит физический ЗИП",
                    confidence="high",
                )
            ],
            fieldCandidates=[
                DocumentFieldCandidate(fieldName="lotDivisible", value="no", confidence="high", evidence="лот неделимый"),
                DocumentFieldCandidate(fieldName="counterpartyName", value="Заказчик", confidence="high", evidence="Заказчик"),
                DocumentFieldCandidate(fieldName="counterpartyInn", value="1234567890", confidence="high", evidence="ИНН"),
            ],
        )

    def json_call(self, *, operation: str, schema: object, **_: object) -> object:
        self.calls.append(operation)
        if operation == "resolve_actual_customer":
            return ActualCustomerResponse(
                selected=True,
                source="fallback",
                selectedRole="Заказчик",
                actualCustomerName="Заказчик",
                actualCustomerInn="1234567890",
                confidence="high",
                evidence="Заказчик",
            )
        raise AssertionError(f"unexpected json_call operation: {operation}")

    def decide(self, prompt: str) -> LlmDecision:
        self.calls.append("decide")
        assert "documentReasonHits" in prompt
        assert "Compact structured analysis" in prompt
        assert "Поставка анализатора" not in prompt
        return LlmDecision(
            decision="reject",
            primaryReason=REPAIR_KIT_REASON,
            detectedReasons=[
                DecisionReason(
                    reason=REPAIR_KIT_REASON,
                    evidence="В комплект поставки входит физический ЗИП",
                    confidence="high",
                )
            ],
            confidence="high",
        )

    def extract_fields(self, *_: object) -> object:
        raise AssertionError("legacy extract_fields must not be called in DocumentAnalysis pipeline")

    def extract_products(self, *_: object, **__: object) -> object:
        raise AssertionError("legacy extract_products must not be called in DocumentAnalysis pipeline")

    def audit_product_candidates(self, *_: object) -> object:
        raise AssertionError("legacy audit_product_candidates must not be called in DocumentAnalysis pipeline")


LAST_LLM: FakeLlmClient | None = None


def test_document_analysis_pipeline_uses_units_consolidator_and_no_legacy_llm(monkeypatch, tmp_path) -> None:
    import app.pipeline as pipeline_module

    def make_llm(*args: object, **kwargs: object) -> FakeLlmClient:
        global LAST_LLM
        LAST_LLM = FakeLlmClient(*args, **kwargs)
        return LAST_LLM

    monkeypatch.setattr(pipeline_module, "SeldonClient", FakeSeldonClient)
    monkeypatch.setattr(pipeline_module, "DocumentProcessor", FakeDocumentProcessor)
    monkeypatch.setattr(pipeline_module, "IProClient", FakeIProClient)
    monkeypatch.setattr(pipeline_module, "CatalogMatcher", FakeCatalogMatcher)
    monkeypatch.setattr(pipeline_module, "LlmClient", make_llm)

    settings = Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        llm_api_key="test",
        enable_document_analysis_pipeline=True,
    )
    claim = JobClaim(
        record_key="record-1",
        batch_id="batch-1",
        attempt=1,
        report_id=1,
        seldon_id="123",
        report_fields={"Код ТО": "ТО1", "Код ФЗ": "223"},
        input_json={"reportId": 1, "seldonId": "123", "toCode": "ТО1", "lawCode": "223"},
    )

    result = TenderPipeline(settings, tmp_path).run(claim)

    assert LAST_LLM is not None
    assert LAST_LLM.calls.count("analyze_document_unit") == result["debug"]["documentAnalysis"]["unitCount"]
    assert LAST_LLM.calls.count("consolidate_document_analysis") == 1
    assert LAST_LLM.calls.count("decide") == 1
    steps = [entry["step"] for entry in result["logs"]]
    assert "AI Agent - Extract Tender Fields2" not in steps
    assert "AI Agent - Extract Tender Positions" not in steps
    assert "Validate Product Candidates" not in steps
    assert "Build Fields From Document Analysis" in steps
    assert result["debug"]["llmTextLength"] == 0
