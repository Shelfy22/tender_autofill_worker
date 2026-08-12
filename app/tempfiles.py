from __future__ import annotations

import shutil
from pathlib import Path


def cleanup_orphaned_temp_dirs(temp_root: Path) -> int:
    """Remove only service-owned directories left by a killed disposable child."""
    temp_root.mkdir(parents=True, exist_ok=True)
    resolved_root = temp_root.resolve()
    removed = 0
    for candidate in temp_root.glob("tender-*"):
        try:
            resolved = candidate.resolve()
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        shutil.rmtree(candidate)
        removed += 1
    return removed
