from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from openpyxl import Workbook

from app.config import Settings
from app.services.documents import DocumentProcessor, document_processing_context


class FakeObserver:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, **values: Any) -> None:
        self.events.append(values)


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "postgres_dsn": "postgresql://user:pass@localhost/db",
        "temp_root": tmp_path,
        "document_enable_curl_fallback": False,
    }
    values.update(overrides)
    return Settings(**values)


def xlsx_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "source.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Products"
    sheet.append(["Product name", "Quantity", "Unit"])
    sheet.append(["Industrial cable with technical specification", 25, "pcs"])
    workbook.save(path)
    workbook.close()
    return path.read_bytes()


def test_extensionless_seldon_name_uses_xlsx_suffix_from_url_and_parses_ooxml(
    tmp_path: Path,
) -> None:
    content = xlsx_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "application/octet-stream"},
            request=request,
        )

    observer = FakeObserver()
    processor = DocumentProcessor(settings(tmp_path), tmp_path, observer=observer)
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        documents, warnings = processor.process_all(
            [
                {
                    "index": 1,
                    "fileName": "Specification from Seldon",
                    "url": "https://com.roseltorg.test/file/name/specification.xlsx",
                }
            ]
        )
    finally:
        processor.close()

    assert not warnings
    assert len(documents) == 1
    assert documents[0].fileName == "Specification from Seldon.xlsx"
    assert documents[0].fileExtension == "xlsx"
    assert documents[0].parserRoute == "spreadsheet"
    assert documents[0].textQualityOk is True
    event = observer.events[-1]
    assert event["service"] == "document_http"
    assert event["http_status"] == 200
    assert event["byte_count"] == len(content)
    assert event["details"]["transport"] == "httpx"
    assert event["details"]["detectedType"] == "xlsx"
    assert event["details"]["resolvedFileName"] == "Specification from Seldon.xlsx"


def test_content_disposition_restores_xlsx_name_when_url_has_no_suffix(
    tmp_path: Path,
) -> None:
    content = xlsx_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''supplier_specification.xlsx"
                ),
            },
            request=request,
        )

    processor = DocumentProcessor(settings(tmp_path), tmp_path)
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        documents, _ = processor.process_all(
            [
                {
                    "index": 1,
                    "fileName": "Seldon document",
                    "url": "https://files.example.test/download/123",
                }
            ]
        )
    finally:
        processor.close()

    assert len(documents) == 1
    assert documents[0].fileName == "supplier_specification.xlsx"
    assert documents[0].fileExtension == "xlsx"
    assert documents[0].textQualityOk is True


def test_failed_procedure_bundle_is_only_warning_when_lot_document_is_usable(
    tmp_path: Path,
) -> None:
    content = xlsx_bytes(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/ProcedureDocuments/" in request.url.path:
            return httpx.Response(403, request=request)
        return httpx.Response(200, content=content, request=request)

    descriptors = [
        {
            "index": 1,
            "fileName": "Lot specification",
            "url": "https://com.roseltorg.test/file/get/t/LotDocuments/name/spec.xlsx",
        },
        {
            "index": 2,
            "fileName": "All procedure documents",
            "url": "https://com.roseltorg.test/file/get/t/ProcedureDocuments/id/1",
        },
    ]
    observer = FakeObserver()
    processor = DocumentProcessor(settings(tmp_path), tmp_path, observer=observer)
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        documents, warnings = processor.process_all(descriptors)
    finally:
        processor.close()

    context = document_processing_context(descriptors, documents)
    assert context["processingStatus"] == "partial"
    assert context["documentationUnavailable"] is False
    assert context["documentsParsed"] == 1
    assert any("403" in warning for warning in warnings)
    failed_event = next(event for event in observer.events if event["status"] == "failed")
    assert failed_event["http_status"] == 403
    assert failed_event["details"]["transport"] == "httpx"


def test_timeline_records_failed_httpx_and_successful_curl_attempt(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_bytes(b"%PDF-1.7\nvalid document")
        headers_path = Path(command[command.index("--dump-header") + 1])
        headers_path.write_text(
            'HTTP/1.1 200 OK\r\nContent-Disposition: attachment; filename="spec.pdf"\r\n',
            encoding="iso-8859-1",
        )
        return SimpleNamespace(
            returncode=0,
            stdout="200\napplication/pdf",
            stderr="",
        )

    observer = FakeObserver()
    processor = DocumentProcessor(
        settings(tmp_path, document_enable_curl_fallback=True),
        tmp_path,
        observer=observer,
    )
    processor.http.close()
    processor.http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("app.services.documents.subprocess.run", fake_run)
    try:
        _, _, resolved_name, _ = processor.download(
            {
                "index": 1,
                "fileName": "Seldon document",
                "url": "https://files.example.test/download/123",
            }
        )
    finally:
        processor.close()

    assert resolved_name == "spec.pdf"
    assert [event["status"] for event in observer.events] == ["failed", "completed"]
    assert [event["http_status"] for event in observer.events] == [403, 200]
    assert [event["details"]["transport"] for event in observer.events] == [
        "httpx",
        "curl_insecure",
    ]
    assert observer.events[-1]["details"]["detectedType"] == "pdf"
    assert observer.events[-1]["byte_count"] == len(b"%PDF-1.7\nvalid document")
