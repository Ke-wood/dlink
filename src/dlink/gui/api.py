"""GUI 与本地后端的通信封装。

为什么单独一层：
    GUI 只依赖这个类，不直接碰 urllib。将来换成 IPC 或 WebSocket 只需改这里。

设计：
    · 同步调用（PySide6 里放到 QThread/QTimer 里跑，或用短超时）
    · 端口自动探测（后端从 8765 起尝试）
    · 所有方法返回 dict 或 None（失败返回 None + 记录 last_error）
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

PORTS = (8765, 8766, 8767, 8768, 8769)


class ApiClient:
    def __init__(self, port: Optional[int] = None, timeout: float = 3.0) -> None:
        self.port = port
        self.timeout = timeout
        self.last_error = ""

    # ---------- 底层 ----------

    def _base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _request(self, method: str, path: str,
                 payload: Optional[dict] = None,
                 timeout: Optional[float] = None) -> Optional[Any]:
        if self.port is None and not self.discover():
            return None
        url = self._base() + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                self.last_error = body.get("error", str(e))
            except Exception:
                self.last_error = f"HTTP {e.code}"
            return None
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.port = None   # 下次重新探测
            return None

    # ---------- 连接 ----------

    def discover(self) -> bool:
        for p in PORTS:
            self.port = p
            j = self._request("GET", "/api/health", timeout=0.8)
            if j and j.get("app") == "dlink":
                return True
        self.port = None
        return False

    def health(self) -> Optional[dict]:
        return self._request("GET", "/api/health", timeout=1.5)

    # ---------- 任务 ----------

    def tasks(self) -> list[dict]:
        j = self._request("GET", "/api/tasks")
        return (j or {}).get("tasks") or []

    def task_action(self, task_id: str, action: str) -> bool:
        j = self._request("POST", "/api/task/action",
                          {"id": task_id, "action": action})
        return bool(j and j.get("ok"))

    def clear_finished(self) -> int:
        j = self._request("DELETE", "/api/tasks/clear")
        return int((j or {}).get("cleared") or 0)

    def add_task(self, items: list[dict], platform: str = "direct") -> Optional[dict]:
        return self._request("POST", "/api/task",
                             {"version": 1, "platform": platform, "items": items})

    # ---------- 配置 ----------

    def profiles(self) -> dict:
        j = self._request("GET", "/api/profiles")
        return (j or {}).get("profiles") or {}

    def save_profile(self, platform: str, **fields) -> Optional[dict]:
        payload = {"platform": platform, **fields}
        return self._request("POST", "/api/profiles", payload)

    def reset_profile(self, platform: str) -> Optional[dict]:
        return self._request("POST", "/api/profiles",
                             {"platform": platform, "_reset": True})

    def settings(self) -> dict:
        j = self._request("GET", "/api/settings")
        return (j or {}).get("settings") or {}

    def save_settings(self, **fields) -> Optional[dict]:
        return self._request("POST", "/api/settings", fields)

    # ---------- host 学习 ----------

    def hosts(self) -> dict:
        j = self._request("GET", "/api/hosts")
        return (j or {}).get("hosts") or {}

    def forget_host(self, host: str) -> bool:
        j = self._request("POST", "/api/hosts", {"host": host})
        return bool(j and j.get("ok"))
