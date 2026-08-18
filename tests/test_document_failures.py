from app.models import ParsedDocument
from app.services.documents import document_processing_context


def test_all_listed_documents_failing_becomes_unavailable_documentation() -> None:
    descriptors = [{"index": 1, "url": "https://example.test/spec.xlsx"}]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="spec.xlsx",
            parserStatus="error",
            parserError="download timeout",
        )
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentationUnavailable"] is True
    assert context["processingStatus"] == "unavailable"
    assert context["documentsParsed"] == 0
    assert "spec.xlsx" in context["documentationNote"]


def test_partial_document_failure_can_continue_with_usable_text() -> None:
    descriptors = [
        {"index": 1, "url": "https://example.test/one.pdf"},
        {"index": 2, "url": "https://example.test/two.pdf"},
    ]
    parsed = [
        ParsedDocument(documentIndex=1, fileName="one.pdf", parserStatus="error"),
        ParsedDocument(
            documentIndex=2,
            fileName="two.pdf",
            parserStatus="parsed",
            text="Техническое задание",
            textQualityOk=True,
        ),
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentationUnavailable"] is False
    assert context["processingStatus"] == "partial"
    assert context["documentsParsed"] == 1


def test_parser_warning_with_usable_text_can_continue() -> None:
    descriptors = [{"index": 1, "url": "https://example.test/spec.pdf"}]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="spec.pdf",
            parserStatus="parsed_with_warning",
            parserWarning="OCR fallback used",
            text="Техническое задание",
            textQualityOk=True,
        )
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentationUnavailable"] is False
    assert context["processingStatus"] == "available"


def test_empty_download_is_named_in_documentation_note() -> None:
    descriptors = [{"index": 1, "url": "https://example.test/spec.xlsx"}]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="spec.xlsx",
            parserStatus="error",
            parserError="Сервер вернул пустой файл",
        )
    ]

    context = document_processing_context(descriptors, parsed)

    assert context["documentationUnavailable"] is True
    assert context["emptyFiles"] == ["spec.xlsx"]
    assert "Пустые файлы: spec.xlsx" in context["documentationNote"]
