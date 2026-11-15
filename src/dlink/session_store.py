"""登录态/会话的本地存储。

设计取舍：
- 网盘 cookie 是**活凭证**，绝不能进仓库、不能进日志。
- 落盘位置默认 `%LOCALAPPDATA%\\dlink\\sessions.json`（Linux/macOS 用 ~/.local/state/dlink）。
- 文件权限尽量收紧（Windows 上用 ACL 太麻烦，改为约定 + CLI 首次写入时提醒）。
- 导出时做脱敏，任何 `show` 类命令都不打印完整 cookie。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SESSION_FILE_ENV = "DLINK_SESSION_FILE"


def session_path() -> Path:
    override = os.environ.get(SESSION_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "dlink" / "sessions.json"


def load() -> dict:
    p = session_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(data: dict) -> Path:
    p = session_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        if os.name != "nt":
            os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def put(platform: str, cookies: dict, ua: str = "", extra: dict | None = None) -> Path:
    data = load()
    entry = {"cookies": cookies, "ua": ua}
    if extra:
        entry.update(extra)
    data[platform] = entry
    return save(data)


def get(platform: str) -> dict | None:
    return load().get(platform)


def redact(cookies: dict) -> dict:
    """脱敏：只留前 4 位，其余打码。用于任何面向屏幕的输出。"""
    out = {}
    for k, v in cookies.items():
        v = str(v)
        out[k] = (v[:4] + "*" * max(0, len(v) - 4)) if len(v) > 4 else "****"
    return out
