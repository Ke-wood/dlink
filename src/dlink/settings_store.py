"""全局设置的持久化存储。

为什么单独一个模块（而不是散落在 server.py 里）：
    · 用户改了「保存目录」，重启 exe 后必须还在 —— 曾经这里只存内存，
      是真实的 bug（用户反馈"换下载地址不生效"）。
    · 与 profile.py 共用同一个配置目录（%LOCALAPPDATA%/dlink），便于备份。
    · 原子写入（tmp + os.replace），避免写一半断电导致配置损坏。

存储位置：%LOCALAPPDATA%/dlink/settings.json
环境变量覆盖：DLINK_SETTINGS_FILE（测试用）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SETTINGS_FILE_ENV = "DLINK_SETTINGS_FILE"

# 默认值必须与 server.SETTINGS 的初始值保持一致。
DEFAULTS: dict[str, Any] = {
    # 用 resources.default_download_dir()，兼容中文 Windows 的「下载」目录
    "save_dir": "",   # 运行时填充（见下方 _defaults()）
    "max_concurrent": 3,
    "verify": True,
    "open_folder_when_done": False,
    "auto_cleanup_after_download": False,
}

# 只接受这些键，防止旧版本遗留字段或任意注入
ALLOWED_KEYS = tuple(DEFAULTS)


def _fill_defaults() -> None:
    """延迟填充 save_dir 默认值（避免导入时依赖 home 目录状态）。"""
    from .resources import default_download_dir
    DEFAULTS["save_dir"] = str(default_download_dir())


_fill_defaults()

# 类型约束：读回来时强转，避免 GUI 传字符串导致后续比较出错
_BOOL_KEYS = ("verify", "open_folder_when_done", "auto_cleanup_after_download")
_INT_KEYS = ("max_concurrent",)


def settings_path() -> Path:
    override = os.environ.get(SETTINGS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dlink" / "settings.json"


def _coerce(key: str, value: Any) -> Any:
    """把值转成该键应有的类型；失败则回退默认值。"""
    try:
        if key in _BOOL_KEYS:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if key in _INT_KEYS:
            return max(1, int(value))
        return str(value)
    except (TypeError, ValueError):
        return DEFAULTS[key]


def load_settings() -> dict[str, Any]:
    """读取磁盘设置，缺键用默认值补齐。任何异常都退回默认值。"""
    out = dict(DEFAULTS)
    p = settings_path()
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    raw = data.get("settings") if isinstance(data.get("settings"), dict) else data
    for key in ALLOWED_KEYS:
        if key in raw and raw[key] is not None:
            out[key] = _coerce(key, raw[key])
    return out


def save_settings(values: dict[str, Any]) -> Path:
    """把 values 合并到磁盘上的设置里并原子写入。返回文件路径。"""
    current = load_settings()
    for key in ALLOWED_KEYS:
        if key in values and values[key] is not None:
            current[key] = _coerce(key, values[key])

    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "settings": current}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p
