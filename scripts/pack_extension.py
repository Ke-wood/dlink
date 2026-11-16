#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 extension/ 打成发布用的 zip。

用法：
    python scripts/pack_extension.py

产物：dist/dlink-extension-v<version>.zip
     内部结构保留顶层 `extension/` 目录 —— 用户解压后直接拿这个文件夹去
     「加载解压缩的扩展」，不会把一堆 js 散落到下载目录里。
版本号从 manifest.json 读，避免手写打错。
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"
DIST = ROOT / "dist"

# 不该进包的（开发残留）
SKIP_NAMES = {".DS_Store", "Thumbs.db"}
SKIP_DIRS = {"__pycache__", ".git"}


def main() -> int:
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    DIST.mkdir(exist_ok=True)
    out = DIST / f"dlink-extension-v{version}.zip"

    files = [
        p for p in sorted(EXT.rglob("*"))
        if p.is_file()
        and p.name not in SKIP_NAMES
        and not (set(p.relative_to(EXT).parts[:-1]) & SKIP_DIRS)
    ]
    if not files:
        print("extension/ 是空的？")
        return 1

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, p.relative_to(EXT.parent))

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"已生成 {out.relative_to(ROOT)}")
    print(f"  文件数：{len(files)}")
    print(f"  大小：  {out.stat().st_size:,} bytes")
    print(f"  SHA256：{digest}")
    print(f"  manifest版本：{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
