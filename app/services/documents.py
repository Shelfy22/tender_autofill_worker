from __future__ import annotations

import html
import logging
import re
import ssl
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import httpx
import py7zr
from bs4 import BeautifulSoup

from app.config import Settings
from app.models import ParsedDocument
from app.services.parsers.archives import UnsafeArchiveError, extract_archive
from app.services.parsers.common import detect_file_type, parse_file


logger = logging.getLogger(__name__)


def document_processing_context(
    descriptors: list[dict[str, Any]], documents: list[ParsedDocument]
) -> dict[str, Any]:
    """Describe document availability without turning a business rejection into a job error."""
    if not descriptors:
        return {
            "processingStatus": "seldon_returned_no_documents",
            "documentsRequested": 0,
            "documentsParsed": 0,
            "documentationUnavailable": False,
            "documentationNote": "",
            "emptyFiles": [],
            "failedFiles": [],
            "filesWithoutUsableText": [],
        }

    usable = [document for document in documents if document.textQualityOk]
    failed = [
        document
        for document in documents
        if document.parserStatus == "error" or bool(document.parserError)
    ]
    empty = [
        document
        for document in failed
        if re.search(
            r"пуст(?:ой|ые|ого)|empty\s+file|zero[- ]byte",
            document.parserError or document.parserWarning or "",
            re.I,
        )
    ]
    empty_names = [document.fileName for document in empty]
    failed_details = [
        {
            "fileName": document.fileName,
            "error": (
                document.parserError
                or document.parserWarning
                or document.parserStatus
            )[:500],
        }
        for document in failed
        if document not in empty
    ]
    no_text = [
        document.fileName
        for document in documents
        if not document.textQualityOk and document not in failed
    ]

    unavailable = not usable
    note_parts: list[str] = []
    if unavailable:
        note_parts.append(
            f"Seldon выдал ссылки на {len(descriptors)} документ(ов), "
            "но пригодный текст документации не получен."
        )
    if empty_names:
        note_parts.append(f"Пустые файлы: {', '.join(empty_names[:20])}.")
    if failed_details:
        formatted = "; ".join(
            f"{item['fileName']} — {item['error']}" for item in failed_details[:20]
        )
        note_parts.append(f"Не удалось скачать или обработать: {formatted}.")
    if no_text:
        note_parts.append(
            f"Файлы без пригодного текста: {', '.join(no_text[:20])}."
        )

    return {
        "processingStatus": (
            "unavailable"
            if unavailable
            else "partial"
            if failed or no_text
            else "available"
        ),
        "documentsRequested": len(descriptors),
        "documentsParsed": len(usable),
        "documentationUnavailable": unavailable,
        "documentationNote": " ".join(note_parts)[:3000],
        "emptyFiles": empty_names,
        "failedFiles": failed_details,
        "filesWithoutUsableText": no_text,
    }


def safe_filename(value: str, fallback: str) -> str:
    name = unquote(Path(value.replace("\\", "/")).name).strip()
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name).strip(" .")
    return name[:220] or fallback


def _safe_url_for_log(value: str) -> str:
    """Remove credentials and query strings before recording a document URL."""
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    netloc = f"{hostname}:{parsed.port}" if parsed.port else hostname
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def _certificate_verification_failed(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        message = str(current).upper()
        if "CERTIFICATE_VERIFY_FAILED" in message or "CERTIFICATE VERIFY FAILED" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _document_urls(descriptor: dict[str, Any]) -> list[str]:
    candidates = [
        descriptor.get("urlSeldon"),
        descriptor.get("urlSource"),
        descriptor.get("url"),
        descriptor.get("downloadUrl"),
    ]
    return list(
        dict.fromkeys(
            str(candidate).strip()
            for candidate in candidates
            if candidate is not None and str(candidate).strip()
        )
    )


def _validate_downloaded_content(path: Path, declared_name: str, content_type: str) -> None:
    """Reject login/error pages and corrupt binary containers before parsing."""
    if path.stat().st_size == 0:
        raise ValueError("Сервер вернул пустой файл")
    with path.open("rb") as source:
        head = source.read(4096)
    stripped = head.lstrip().lower()
    normalized_type = content_type.lower().split(";", 1)[0].strip()
    if (
        normalized_type in {"text/html", "application/xhtml+xml"}
        or stripped.startswith(b"<!doctype html")
        or stripped.startswith(b"<html")
    ):
        raise ValueError(
            "Вместо документа сервер вернул HTML-страницу (возможно, страницу входа или ошибки)"
        )

    suffix = Path(declared_name).suffix.lower()
    zip_magic = head[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}
    seven_zip_magic = head.startswith(b"7z\xbc\xaf'\x1c")
    rar_magic = head.startswith(b"Rar!\x1a\x07")
    pdf_magic = head.startswith(b"%PDF")
    ole_magic = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    if zip_magic and not zipfile.is_zipfile(path):
        raise ValueError("Повреждённый или неполный ZIP/OOXML-файл")
    if seven_zip_magic and not py7zr.is_7zfile(path):
        raise ValueError("Повреждённый или неполный 7Z-архив")

    required_binary_signatures = {
        ".zip": zip_magic,
        ".7z": seven_zip_magic,
        ".rar": rar_magic,
        ".pdf": pdf_magic,
        ".docx": zip_magic,
        ".xlsx": zip_magic,
    }
    expected = required_binary_signatures.get(suffix)
    if expected is False and not any(
        (zip_magic, seven_zip_magic, rar_magic, pdf_magic, ole_magic)
    ):
        raise ValueError(
            f"Содержимое файла не соответствует заявленному формату {suffix or declared_name}"
        )


class DocumentProcessor:
    def __init__(
        self,
        settings: Settings,
        temp_dir: Path,
        llm: Any = None,
        *,
        referer_url: str | None = None,
    ) -> None:
        self.settings = settings
        self.temp_dir = temp_dir
        self.llm = llm
        self.referer_url = str(referer_url or settings.seldon_base_url).strip()
        self.downloaded_total = 0
        self.http = self._build_http_client(verify=True)
        self._insecure_http: httpx.Client | None = None

    def _build_http_client(self, *, verify: bool) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(
                connect=self.settings.http_connect_timeout_seconds,
                read=self.settings.document_download_timeout_seconds,
                write=self.settings.document_download_timeout_seconds,
                pool=self.settings.http_connect_timeout_seconds,
            ),
            follow_redirects=True,
            headers={
                # Keep these values aligned with the proven n8n Download Document node.
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                ),
                "Accept": (
                    "application/octet-stream,application/pdf,"
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document,*/*"
                ),
            },
            verify=verify,
        )

    def close(self) -> None:
        self.http.close()
        if self._insecure_http is not None:
            self._insecure_http.close()

    def _client_without_ssl_verification(self) -> httpx.Client:
        if self._insecure_http is None:
            self._insecure_http = self._build_http_client(verify=False)
        return self._insecure_http

    def _request_headers(self, _: str) -> dict[str, str]:
        # n8n uses tenderUrl, falling back to the Seldon base URL. Some public
        # platforms return an empty/error body when only their site root is sent.
        return {"Referer": self.referer_url} if self.referer_url else {}

    def _download_url(
        self,
        client: httpx.Client,
        url: str,
        destination: Path,
        name: str,
    ) -> None:
        temporary = destination.with_suffix(f"{destination.suffix}.part")
        temporary.unlink(missing_ok=True)
        size = 0
        try:
            with client.stream(
                "GET", url, headers=self._request_headers(url)
            ) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > self.settings.max_download_bytes_per_file:
                    raise ValueError(f"Документ {name} превышает лимит размера")
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > self.settings.max_download_bytes_per_file:
                            raise ValueError(f"Документ {name} превышает лимит размера")
                        if self.downloaded_total + size > self.settings.max_download_bytes_total:
                            raise ValueError("Общий размер документов tender превышает лимит")
                        output.write(chunk)
                _validate_downloaded_content(
                    temporary,
                    name,
                    response.headers.get("content-type", ""),
                )
            temporary.replace(destination)
            self.downloaded_total += size
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _download_url_with_curl(
        self,
        url: str,
        destination: Path,
        name: str,
    ) -> None:
        """Fallback transport for public ETP links that behave differently in Node/curl."""
        temporary = destination.with_suffix(f"{destination.suffix}.curl.part")
        temporary.unlink(missing_ok=True)
        remaining_total = (
            self.settings.max_download_bytes_total - self.downloaded_total
        )
        maximum = min(self.settings.max_download_bytes_per_file, remaining_total)
        if maximum <= 0:
            raise ValueError("Общий размер документов tender превышает лимит")

        command = [
            self.settings.curl_binary,
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--max-redirs",
            "10",
            "--connect-timeout",
            str(self.settings.http_connect_timeout_seconds),
            "--max-time",
            str(self.settings.document_download_timeout_seconds),
            "--max-filesize",
            str(maximum),
            "--proto",
            "=http,https",
            "--insecure",
            "--compressed",
            "--user-agent",
            str(self.http.headers.get("user-agent") or ""),
            "--header",
            f"Accept: {self.http.headers.get('accept') or '*/*'}",
        ]
        if self.referer_url:
            command.extend(["--referer", self.referer_url])
        command.extend(
            [
                "--output",
                str(temporary),
                "--write-out",
                "%{content_type}",
                url,
            ]
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=(
                    self.settings.document_download_timeout_seconds
                    + self.settings.http_connect_timeout_seconds
                    + 10
                ),
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    f"curl завершился с кодом {completed.returncode}: {detail[:1000]}"
                )
            _validate_downloaded_content(
                temporary,
                name,
                (completed.stdout or "").strip(),
            )
            size = temporary.stat().st_size
            if size > maximum:
                raise ValueError(f"Документ {name} превышает лимит размера")
            temporary.replace(destination)
            self.downloaded_total += size
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def download(self, descriptor: dict[str, Any]) -> tuple[Path, str, list[str]]:
        index = int(descriptor.get("index") or 1)
        urls = _document_urls(descriptor)
        if not urls:
            raise ValueError("Для документа отсутствует URL скачивания")
        parsed_name = Path(urlparse(urls[0]).path).name
        name = safe_filename(
            str(descriptor.get("fileName") or parsed_name), f"document_{index}"
        )
        destination = self.temp_dir / "downloads" / f"{index:03d}_{name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        failures: list[str] = []
        for position, url in enumerate(urls, start=1):
            display_url = _safe_url_for_log(url)
            try:
                self._download_url(self.http, url, destination, name)
            except Exception as exc:
                logger.warning(
                    "document_download_attempt_failed",
                    extra={
                        "event": {
                            "stage": "document_download",
                            "file_name": name,
                            "url": display_url,
                            "error": str(exc),
                        }
                    },
                )
                last_error: Exception = exc
                if (
                    self.settings.document_allow_insecure_ssl_fallback
                    and _certificate_verification_failed(exc)
                ):
                    try:
                        self._download_url(
                            self._client_without_ssl_verification(),
                            url,
                            destination,
                            name,
                        )
                    except Exception as insecure_exc:
                        last_error = insecure_exc
                    else:
                        warnings.append(
                            "TLS-сертификат источника не прошёл проверку; документ скачан "
                            f"с отключённой проверкой сертификата: {display_url}"
                        )
                        logger.warning(
                            "document_downloaded_without_ssl_verification",
                            extra={
                                "event": {
                                    "stage": "document_download",
                                    "file_name": name,
                                    "url": display_url,
                                }
                            },
                        )
                        if position > 1:
                            warnings.append(
                                f"Использован резервный URL документа: {display_url}"
                            )
                        return destination, url, warnings

                if self.settings.document_enable_curl_fallback:
                    try:
                        self._download_url_with_curl(url, destination, name)
                    except Exception as curl_exc:
                        failures.append(
                            f"{display_url}: httpx={last_error}; curl={curl_exc}"
                        )
                        continue
                    warnings.append(
                        "Документ скачан резервным curl-транспортом после ошибки "
                        f"основного HTTP-клиента: {display_url}"
                    )
                    logger.warning(
                        "document_downloaded_with_curl_fallback",
                        extra={
                            "event": {
                                "stage": "document_download",
                                "file_name": name,
                                "url": display_url,
                                "httpx_error": str(last_error),
                            }
                        },
                    )
                    if position > 1:
                        warnings.append(
                            f"Использован резервный URL документа: {display_url}"
                        )
                    return destination, url, warnings

                failures.append(f"{display_url}: {last_error}")
                continue
            if position > 1:
                warnings.append(f"Использован резервный URL документа: {display_url}")
                logger.info(
                    "document_downloaded_from_fallback_url",
                    extra={
                        "event": {
                            "stage": "document_download",
                            "file_name": name,
                            "url": display_url,
                        }
                    },
                )
            return destination, url, warnings
        raise RuntimeError("; ".join(failures)[:3000])

    def process_all(self, descriptors: list[dict[str, Any]]) -> tuple[list[ParsedDocument], list[str]]:
        warnings: list[str] = []
        parsed: list[ParsedDocument] = []
        for descriptor in descriptors[: self.settings.max_documents]:
            try:
                path, downloaded_url, download_warnings = self.download(descriptor)
                warnings.extend(download_warnings)
                effective_descriptor = {**descriptor, "url": downloaded_url}
                parsed.extend(self._process_path(path, effective_descriptor, depth=0))
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
                try:
                    result.extend(self._process_path(child, child_descriptor, depth + 1))
                except Exception as exc:
                    result.append(
                        ParsedDocument(
                            documentIndex=int(child_descriptor["index"]),
                            documentUrl=str(child_descriptor.get("url") or ""),
                            fileName=child.name,
                            extractedFromArchive=True,
                            parentArchiveFileName=path.name,
                            parserStatus="error",
                            parserWarning=f"{child.name}: {exc}",
                            parserError=str(exc),
                        )
                    )
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
