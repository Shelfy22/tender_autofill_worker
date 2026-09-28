from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from app.config import Settings
from app.services.yandex_disk import YandexDiskUploader


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: dict[str, str] | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, str]:
        return self.payload or {}


class FakeYandexClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, bytes | None]] = []

    def put(self, url: str, *, params: dict | None = None, content: bytes | None = None, headers: dict | None = None) -> FakeResponse:
        self.calls.append(("PUT", url, params, content))
        return FakeResponse(status_code=201)

    def get(self, url: str, *, params: dict | None = None) -> FakeResponse:
        self.calls.append(("GET", url, params, None))
        return FakeResponse(payload={"href": "https://upload.example.test/one-time"})

    def close(self) -> None:
        raise AssertionError("injected client must not be closed")


def settings() -> Settings:
    return Settings(
        postgres_dsn=SecretStr("postgresql://user:password@localhost/database"),
        yandex_disk_token=SecretStr("secret-token"),
    )


def test_upload_xlsx_creates_folder_and_uploads_with_overwrite() -> None:
    client = FakeYandexClient()
    uploader = YandexDiskUploader(settings(), client=client)  # type: ignore[arg-type]

    result = uploader.upload_xlsx("autopodbor_700.xlsx", b"xlsx-content")
    uploader.close()

    assert result.remote_path == "disk:/Тендеры автоподбор/autopodbor_700.xlsx"
    assert client.calls == [
        (
            "PUT",
            "https://cloud-api.yandex.net/v1/disk/resources",
            {"path": "disk:/Тендеры автоподбор"},
            None,
        ),
        (
            "GET",
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            {
                "path": "disk:/Тендеры автоподбор/autopodbor_700.xlsx",
                "overwrite": "true",
                "fields": "href",
            },
            None,
        ),
        ("PUT", "https://upload.example.test/one-time", None, b"xlsx-content"),
    ]
