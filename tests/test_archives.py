import json
import subprocess
import zipfile
from pathlib import Path

import pytest
import py7zr

from app.config import Settings
from app.services.parsers.archives import (
    UnsafeArchiveError,
    _rar_members_from_lsar,
    extract_7z,
    extract_rar,
    extract_zip,
)
from app.services.parsers.common import detect_file_type


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


def test_rar_falls_back_to_bsdtar_when_unar_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "documents.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00")
    events: list[dict[str, object]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "lsar":
            payload = {
                "lsarContents": [
                    {
                        "XADFileName": "docs/specification.xlsx",
                        "XADFileSize": 20,
                        "XADCompressedSize": 8,
                    }
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[0] == "unar":
            return subprocess.CompletedProcess(command, 1, "", "Unsupported RAR5 method")
        if command[0] == "bsdtar":
            destination = Path(command[command.index("-C") + 1])
            extracted = destination / "docs" / "specification.xlsx"
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(b"document-placeholder")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    extracted = extract_rar(
        archive,
        tmp_path / "out-rar",
        settings(tmp_path),
        observer=events.append,
    )

    assert len(extracted) == 1
    assert extracted[0].name == "specification.xlsx"
    assert [(event["extractor"], event["status"]) for event in events] == [
        ("unar", "failed"),
        ("bsdtar", "completed"),
    ]
    assert events[0]["stderr"] == "Unsupported RAR5 method"
    assert events[1]["fallbackUsed"] is True


def test_rar_reports_both_extractor_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "documents.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "lsar":
            payload = {
                "lsarContents": [
                    {
                        "XADFileName": "specification.xlsx",
                        "XADFileSize": 20,
                        "XADCompressedSize": 8,
                    }
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[0] == "unar":
            return subprocess.CompletedProcess(command, 1, "", "unar failed")
        if command[0] == "bsdtar":
            return subprocess.CompletedProcess(command, 2, "", "bsdtar failed")
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(UnsafeArchiveError, match="unar failed.*bsdtar failed"):
        extract_rar(archive, tmp_path / "out-rar", settings(tmp_path))


def test_extensionless_ooxml_packages_are_not_treated_as_generic_zip(
    tmp_path: Path,
) -> None:
    xlsx = tmp_path / "xlsx-without-extension"
    with zipfile.ZipFile(xlsx, "w") as output:
        output.writestr("[Content_Types].xml", "<Types/>")
        output.writestr("xl/workbook.xml", "<workbook/>")
    docx = tmp_path / "docx-without-extension"
    with zipfile.ZipFile(docx, "w") as output:
        output.writestr("[Content_Types].xml", "<Types/>")
        output.writestr("word/document.xml", "<document/>")

    assert detect_file_type(xlsx) == "xlsx"
    assert detect_file_type(docx) == "docx"


def test_generic_zip_remains_an_archive(tmp_path: Path) -> None:
    archive = tmp_path / "documents-without-extension"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("docs/specification.txt", "product | quantity")

    assert detect_file_type(archive) == "zip"
