from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import py7zr

from app.config import Settings


class UnsafeArchiveError(ValueError):
    pass


def _safe_target(root: Path, member_name: str) -> Path:
    member = member_name.replace("\\", "/")
    if member.startswith("/") or "\x00" in member:
        raise UnsafeArchiveError(f"Небезопасный путь в архиве: {member_name}")
    target = (root / member).resolve()
    resolved_root = root.resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeArchiveError(f"Archive path traversal: {member_name}") from exc
    return target


def _check_limits(count: int, total_size: int, compressed_size: int, settings: Settings) -> None:
    if count > settings.max_archive_members:
        raise UnsafeArchiveError(f"В архиве {count} файлов; лимит {settings.max_archive_members}")
    if total_size > settings.max_archive_uncompressed_bytes:
        raise UnsafeArchiveError("Распакованный размер архива превышает лимит")
    if compressed_size > 0 and total_size / compressed_size > settings.max_archive_compression_ratio:
        raise UnsafeArchiveError("Подозрение на zip bomb: compression ratio превышает лимит")


def extract_zip(path: Path, destination: Path, settings: Settings) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        _check_limits(
            len(members), sum(member.file_size for member in members),
            sum(member.compress_size for member in members), settings,
        )
        output: list[Path] = []
        for member in members:
            # Unix symlink bits in ZIP external attributes.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnsafeArchiveError(f"Symbolic link запрещён: {member.filename}")
            target = _safe_target(destination, member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            output.append(target)
        return output


def validate_zip_container(path: Path, settings: Settings) -> None:
    """Validate an OOXML/ZIP container without extracting it to disk."""
    with zipfile.ZipFile(path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        _check_limits(
            len(members),
            sum(member.file_size for member in members),
            sum(member.compress_size for member in members),
            settings,
        )
        virtual_root = Path("/office-container")
        for member in members:
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnsafeArchiveError(f"Symbolic link запрещён: {member.filename}")
            _safe_target(virtual_root, member.filename)


def extract_7z(path: Path, destination: Path, settings: Settings) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(path, mode="r") as archive:
        infos = archive.list()
        files = [info for info in infos if not info.is_directory]
        if any(bool(getattr(info, "is_symlink", False)) for info in files):
            raise UnsafeArchiveError("Symbolic links в 7Z запрещены")
        _check_limits(
            len(files),
            sum(int(info.uncompressed or 0) for info in files),
            sum(int(info.compressed or 0) for info in files),
            settings,
        )
        for info in files:
            _safe_target(destination, info.filename)
        archive.extractall(path=destination)
    result: list[Path] = []
    for file in destination.rglob("*"):
        if file.is_symlink():
            raise UnsafeArchiveError(f"Symbolic link запрещён: {file}")
        if file.is_file():
            result.append(file)
    return result


def _rar_members_from_lsar(payload: object) -> list[tuple[str, int, int]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("lsarContents"), list):
        raise UnsafeArchiveError("lsar вернул неожиданный формат списка RAR")
    members: list[tuple[str, int, int]] = []
    for entry in payload["lsarContents"]:
        if not isinstance(entry, dict) or bool(entry.get("XADIsDirectory")):
            continue
        name = entry.get("XADFileName")
        if not isinstance(name, str) or not name.strip():
            raise UnsafeArchiveError("RAR содержит файл без корректного имени")
        if bool(entry.get("XADIsLink")) or entry.get("XADLinkDestination"):
            raise UnsafeArchiveError(f"Symbolic link в RAR запрещён: {name}")
        try:
            size = int(entry.get("XADFileSize") or 0)
            compressed = int(entry.get("XADCompressedSize") or 0)
        except (TypeError, ValueError) as exc:
            raise UnsafeArchiveError(f"Некорректный размер RAR member: {name}") from exc
        if size < 0 or compressed < 0:
            raise UnsafeArchiveError(f"Отрицательный размер RAR member: {name}")
        members.append((name, size, compressed))
    return members


def extract_rar(path: Path, destination: Path, settings: Settings) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(
        [settings.lsar_binary, "-json", "-no-recursion", str(path)],
        capture_output=True,
        text=True,
        timeout=settings.conversion_timeout_seconds,
        check=True,
    )
    try:
        members = _rar_members_from_lsar(json.loads(listing.stdout))
    except json.JSONDecodeError as exc:
        raise UnsafeArchiveError("lsar не вернул валидный JSON для RAR") from exc
    for name, _, _ in members:
        _safe_target(destination, name)
    _check_limits(
        len(members),
        sum(size for _, size, _ in members),
        sum(compressed for _, _, compressed in members),
        settings,
    )
    subprocess.run(
        [
            settings.unar_binary,
            "-force-overwrite",
            "-no-directory",
            "-output-directory",
            str(destination),
            str(path),
        ],
        capture_output=True,
        timeout=settings.conversion_timeout_seconds,
        check=True,
    )
    output: list[Path] = []
    for file in destination.rglob("*"):
        if file.is_symlink():
            raise UnsafeArchiveError(f"Symbolic link запрещён: {file}")
        if file.is_file():
            _safe_target(destination, str(file.relative_to(destination)))
            output.append(file)
    actual_size = sum(file.stat().st_size for file in output)
    _check_limits(len(output), actual_size, path.stat().st_size, settings)
    return output


def extract_archive(path: Path, file_type: str, destination: Path, settings: Settings) -> list[Path]:
    if file_type == "zip":
        return extract_zip(path, destination, settings)
    if file_type == "7z":
        return extract_7z(path, destination, settings)
    if file_type == "rar":
        return extract_rar(path, destination, settings)
    raise ValueError(f"Unsupported archive type: {file_type}")
