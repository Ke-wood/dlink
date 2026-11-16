"""资源路径解析（兼容开发态与 PyInstaller 打包态）。

为什么需要：
    PyInstaller onefile 模式下，模块被解压到临时目录（sys._MEIPASS），
    `Path(__file__).parent` 指向的是解压后的虚拟路径。
    直接写死相对路径在两种模式下会指向不同地方，必须集中处理。

用法：
    from .resources import asset_path, extension_src_dir, is_frozen

    icon = asset_path("icon256.png")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def is_frozen() -> bool:
    """是否运行在打包后的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """应用根目录。

    - 打包态：sys._MEIPASS（解压出的临时目录）
    - 开发态：仓库根目录（src 的上一级）
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def package_dir() -> Path:
    """dlink 包所在目录。"""
    return Path(__file__).resolve().parent


def asset_path(name: str) -> Path:
    """GUI 图标等资源。

    打包后 datas 把它们放在 dlink/gui/assets 下，所以两种模式
    都可以用「本模块同级的 assets 目录」定位。
    """
    return package_dir() / "gui" / "assets" / name


def assets_dir() -> Path:
    return package_dir() / "gui" / "assets"


def extension_src_dir() -> Optional[Path]:
    """浏览器扩展的源目录。

    开发态：仓库根目录下的 extension/
    打包态：datas 把它放在 <MEIPASS>/extension/
    找不到时返回 None（调用方应提示用户）。
    """
    cand = app_root() / "extension"
    if (cand / "manifest.json").exists():
        return cand
    # 兼容另一种可能的布局
    cand2 = package_dir().parent / "extension"
    if (cand2 / "manifest.json").exists():
        return cand2
    return None


def user_config_dir() -> Path:
    """用户配置目录（设置/profiles/hosts 都放这）。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dlink"


def default_download_dir() -> Path:
    """默认下载目录。

    优先用系统「下载」文件夹（在中文 Windows 上可能是「下载」，
    不能硬写 Downloads）。
    """
    home = Path.home()
    for name in ("Downloads", "下载"):
        cand = home / name
        if cand.is_dir():
            return cand / "DLink"
    return home / "Downloads" / "DLink"
