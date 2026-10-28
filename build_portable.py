#!/usr/bin/env python3
"""Build the portable Windows executable for GitHub Releases."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
EXE_NAME = "ExamMaterialSqueezer"
TARGET_NAME = f"{EXE_NAME}-v1.0.0-portable.exe"


def read_version() -> str:
    text = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Could not read APP_VERSION from ui/theme.py")
    return match.group(1)


def main() -> int:
    version = read_version()
    if version != "1.0.0":
        raise SystemExit(f"Expected v1.0.0, found {version}")

    for folder in (DIST, BUILD):
        if folder.exists():
            shutil.rmtree(folder)

    data_sep = ";" if os.name == "nt" else ":"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        EXE_NAME,
        "--icon",
        str(ROOT / "assets" / "icon.ico"),
        "--add-data",
        f"{ROOT / 'assets'}{data_sep}assets",
        "--collect-all",
        "PIL",
        "--version-file",
        str(ROOT / "packaging" / "version_info.txt"),
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        str(ROOT / "app.py"),
    ]
    print("Running:", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)

    built = DIST / f"{EXE_NAME}.exe"
    if not built.is_file():
        raise SystemExit(f"Build output not found: {built}")

    target = DIST / TARGET_NAME
    built.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    checksum = DIST / f"{TARGET_NAME}.sha256"
    checksum.write_text(f"{digest}  {TARGET_NAME}\n", encoding="utf-8")

    print()
    print(f"Portable executable: {target}")
    print(f"Size: {target.stat().st_size / (1024 * 1024):.1f} MB")
    print(f"SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
