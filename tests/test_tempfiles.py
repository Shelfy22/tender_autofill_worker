from app.tempfiles import cleanup_orphaned_temp_dirs


def test_cleanup_removes_only_service_owned_directories(tmp_path) -> None:
    orphan = tmp_path / "tender-old"
    orphan.mkdir()
    (orphan / "document.bin").write_bytes(b"data")
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    marker = tmp_path / "tender-file"
    marker.write_text("keep", encoding="utf-8")

    assert cleanup_orphaned_temp_dirs(tmp_path) == 1
    assert not orphan.exists()
    assert unrelated.exists()
    assert marker.exists()
