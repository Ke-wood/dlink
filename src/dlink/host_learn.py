"""按主机学习最优并发数。

为什么需要（实测驱动）：
    同样的并发设置，在不同 HTTP 主机上结果完全相反：

        proof.ovh.net          1路 4.91 → 4路 8.19 → 8路 3.07 MB/s  （4 路最优，8 路崩）
        registry.npmmirror.com 1路 2.72 → 4路 8.59 → 8路 11.55 MB/s （越多越快）

    所以对「普通链接」写死一个并发数是错的。

设计取舍：
    · 不为每次下载跑测速（浪费流量和时间）
    · 首次用平台默认；下载过程中**观察失败信号**（大量 403/连接被拒/0 速段）
    · 若判定为"限并发"，下调并**记住这个主机**
    · 若表现良好，也记住（下次直接用）

    这是一个"事后学习"模型，比"事前探测"省得多。

存储：%LOCALAPPDATA%/dlink/hosts.json
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

HOSTS_FILE_ENV = "DLINK_HOSTS_FILE"
_LOCK = threading.RLock()

# 判定的阈值
BAD_RATIO = 0.3      # 失败段占比超过 30% 视为"限并发"
GOOD_RATIO = 0.05    # 失败段占比低于 5% 视为"良好"
MAX_WORKERS = 16
MIN_WORKERS = 1


def hosts_path() -> Path:
    override = os.environ.get(HOSTS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dlink" / "hosts.json"


def _load() -> dict:
    p = hosts_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("hosts") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> Path:
    p = hosts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "hosts": data},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def learned_workers(url: str) -> Optional[int]:
    """取该主机学到的并发数；没学过返回 None。"""
    h = host_of(url)
    if not h:
        return None
    with _LOCK:
        entry = _load().get(h)
    if not entry:
        return None
    w = entry.get("workers")
    try:
        w = int(w)
    except (TypeError, ValueError):
        return None
    return max(MIN_WORKERS, min(MAX_WORKERS, w))


def record(url: str, workers: int, attempted: int, failed: int,
           speed: float = 0.0) -> Optional[Path]:
    """记录一次下载结果，必要时调整该主机的学习值。

    url       : 下载地址
    workers   : 本次用的并发数
    attempted : 计划的分段数
    failed    : 失败/被拒的分段数
    speed     : 实测聚合速度（字节/秒）
    """
    h = host_of(url)
    if not h or attempted <= 0:
        return None

    ratio = failed / attempted
    with _LOCK:
        data = _load()
        cur = data.get(h) or {}
        prev_workers = int(cur.get("workers") or workers)
        new_workers = prev_workers
        reason = ""

        if ratio >= BAD_RATIO and workers > MIN_WORKERS:
            # 限并发：减半，并记住这个值试过且失败，避免下次又升回去
            new_workers = max(MIN_WORKERS, workers // 2)
            bad = list(cur.get("bad_workers") or [])
            if workers not in bad:
                bad.append(workers)
            reason = f"{workers} 路时 {failed}/{attempted} 段失败，降到 {new_workers}"
            ceiling = new_workers
        elif ratio <= GOOD_RATIO:
            # 表现良好：**保持当前值，不自动升档**。
            # 曾经考虑过"成功后升一档以探索更高速度"，但实测会震荡：
            # ovh 8 路失败(3.07) → 4 路成功(8.19) → 升回 7 路又接近失败区。
            # 保守策略：只降不升，用户想更快可以手动在 GUI 里调高。
            new_workers = workers
            bad = list(cur.get("bad_workers") or [])
            reason = f"{workers} 路表现良好（失败率 {ratio:.0%}），保持"
        else:
            bad = list(cur.get("bad_workers") or [])
            reason = f"{workers} 路，失败率 {ratio:.0%}，维持"

        data[h] = {
            "workers": new_workers,
            "last_workers": workers,
            "bad_workers": sorted(set(bad)),
            "fail_ratio": round(ratio, 3),
            "speed": int(speed),
            "reason": reason,
            "updated": int(time.time()),
        }
        return _save(data)


def all_hosts() -> dict:
    with _LOCK:
        return _load()


def forget(url_or_host: str) -> Path:
    """忘掉某主机（GUI 里"重置"用）。"""
    h = host_of(url_or_host) or url_or_host.lower()
    with _LOCK:
        data = _load()
        data.pop(h, None)
        return _save(data)


def forget_all() -> Path:
    with _LOCK:
        return _save({})


def effective_workers(url: str, platform_default: int) -> tuple[int, str]:
    """综合平台默认与主机学习，给出最终并发数 + 来源说明。

    ⚠️ 学习值**只能往下走，不能突破平台上限**。
    否则一个在普通链接上学到的 8 路，会把百度的 1 路上限顶掉 ——
    百度是账号级限速，实测 8 路掉到 9 KB/s（1 路 75、3 路 130）。
    这个上限必须由平台策略说了算，学习值只是"比默认更保守"的补充。
    """
    base = max(MIN_WORKERS, int(platform_default))
    learned = learned_workers(url)
    if learned is None:
        return base, "平台默认"
    if learned > base:
        return base, (f"平台上限 {base} 路"
                      f"（主机学到 {learned} 路，已按平台策略封顶）")
    return learned, f"按主机学习值（{host_of(url)}）"
