import zipfile
from pathlib import Path

import pytest
import py7zr

from app.config import Settings
from app.services.parsers.archives import (
    UnsafeArchiveError,
    _rar_members_from_lsar,
    extract_7z,
    extract_zip,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        postgres_dsn="postgresql://user:pass@localhost/db",
        temp_root=tmp_path,
        max_archive_members=10,
        max_archive_uncompressed_bytes=10_000,
        max_archive_compression_ratio=100,
    )


def test_zip_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../escape.txt", "bad")
    with pytest.raises(UnsafeArchiveError):
        extract_zip(archive, tmp_path / "out", settings(tmp_path))
    assert not (tmp_path / "escape.txt").exists()


def test_zip_member_limit_is_enforced(tmp_path: Path) -> None:
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for index in range(11):
            output.writestr(f"{index}.txt", "x")
    with pytest.raises(UnsafeArchiveError):
        extract_zip(archive, tmp_path / "out", settings(tmp_path))


def test_7z_archive_is_extracted(tmp_path: Path) -> None:
    source = tmp_path / "specification.txt"
    source.write_text("product | quantity\nCable | 10", encoding="utf-8")
    archive = tmp_path / "documents.7z"
    with py7zr.SevenZipFile(archive, "w") as output:
        output.write(source, arcname="docs/specification.txt")

    extracted = extract_7z(archive, tmp_path / "out-7z", settings(tmp_path))

    assert len(extracted) == 1
    assert extracted[0].read_text(encoding="utf-8") == "product | quantity\nCable | 10"


def test_rar_lsar_listing_preserves_sizes() -> None:
    payload = {
        "lsarFormatVersion": 2,
        "lsarContents": [
            {
                "XADFileName": "docs/specification.xlsx",
                "XADFileSize": 1200,
                "XADCompressedSize": 600,
            },
            {"XADFileName": "empty", "XADIsDirectory": True},
        ],
    }
    assert _rar_members_from_lsar(payload) == [("docs/specification.xlsx", 1200, 600)]


def test_rar_symlink_is_rejected() -> None:
    payload = {
        "lsarContents": [
            {
                "XADFileName": "unsafe-link",
                "XADFileSize": 0,
                "XADCompressedSize": 0,
                "XADIsLink": True,
            }
        ]
    }
    with pytest.raises(UnsafeArchiveError):
        _rar_members_from_lsar(payload)
