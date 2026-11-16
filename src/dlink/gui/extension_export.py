"""导出浏览器扩展。

为什么需要：
    用户只下载了一个 DLink.exe，装完发现"还要个扩展"，但不知道去哪找。
    所以把扩展打进 exe，并在菜单里提供「导出扩展」——选中一个文件夹，
    自动释放扩展文件，用户再去浏览器里"加载已解压的扩展"。

这样就做到真正的自包含：一个 exe 分发所有东西。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..resources import extension_src_dir

# 需要释放的文件（相对 extension/）
EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "content.js",
    "injected.js",
    "platforms.js",
    "popup.html",
    "popup.js",
    "README.md",
    "icons/icon16.png",
    "icons/icon48.png",
    "icons/icon128.png",
)


class ExtensionExportError(Exception):
    pass


def export_extension(target_dir: Path, overwrite: bool = True) -> tuple[Path, int]:
    """把扩展复制到 target_dir/。返回 (目标子目录, 复制文件数)。"""
    src = extension_src_dir()
    if src is None:
        raise ExtensionExportError(
            "找不到内置的扩展文件（打包时未包含 extension 目录）")

    dest = Path(target_dir) / "dlink-extension"
    dest.mkdir(parents=True, exist_ok=True)

    n = 0
    for rel in EXTENSION_FILES:
        s = src / rel
        if not s.exists():
            continue
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists() and not overwrite:
            continue
        shutil.copy2(s, d)
        n += 1

    if n == 0:
        raise ExtensionExportError("没有任何扩展文件可导出")

    # 额外写一份安装说明，用户不用回来翻 README
    readme = dest / "安装说明.txt"
    readme.write_text(
        "DLink 浏览器扩展\n"
        "================\n\n"
        "安装步骤（Edge 或 Chrome）：\n"
        "  1. 地址栏输入 edge://extensions/ 或 chrome://extensions/\n"
        "  2. 打开左下角（或右上角）的「开发人员模式」\n"
        "  3. 点「加载解压缩的扩展」，选择本文件夹\n"
        "  4. 装好后在网盘分享页右下角会出现「↓ 提取直链」按钮\n\n"
        "注意：\n"
        "  · 需要先运行 DLink.exe（下载器），扩展才能把任务发过去\n"
        "  · 扩展只负责取直链，下载由 DLink.exe 完成\n",
        encoding="utf-8",
    )
    n += 1

    return dest, n
