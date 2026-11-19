"""按平台分组的下载策略。

用户要求：**每种网盘和常规链接的设置必须分开**。
每个平台有自己的限速特性，一刀切必然踩坑（百度 4 路就会触发风控）。

存储位置：%LOCALAPPDATA%/dlink/profiles.json
优先级：用户配置 > 平台内置默认

拓展新平台时，只需在该平台的 adapter 里实现 default_profile()，
这里会自动兜底到 DEFAULT_PROFILE。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

PROFILE_FILE_ENV = "DLINK_PROFILE_FILE"


@dataclass
class DownloadProfile:
    """单个平台的下载参数。"""

    workers: int = 8
    seg_size: int = 32 * 1024 * 1024     # 32MB
    max_retries: int = 6
    timeout: float = 180.0

    # 高级选项
    force_single: bool = False            # 强制单连接（不管并发设置）
    respect_ranges: bool = True            # 是否尝试分段
    no_range_workers: int = 1              # 服务端不支持 Range 时的降级并发

    # 展示用
    note: str = ""                         # 给用户看的说明（GUI 显示）

    def effective_workers(self) -> int:
        return 1 if self.force_single else max(1, int(self.workers))


# 各平台内置默认值 —— 全部来自实测，不是拍的
BUILTIN: dict[str, DownloadProfile] = {
    # 常规 HTTP/HTTPS 直链：大多数服务器不限速，多段并行通常能跑满带宽
    "direct": DownloadProfile(
        workers=8, seg_size=32 * 1024 * 1024,
        note="大多数 HTTP 服务器不限速，多段并行通常能跑满带宽。"
             "若服务器不支持 Range，会自动降为单连接。",
    ),
    # 百度：账号级配额，实测恒定 80KB/s，并发反而触发风控
    "baidu": DownloadProfile(
        workers=1, force_single=True,
        note="百度是账号级限速（约 80KB/s），实测并发无效且会触发风控。"
             "BaiduPCS-Go 作者也建议并发设为 1。",
    ),
    # 微云：连接级限速，单连接 14MB 后掉到 240B/s，新连接满速
    "weiyun": DownloadProfile(
        workers=8, seg_size=32 * 1024 * 1024,
        note="微云限的是连接年龄（单连接约 14MB 后降速），"
             "分段并行实测有效（8 路可达 9.7MB/s）。",
    ),
    # 夸克：二阶段限速，窗口期快，限流期总量恒定
    "quark": DownloadProfile(
        workers=6, seg_size=16 * 1024 * 1024,
        note="夸克是二阶段限速：窗口期可到 15-20MB/s，"
             "18:00-23:00 可能限流到约 2MB/s。限流期建议降到 2 路。",
    ),
    "uc": DownloadProfile(
        workers=6, seg_size=16 * 1024 * 1024,
        note="与夸克同架构（阿里系），推测同样的二阶段限速。尚未实测。",
    ),
    # Cloudreve：直链是对象存储（实测 Cloudflare R2）的预签名 URL，
    # 走 CDN、支持 Range、不限速，可以放心多段并行。
    "cloudreve": DownloadProfile(
        workers=8, seg_size=32 * 1024 * 1024,
        note="Cloudreve 直链是对象存储预签名 URL（走 CDN），支持分段并行；"
             "但签名有效期很短（实测 1 小时），过期需要重新解析。",
    ),
}

DEFAULT_PROFILE = DownloadProfile(note="未知平台，使用通用默认值。")


def profile_path() -> Path:
    override = os.environ.get(PROFILE_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dlink" / "profiles.json"


def load_user_profiles() -> dict:
    p = profile_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("profiles") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def get_profile(platform: str) -> DownloadProfile:
    """取某平台的有效配置：用户覆盖 > 内置默认 > 通用默认。"""
    base = BUILTIN.get(platform) or DEFAULT_PROFILE
    user = load_user_profiles().get(platform) or {}
    if not user:
        return base

    merged = asdict(base)
    for k, v in user.items():
        if k in merged:
            merged[k] = v
    return DownloadProfile(**merged)


def all_profiles() -> dict[str, DownloadProfile]:
    """所有已知平台的有效配置（GUI 设置页用）。"""
    names = set(BUILTIN) | set(load_user_profiles())
    return {n: get_profile(n) for n in sorted(names)}


def save_profile(platform: str, **kwargs) -> Path:
    """保存某平台的用户配置。"""
    p = profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "profiles": load_user_profiles()}
    entry = dict(data["profiles"].get(platform) or {})
    entry.update({k: v for k, v in kwargs.items() if v is not None})
    data["profiles"][platform] = entry
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def reset_profile(platform: str) -> Path:
    p = profile_path()
    data = {"version": 1, "profiles": load_user_profiles()}
    data["profiles"].pop(platform, None)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p
