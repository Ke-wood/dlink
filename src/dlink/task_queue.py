"""任务队列：控制同时下载的任务数。

为什么需要（设计文档矛盾 7）：
    没有队列时，/api/task 收到 100 个 item 就起 100 个线程 → 100 × 8 连接，
    平台立刻掐。队列把"推送"和"开始下载"解耦。

设计：
    · 有界并发（默认 3 个任务同时下）
    · 暂停/继续/取消
    · 任务状态：queued → running → done/failed，或 paused
    · 状态变化通过回调通知（GUI 轮询 /api/tasks 即可）

不做：
    · 优先级（自用工具不需要）
    · 定时任务
    · 断点跨进程恢复（.parts 目录天然支持重跑续传）
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

QUEUED = "queued"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"
NEED_REFRESH = "need_refresh"


@dataclass
class Task:
    id: str
    name: str
    size: int = 0
    platform: str = "direct"
    status: str = QUEUED
    done: int = 0
    total: int = 0
    error: str = ""
    path: str = ""
    workers: int = 0
    workers_src: str = ""   # 并发数来源（平台默认/主机学习/任务指定）
    cleanup: str = ""       # 转存清理结果（空=未做）
    expires_at: float = 0.0 # 直链有效期（Unix 时间戳，0=未知）
    refresh_attempts: int = 0  # 续链次数
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    # 内部
    _payload: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "platform": self.platform,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "path": self.path,
            "workers": self.workers,
            "workers_src": self.workers_src,
            "cleanup": self.cleanup,
            "expires_at": self.expires_at,
            "refresh_attempts": self.refresh_attempts,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "speed": self.speed(),
            "eta": self.eta(),
        }

    def speed(self) -> float:
        """字节/秒。用总进度除以已运行时间，比瞬时值稳。"""
        if self.status != RUNNING or not self.started:
            return 0.0
        el = time.time() - self.started
        return self.done / el if el > 0.5 else 0.0

    def eta(self) -> Optional[float]:
        """预计剩余秒数。"""
        sp = self.speed()
        if sp <= 0 or self.total <= 0:
            return None
        return max(0.0, (self.total - self.done) / sp)


class Queue:
    def __init__(self, max_concurrent: int = 3) -> None:
        self.max_concurrent = max_concurrent
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._pending: deque[str] = deque()
        self._active: set[str] = set()
        self._runner: Optional[Callable[[Task], None]] = None
        self._threads: dict[str, threading.Thread] = {}

    def set_runner(self, fn: Callable[[Task], None]) -> None:
        """设置真正的下载执行函数（由 server 注入）。"""
        self._runner = fn

    # ---------- 增删 ----------

    def add(self, task: Task) -> None:
        with self._lock:
            self._tasks[task.id] = task
            if task.status == QUEUED:
                self._pending.append(task.id)
        self._pump()

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[dict]:
        with self._lock:
            return [t.to_dict() for t in self._tasks.values()]

    def remove(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            if t.status == RUNNING:
                t.status = CANCELED
            self._tasks.pop(task_id, None)
            try:
                self._pending.remove(task_id)
            except ValueError:
                pass
            self._active.discard(task_id)
        return True

    def clear_finished(self) -> int:
        with self._lock:
            dead = [k for k, t in self._tasks.items()
                    if t.status in (DONE, FAILED, CANCELED)]
            for k in dead:
                self._tasks.pop(k, None)
            return len(dead)

    # ---------- 状态控制 ----------

    def pause(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            if t.status == QUEUED:
                t.status = PAUSED
                try:
                    self._pending.remove(task_id)
                except ValueError:
                    pass
                return True
            if t.status == RUNNING:
                # 运行中的暂停只标记，由下载器在段边界响应
                t.status = PAUSED
                return True
        return False

    def resume(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t.status != PAUSED:
                return False
            t.status = QUEUED
            self._pending.append(task_id)
        self._pump()
        return True

    def requeue(self, task_id: str, front: bool = True) -> bool:
        """把任务放回队列（续链后用）。保留 .parts，会从断点续传。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            t.status = QUEUED
            t.error = ""
            try:
                self._pending.remove(task_id)
            except ValueError:
                pass
            if front:
                self._pending.appendleft(task_id)
            else:
                self._pending.append(task_id)
            self._active.discard(task_id)
        self._pump()
        return True

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            t.status = CANCELED
            try:
                self._pending.remove(task_id)
            except ValueError:
                pass
        return True

    # ---------- 调度 ----------

    def set_max_concurrent(self, n: int) -> None:
        with self._lock:
            self.max_concurrent = max(1, int(n))
        self._pump()

    def _pump(self) -> None:
        """尽可能多地启动排队任务。"""
        with self._lock:
            while self._pending and len(self._active) < self.max_concurrent:
                tid = self._pending.popleft()
                t = self._tasks.get(tid)
                if not t or t.status != QUEUED:
                    continue
                if not self._runner:
                    # 没有 runner 时直接失败，避免任务永远卡在 queued
                    t.status = FAILED
                    t.error = "内部错误：未设置下载执行器"
                    continue
                t.status = RUNNING
                t.started = time.time()
                self._active.add(tid)
                th = threading.Thread(target=self._run, args=(t,), daemon=True)
                self._threads[tid] = th
                th.start()

    def _run(self, task: Task) -> None:
        try:
            self._runner(task)  # type: ignore[misc]
            with self._lock:
                # need_refresh 是「等待外部恢复」的中间态，不要当失败
                if task.status == NEED_REFRESH:
                    pass
                elif task.status == RUNNING:
                    task.status = DONE if not task.error else FAILED
        except Exception as e:
            with self._lock:
                task.status = FAILED
                task.error = str(e)
        finally:
            with self._lock:
                task.finished = time.time()
                self._active.discard(task.id)
                self._threads.pop(task.id, None)
            self._pump()

    def stats(self) -> dict:
        with self._lock:
            by = {}
            for t in self._tasks.values():
                by[t.status] = by.get(t.status, 0) + 1
            total_speed = sum(t.speed() for t in self._tasks.values()
                              if t.status == RUNNING)
            return {
                "total": len(self._tasks),
                "by_status": by,
                "active": len(self._active),
                "pending": len(self._pending),
                "max_concurrent": self.max_concurrent,
                "total_speed": total_speed,
            }
