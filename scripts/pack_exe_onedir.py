#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 onedir 产物打成 zip（解压即用，不需要临时目录解压）。

用法：
    python scripts/pack_exe_onedir.py

产物：dist/dlink-v<version>-windows-x64-onedir.zip
     解压后是 `DLink-onedir/` 文件夹，里面有 DLink.exe。
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FOLDER = ROOT / "dist" / "DLink-onedir"
DIST = ROOT / "dist"


def main() -> int:
    if not (FOLDER / "DLink.exe").exists():
        print("先跑：python -m PyInstaller DLink-onedir.spec --noconfirm --clean")
        return 1

    from dlink import __version__ as version

    out = DIST / f"dlink-v{version}-windows-x64-onedir.zip"
    files = [p for p in sorted(FOLDER.rglob("*")) if p.is_file()]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            z.write(p, p.relative_to(FOLDER.parent))

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"已生成 {out.relative_to(ROOT)}")
    print(f"  文件数：{len(files)}")
    print(f"  大小：  {out.stat().st_size:,} bytes")
    print(f"  SHA256：{digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
