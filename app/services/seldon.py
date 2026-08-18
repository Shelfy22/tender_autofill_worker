from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import redis

from app.config import Settings
from app.models import NormalizedJob


class SeldonDocumentsError(RuntimeError):
    """A technical Seldon failure that must be retried by the job controller."""


@dataclass(frozen=True)
class SeldonDocumentsResult:
    documents: list[dict[str, Any]]
    warnings: list[str]
    api_code: int
    api_description: str

    @property
    def documentation_missing(self) -> bool:
        return self.api_code == 404 or (self.api_code == 200 and not self.documents)

    def decision_context(self) -> dict[str, Any]:
        note = ""
        if self.documentation_missing:
            note = (
                "Seldon не выдал документы после запроса: "
                f"code={self.api_code}; "
                f"{self.api_description or 'документация отсутствует'}."
            )
        return {
            "apiCode": self.api_code,
            "apiDescription": self.api_description,
            "documentsFound": len(self.documents),
            "documentationMissing": self.documentation_missing,
            "documentationUnavailable": False,
            "processingStatus": (
                "seldon_returned_no_documents"
                if self.documentation_missing
                else "links_received"
            ),
            "documentationNote": note,
        }


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _body(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    data = response.json()
    return data.get("body") if isinstance(data, dict) and isinstance(data.get("body"), dict) else data


class SeldonClient:
    TOKEN_KEY = "tender-autofill:seldon-token"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
        self.http = httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout_seconds,
                read=settings.http_read_timeout_seconds,
                write=settings.http_read_timeout_seconds,
                pool=settings.http_connect_timeout_seconds,
            ),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()
        self.redis.close()

    def get_token(self, supplied: str | None = None) -> str:
        if supplied and supplied.strip():
            return supplied.strip()
        cached = self.redis.get(self.TOKEN_KEY)
        if cached:
            return str(cached)
        if not self.settings.seldon_username or not self.settings.seldon_password:
            raise RuntimeError("Seldon token отсутствует, SELDON_USERNAME/SELDON_PASSWORD не настроены")
        response = self.http.post(
            f"{self.settings.seldon_base_url.rstrip('/')}/User/Login",
            json={
                "name": self.settings.seldon_username,
                "password": self.settings.seldon_password.get_secret_value(),
            },
        )
        data = _body(response)
        code = int(data.get("status", {}).get("code", 0))
        token = str(data.get("result", {}).get("token") or data.get("token") or "").strip()
        if code != 200 or not token:
            description = data.get("status", {}).get("descr") or data.get("message") or "нет описания"
            raise RuntimeError(f"Не удалось авторизоваться в Seldon API: code={code}; {description}")
        self.redis.setex(self.TOKEN_KEY, int(23.5 * 3600), token)
        return token

    def get_purchase_documents(self, job: NormalizedJob, token: str) -> SeldonDocumentsResult:
        lookup: dict[str, Any] = {"reportId": job.report_id}
        if job.seldon_id:
            try:
                lookup["seldonId"] = int(job.seldon_id)
            except ValueError:
                raise ValueError(f"Некорректный seldonId: {job.seldon_id}") from None
        else:
            lookup["etpId"] = job.etp_id
        url = (
            f"{self.settings.seldon_base_url.rstrip('/')}/PurchasesDocuments/Get"
            f"?token={quote(token, safe='')}"
        )
        response = self.http.post(url, json=lookup)
        data = _body(response)
        code = int(data.get("status", {}).get("code", 0))
        warnings: list[str] = []
        description = str(
            data.get("status", {}).get("descr")
            or data.get("message")
            or "нет описания"
        ).strip()
        if code == 404:
            warnings.append(
                f"Документы Seldon не получены: code={code}; {description}. Обработка продолжена по purchase."
            )
            return SeldonDocumentsResult([], warnings, code, description)
        if code != 200:
            raise SeldonDocumentsError(
                f"Техническая ошибка Seldon при получении документов: "
                f"code={code}; {description}"
            )
        result = data.get("result") or {}
        groups = (
            result.get("purchasesdocuments")
            or result.get("purchaseDocuments")
            or result.get("documentsByPurchase")
            or []
        )
        if isinstance(groups, dict):
            groups = [groups]
        if not groups and isinstance(result.get("documents"), list):
            groups = [{"documents": result["documents"]}]
        documents: list[dict[str, Any]] = []
        for group in groups if isinstance(groups, list) else []:
            for document in group.get("documents", []) if isinstance(group, dict) else []:
                if not isinstance(document, dict):
                    continue
                version = _first(document, "Version", "version")
                if version is False or str(version).strip() == "0":
                    continue
                url_seldon = _first(document, "urlSeldon")
                url_source = _first(document, "urlSource")
                selected = url_seldon or url_source or _first(document, "url", "downloadUrl")
                if not selected:
                    continue
                extension = str(_first(document, "fileType", "extension", "ext") or "").lower().lstrip(".")
                name = str(_first(document, "name", "fileName", "filename", "title") or f"seldon_document_{len(documents)+1}")
                if extension and not name.lower().endswith(f".{extension}"):
                    name = f"{name}.{extension}"
                documents.append(
                    {
                        "index": len(documents) + 1,
                        "id": _first(document, "id", "Id", "documentId"),
                        "url": str(selected),
                        "urlSeldon": str(url_seldon) if url_seldon else None,
                        "urlSource": str(url_source) if url_source else None,
                        "source": "seldon" if url_seldon else "source" if url_source else "other",
                        "fileName": name,
                        "fileType": extension,
                        "fileSize": _first(document, "fileSize", "size"),
                        "version": version,
                        "publishDate": document.get("publishDate"),
                    }
                )
        if not documents:
            warnings.append(
                "Seldon успешно обработал запрос, но не вернул ни одного актуального документа."
            )
        return SeldonDocumentsResult(documents, warnings, code, description)


def build_page_text(job: NormalizedJob, document_files: list[dict[str, Any]]) -> str:
    purchase = job.seldon_purchase
    subject = _first(purchase, "subject", "purchaseSubject", "name", "purchaseName", "description")
    initial_price = _first(purchase, "price", "purchasePrice", "initialPrice", "maxPrice", "nmck")
    purchase_number = _first(
        purchase, "etpId", "EtpId", "notificationNumber", "purchaseNumber", "number"
    ) or job.etp_id
    metadata = [
        {
            key: document.get(key)
            for key in ("id", "fileName", "fileType", "fileSize", "version", "publishDate", "source")
        }
        for document in document_files
    ]
    return "\n".join(
        [
            "--- ДАННЫЕ ЗАКУПКИ ИЗ SELDON API ---",
            f"Seldon ID: {job.seldon_id or ''}",
            f"Report ID: {job.report_id}",
            f"Номер закупки на площадке: {purchase_number or ''}",
            f"Предмет закупки: {subject or ''}",
            f"Начальная цена / НМЦ: {initial_price or ''}",
            f"Дата публикации: {_first(purchase, 'publishDate', 'firstPublishDate', 'datePublish') or ''}",
            f"Дата начала подачи: {_first(purchase, 'dateStart', 'startDate', 'submissionStartDate') or ''}",
            f"Дата окончания подачи: {_first(purchase, 'dateEnd', 'endDate', 'submissionDeadline') or ''}",
            f"Ссылка на источник: {job.tender_url or ''}",
            "",
            "--- ПОЛНЫЕ СТРУКТУРИРОВАННЫЕ ДАННЫЕ SELDON ---",
            json.dumps(purchase, ensure_ascii=False, indent=2, default=str),
            "",
            "--- АКТУАЛЬНЫЕ ДОКУМЕНТЫ SELDON ---",
            json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        ]
    )
