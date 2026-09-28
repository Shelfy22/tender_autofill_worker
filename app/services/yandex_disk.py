from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


_API_URL = "https://cloud-api.yandex.net/v1/disk"
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class YandexDiskUpload:
    remote_path: str


class YandexDiskUploader:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        token = settings.yandex_disk_token
        if token is None or not token.get_secret_value().strip():
            raise ValueError("YANDEX_DISK_TOKEN is not configured")
        self.folder = settings.yandex_disk_autopodbor_path.strip().strip("/")
        if not self.folder:
            raise ValueError("YANDEX_DISK_AUTOPODBOR_PATH must not be empty")
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=settings.yandex_disk_timeout_seconds,
            headers={"Authorization": f"OAuth {token.get_secret_value()}"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def upload_xlsx(self, filename: str, content: bytes) -> YandexDiskUpload:
        remote_path = f"disk:/{self.folder}/{filename}"
        folder_response = self.client.put(
            f"{_API_URL}/resources",
            params={"path": f"disk:/{self.folder}"},
        )
        if folder_response.status_code not in {201, 409}:
            folder_response.raise_for_status()

        upload_response = self.client.get(
            f"{_API_URL}/resources/upload",
            params={"path": remote_path, "overwrite": "true", "fields": "href"},
        )
        upload_response.raise_for_status()
        payload: dict[str, Any] = upload_response.json()
        href = payload.get("href")
        if not isinstance(href, str) or not href:
            raise RuntimeError("Yandex Disk API did not return an upload URL")

        content_response = self.client.put(
            href,
            content=content,
            headers={"Content-Type": _XLSX_MEDIA_TYPE},
        )
        content_response.raise_for_status()
        return YandexDiskUpload(remote_path=remote_path)


def upload_product_matching_report(
    settings: Settings,
    *,
    filename: str,
    content: bytes,
) -> YandexDiskUpload:
    uploader = YandexDiskUploader(settings)
    try:
        return uploader.upload_xlsx(filename, content)
    finally:
        uploader.close()
