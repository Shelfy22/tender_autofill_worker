import ssl
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.services.documents import (
    DocumentProcessor,
    _certificate_verification_failed,
    _validate_downloaded_content,
)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        temp_root=tmp_path,
        **overrides,
    )


def test_download_uses_source_url_after_seldon_url_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "seldon.test":
            return httpx.Response(403, request=request)
        return httpx.Response(
            200,
            content=b"%PDF-1.7\nvalid test body",
            headers={"Content-Type": "application/pdf"},
            request=request,
        )

    processor = DocumentProcessor(settings(tmp_path), tmp_path)
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        path, used_url, warnings = processor.download(
            {
                "index": 1,
                "fileName": "specification.pdf",
                "urlSeldon": "https://seldon.test/document/1?token=secret",
                "urlSource": "https://source.test/document/1",
            }
        )
        content = path.read_bytes()
    finally:
        processor.close()

    assert content.startswith(b"%PDF")
    assert used_url == "https://source.test/document/1"
    assert warnings == [
        "Использован резервный URL документа: https://source.test/document/1"
    ]


def test_html_response_is_rejected_before_docx_parser(tmp_path: Path) -> None:
    path = tmp_path / "document.docx"
    path.write_text("<!doctype html><html>login required</html>", encoding="utf-8")

    with pytest.raises(ValueError, match="HTML-страницу"):
        _validate_downloaded_content(path, path.name, "text/html; charset=utf-8")


def test_ssl_verification_error_is_identified_through_wrapped_exception() -> None:
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    wrapped = httpx.ConnectError("CERTIFICATE_VERIFY_FAILED")
    wrapped.__cause__ = ssl_error

    assert _certificate_verification_failed(wrapped)


def test_ssl_verification_failure_retries_document_insecurely(tmp_path: Path) -> None:
    def strict_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED", request=request)

    def insecure_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.7\nvalid test body",
            headers={"Content-Type": "application/pdf"},
            request=request,
        )

    processor = DocumentProcessor(settings(tmp_path), tmp_path)
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(strict_handler))
    processor._insecure_http = httpx.Client(
        transport=httpx.MockTransport(insecure_handler), verify=False
    )
    try:
        path, used_url, warnings = processor.download(
            {
                "index": 1,
                "fileName": "specification.pdf",
                "url": "https://broken-tls.test/specification.pdf",
            }
        )
        content = path.read_bytes()
    finally:
        processor.close()

    assert content.startswith(b"%PDF")
    assert used_url == "https://broken-tls.test/specification.pdf"
    assert len(warnings) == 1
    assert "отключённой проверкой сертификата" in warnings[0]
