from app.models import ParsedDocument
from app.services.documents import document_processing_context


def test_marketing_research_placeholder_is_not_substantive_documentation() -> None:
    descriptors = [
        {
            "index": 1,
            "url": "https://storage.example.test/document",
            "fileName": "Маркетинговые исследования.pdf",
        }
    ]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="Документация.pdf",
            originalFileName="Маркетинговые исследования.pdf",
            documentKind="marketing_research",
            parserStatus="ok",
            text=(
                "Неконкурентная закупка. Размещение документации о маркетинговых "
                "исследованиях в ЕИС не предусмотрено."
            ),
            textQualityOk=True,
        )
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentsParsed"] == 1
    assert context["documentationUnavailable"] is True
    assert context["processingStatus"] == "unavailable"
    assert context["marketingResearchFiles"] == ["Маркетинговые исследования.pdf"]
    assert context["documentationPlaceholderFiles"][0]["originalFileName"] == (
        "Маркетинговые исследования.pdf"
    )
    assert "информационный файл-заглушка" in context["documentationNote"]


def test_placeholder_does_not_hide_a_real_specification() -> None:
    descriptors = [
        {"index": 1, "url": "https://example.test/notice.pdf"},
        {"index": 2, "url": "https://example.test/specification.xlsx"},
    ]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="notice.pdf",
            text=(
                "Размещение документации о маркетинговых исследованиях "
                "в ЕИС не предусмотрено."
            ),
            textQualityOk=True,
        ),
        ParsedDocument(
            documentIndex=2,
            fileName="specification.xlsx",
            text="Товар: кабель. Количество: 100 метров.",
            textQualityOk=True,
        ),
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentationUnavailable"] is False
    assert context["processingStatus"] == "available"
