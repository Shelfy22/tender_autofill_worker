import pytest

from app.models import ParsedDocument
from app.services.documents import DocumentProcessingError, ensure_documents_usable


def test_all_listed_documents_failing_is_a_technical_error() -> None:
    descriptors = [{"index": 1, "url": "https://example.test/spec.xlsx"}]
    parsed = [
        ParsedDocument(
            documentIndex=1,
            fileName="spec.xlsx",
            parserStatus="error",
            parserError="download timeout",
        )
    ]

    with pytest.raises(DocumentProcessingError, match="Не удалось скачать или распарсить"):
        ensure_documents_usable(descriptors, parsed)


def test_partial_document_failure_is_still_a_technical_error() -> None:
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

    with pytest.raises(DocumentProcessingError, match="one.pdf"):
        ensure_documents_usable(descriptors, parsed)


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

    ensure_documents_usable(descriptors, parsed)
