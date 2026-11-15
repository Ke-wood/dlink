"""直链续期：链接过期后自动向扩展要一条新的。

为什么必须做：
    大文件下载时间远超直链有效期。
      · 百度 expires=8h
      · 夸克约 6h
      · 微云签名更短
    1.5GB 在百度 80KB/s 下要 5.4 小时 —— 一次下载中途必然过期。

流程（扩展始终是发起方，避免端口/权限问题）：

    下载器                            扩展 (service worker)
    ─────                            ─────────────────────
    检测到 401/403
      → 任务标记 need_refresh  ──轮询──▶  GET /api/pending-refresh
                                        │ 拿到 {task_id, refresh:{platform, shareUrl, fid}}
                                        │ 在对应页面重新执行 injected.js
                                        │ 取到新直链
      ◀──────────────────────────────────  POST /api/refresh (task_id, new link)
    恢复下载

设计要点：
    · 保留已下载的分段（.parts 目录），续链后从断点继续，不重下
    · 有超时：扩展 60 秒没响应就标为「续链失败，需手动处理」
    · 记录续链次数，避免无限循环
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

MAX_REFRESH_ATTEMPTS = 5          # 最多续链 5 次
REFRESH_TIMEOUT_SEC = 90          # 扩展必须在 90 秒内响应


@dataclass
class RefreshRequest:
    """一条待续链请求（扩展来取）。"""

    task_id: str
    platform: str
    share_url: str
    fid: str
    created: float = field(default_factory=time.time)
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "platform": self.platform,
            "share_url": self.share_url,
            "fid": self.fid,
            "created": self.created,
            "age": time.time() - self.created,
            "attempts": self.attempts,
        }


class RefreshManager:
    """管理「需要续链」的任务队列。

    server 在下载失败且原因是链接过期时调用 `request()`；
    扩展轮询 `pending()` 拿走请求，取到新链接后 `fulfill()`。
    """

    def __init__(self) -> None:
        self._pending: dict[str, RefreshRequest] = {}
        self._fulfilled: dict[str, dict] = {}

    # ---------- 下载器侧 ----------

    def request(self, task_id: str, refresh: Optional[dict],
                attempts: int = 0) -> bool:
        """登记一个续链请求。没有 refresh 信息（普通链接）时返回 False。"""
        if not refresh or not refresh.get("shareUrl"):
            return False
        if attempts >= MAX_REFRESH_ATTEMPTS:
            return False
        self._pending[task_id] = RefreshRequest(
            task_id=task_id,
            platform=refresh.get("platform") or "",
            share_url=refresh.get("shareUrl") or "",
            fid=str(refresh.get("fid") or ""),
            attempts=attempts,
        )
        return True

    def pending(self) -> list[dict]:
        """扩展轮询用。同时把超时的请求标记出来。"""
        now = time.time()
        out = []
        for r in list(self._pending.values()):
            if now - r.created > REFRESH_TIMEOUT_SEC:
                # 超时：不算它还在等，返回一个 timeout 标记让扩展/GUI 知道
                out.append({**r.to_dict(), "timed_out": True})
                self._pending.pop(r.task_id, None)
            else:
                out.append(r.to_dict())
        return out

    def take(self, task_id: str) -> Optional[dict]:
        r = self._pending.pop(task_id, None)
        return r.to_dict() if r else None

    def has_pending(self, task_id: str) -> bool:
        return task_id in self._pending

    # ---------- 扩展回填 ----------

    def fulfill(self, task_id: str, link: dict) -> bool:
        """扩展取到新直链后调用。交给 server 恢复下载。"""
        if task_id not in self._pending:
            return False
        self._pending.pop(task_id, None)
        self._fulfilled[task_id] = link
        return True

    def pop_fulfilled(self, task_id: str) -> Optional[dict]:
        return self._fulfilled.pop(task_id, None)

    def cancel(self, task_id: str) -> None:
        self._pending.pop(task_id, None)
        self._fulfilled.pop(task_id, None)

    def stats(self) -> dict:
        return {
            "pending": len(self._pending),
            "fulfilled": len(self._fulfilled),
            "pending_ids": list(self._pending.keys()),
        }
