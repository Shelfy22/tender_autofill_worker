from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.models import ParsedDocument
from app.services.parsers.archives import UnsafeArchiveError, extract_archive
from app.services.parsers.common import detect_file_type, parse_file


class DocumentProcessingError(RuntimeError):
    """Documents existed, but the worker could not obtain usable text from any of them."""


def ensure_documents_usable(
    descriptors: list[dict[str, Any]], documents: list[ParsedDocument]
) -> None:
    if not descriptors:
        return
    failed = [
        document
        for document in documents
        if document.parserStatus == "error" or bool(document.parserError)
    ]
    if failed:
        errors = "; ".join(
            f"{document.fileName}: "
            f"{document.parserError or document.parserWarning or document.parserStatus}"
            for document in failed
        )[:2000]
        raise DocumentProcessingError(
            f"Не удалось скачать или распарсить {len(failed)} документ(ов): {errors}"
        )
    if any(document.textQualityOk for document in documents):
        return
    errors = "; ".join(
        document.parserError or document.parserWarning or document.parserStatus
        for document in documents
    )[:2000]
    raise DocumentProcessingError(
        "Документы Seldon были обнаружены, но ни один документ не удалось "
        f"успешно скачать и извлечь: {errors or 'нет текста после парсинга'}"
    )


def safe_filename(value: str, fallback: str) -> str:
    name = unquote(Path(value.replace("\\", "/")).name).strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return name[:220] or fallback


class DocumentProcessor:
    def __init__(self, settings: Settings, temp_dir: Path, llm: Any = None) -> None:
        self.settings = settings
        self.temp_dir = temp_dir
        self.llm = llm
        self.downloaded_total = 0
        self.http = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_seconds,
                read=settings.document_download_timeout_seconds,
                write=settings.document_download_timeout_seconds,
                pool=settings.http_connect_timeout_seconds,
            ),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 TenderAutofillPython/0.1",
                "Accept": "application/octet-stream,application/pdf,*/*",
            },
        )

    def close(self) -> None:
        self.http.close()

    def download(self, descriptor: dict[str, Any]) -> Path:
        index = int(descriptor.get("index") or 1)
        url = str(descriptor["url"])
        parsed_name = Path(urlparse(url).path).name
        name = safe_filename(
            str(descriptor.get("fileName") or parsed_name), f"document_{index}"
        )
        destination = self.temp_dir / "downloads" / f"{index:03d}_{name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with self.http.stream("GET", url) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > self.settings.max_download_bytes_per_file:
                raise ValueError(f"Документ {name} превышает лимит размера")
            with destination.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_download_bytes_per_file:
                        raise ValueError(f"Документ {name} превышает лимит размера")
                    if self.downloaded_total + size > self.settings.max_download_bytes_total:
                        raise ValueError("Общий размер документов tender превышает лимит")
                    output.write(chunk)
        self.downloaded_total += size
        return destination

    def process_all(self, descriptors: list[dict[str, Any]]) -> tuple[list[ParsedDocument], list[str]]:
        warnings: list[str] = []
        parsed: list[ParsedDocument] = []
        for descriptor in descriptors[: self.settings.max_documents]:
            try:
                path = self.download(descriptor)
                parsed.extend(self._process_path(path, descriptor, depth=0))
            except Exception as exc:
                name = str(descriptor.get("fileName") or descriptor.get("url") or "document")
                warning = f"{name}: {exc}"
                warnings.append(warning)
                parsed.append(
                    ParsedDocument(
                        documentIndex=int(descriptor.get("index") or len(parsed) + 1),
                        documentUrl=str(descriptor.get("url") or ""),
                        fileName=name,
                        parserStatus="error",
                        parserWarning=warning,
                        parserError=str(exc),
                    )
                )
        if len(descriptors) > self.settings.max_documents:
            warnings.append(
                f"Документов {len(descriptors)}; обработаны первые {self.settings.max_documents}."
            )
        return parsed, warnings

    def _process_path(
        self, path: Path, descriptor: dict[str, Any], depth: int
    ) -> list[ParsedDocument]:
        kind = detect_file_type(path, str(descriptor.get("fileName") or path.name))
        index = int(descriptor.get("index") or 1)
        if kind in {"zip", "rar", "7z"}:
            if depth >= self.settings.max_archive_depth:
                return [
                    ParsedDocument(
                        documentIndex=index,
                        documentUrl=str(descriptor.get("url") or ""),
                        fileName=path.name,
                        fileExtension=kind,
                        parserRoute="nested_archive",
                        parserStatus="skipped",
                        parserWarning="Вложенный архив пропущен: достигнут MAX_ARCHIVE_DEPTH.",
                    )
                ]
            destination = self.temp_dir / "extracted" / f"archive_{index}_{depth}"
            children = extract_archive(path, kind, destination, self.settings)
            result: list[ParsedDocument] = []
            for child_number, child in enumerate(children[: self.settings.max_archive_members], start=1):
                child_descriptor = {
                    **descriptor,
                    "index": index * 1000 + child_number,
                    "fileName": child.name,
                    "url": descriptor.get("url") or "",
                    "extractedFromArchive": True,
                    "parentArchiveFileName": path.name,
                }
                result.extend(self._process_path(child, child_descriptor, depth + 1))
            return result

        text, status, warnings = parse_file(path, kind, self.settings, self.llm)
        warning = " ".join(warnings)
        return [
            ParsedDocument(
                documentIndex=index,
                documentUrl=str(descriptor.get("url") or ""),
                fileName=safe_filename(str(descriptor.get("fileName") or path.name), path.name),
                fileExtension=kind,
                mimeType=str(descriptor.get("mimeType") or ""),
                fileSize=path.stat().st_size,
                parserRoute=("word" if kind in {"doc", "docx"} else "spreadsheet" if kind in {"xls", "xlsx", "csv"} else kind),
                extractedFromArchive=bool(descriptor.get("extractedFromArchive")),
                parentArchiveFileName=str(descriptor.get("parentArchiveFileName") or ""),
                text=text,
                textLength=len(text),
                textQualityOk=bool(text),
                textPreview=text[:300],
                parserStatus=status,
                parserWarning=warning,
            )
        ]

    def fetch_tender_html(self, url: str) -> tuple[str, list[str]]:
        warnings: list[str] = []
        try:
            response = self.http.get(url, headers={"Accept": "text/html,*/*"})
            response.raise_for_status()
            content = response.text[: self.settings.max_text_chars_per_file * 2]
            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style", "noscript"]):
                element.decompose()
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()
            return html.unescape(text)[: self.settings.max_text_chars_per_file], warnings
        except Exception as exc:
            warnings.append(f"HTML страницы тендера не получен: {exc}")
            return "", warnings


def build_combined_text(
    page_text: str, documents: list[ParsedDocument], settings: Settings
) -> tuple[str, list[dict[str, Any]], list[str]]:
    sections = ["--- ТЕКСТ СТРАНИЦЫ ТЕНДЕРА / ДОКУМЕНТОВ ---", page_text.strip()]
    lengths: list[dict[str, Any]] = []
    for number, document in enumerate(documents, start=1):
        sections.extend(
            [
                "",
                f"--- ДОКУМЕНТ {number} ---",
                f"fileName: {document.fileName}",
                f"extension: {document.fileExtension}",
                f"url: {document.documentUrl}",
                f"parserStatus: {document.parserStatus}",
                f"parserWarning: {document.parserWarning}" if document.parserWarning else "",
                document.text,
            ]
        )
        lengths.append(
            {
                "index": number,
                "fileName": document.fileName,
                "extension": document.fileExtension,
                "parserStatus": document.parserStatus,
                "textQualityOk": document.textQualityOk,
                "textLength": len(document.text),
            }
        )
    full = "\n".join(section for section in sections if section is not None)
    warnings: list[str] = []
    if len(full) > settings.max_combined_text_chars:
        warnings.append(
            f"Текст для LLM обрезан: {len(full)} -> {settings.max_combined_text_chars} символов."
        )
        full = full[: settings.max_combined_text_chars]
    return full, lengths, warnings
