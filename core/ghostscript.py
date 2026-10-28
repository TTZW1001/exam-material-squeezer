from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Ghostscript discovery


def _version_key(path: Path) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", str(path.parent.parent))]
    return tuple(nums)


def find_ghostscript() -> Optional[str]:
    """Find a Ghostscript executable, preferring modern 64-bit installs."""
    env_names = (
        "EXAM_SQUEEZER_GS",
        "PDF_SQUEEZER_GS",
        "GS_PATH",
        "GHOSTSCRIPT_PATH",
    )
    for name in env_names:
        value = os.environ.get(name)
        if value:
            p = Path(value).expanduser()
            if p.is_file():
                return str(p)

    for name in ("gswin64c", "gswin32c", "gs"):
        found = shutil.which(name)
        if found:
            return found

    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "gs",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "gs",
        Path(r"G:\gs"),
        Path(r"C:\gs"),
        Path(r"D:\gs"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "gs",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root or not root.is_dir():
            continue
        for pattern in ("gs*/bin/gswin64c.exe", "gs*/bin/gswin32c.exe",
                        "bin/gswin64c.exe", "bin/gswin32c.exe"):
            try:
                candidates.extend(root.glob(pattern))
            except OSError:
                pass
    if not candidates:
        return None
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None
    candidates.sort(key=_version_key, reverse=True)
    return str(candidates[0])


def ghostscript_version(exe: str) -> str:
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
        return text.splitlines()[-1] if text else ""
    except Exception:
        return ""
