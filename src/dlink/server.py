"""本地 HTTP API + 任务队列。

职责：
  · 接收扩展推送的下载任务（POST /api/task）
  · 用队列控制并发（避免 100 个文件 = 100 个并发下载）
  · 按平台取下载策略（每平台独立的 workers/seg_size）
  · 给 GUI 提供任务状态、配置的读写接口

端口：从 8765 起尝试，最多 5 个。只监听 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 允许 `python -m dlink.server` 或直接跑本文件
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlink.models import DirectLink  # noqa: E402
from dlink import __version__  # noqa: E402
from dlink.host_learn import effective_workers as learn_workers  # noqa: E402
from dlink.host_learn import all_hosts, forget as forget_host, record as record_host  # noqa: E402
from dlink.profile import all_profiles, get_profile, reset_profile, save_profile  # noqa: E402
from dlink.refresh import RefreshManager  # noqa: E402
from dlink.resources import default_download_dir  # noqa: E402
from dlink.settings_store import load_settings, save_settings  # noqa: E402
from dlink.task_queue import FAILED, NEED_REFRESH, QUEUED, Queue, Task  # noqa: E402

PORT_RANGE = (8765, 8766, 8767, 8768, 8769)
# 从包版本取，不硬编码 —— 曾经写死 "0.1.0"，导致 /api/health 永远报旧版本，
# 发布后无法确认用户装的到底是哪一版（scripts/check_extension.py 会检查这个）
VERSION = __version__
DEFAULT_MAX_CONCURRENT = 3

# 保存目录等全局设置（可由 GUI 修改）
# 设置来自磁盘（%LOCALAPPDATA%/dlink/settings.json），没文件时用内置默认。
# ⚠️ 必须在这里加载而不是写死字面量 —— 否则用户改了保存目录，重启就还原。
SETTINGS: dict = load_settings()
SETTINGS_LOCK = threading.Lock()

# 并发数取磁盘设置里的值（用户改过就沿用）
QUEUE = Queue(max_concurrent=max(1, int(SETTINGS.get("max_concurrent") or DEFAULT_MAX_CONCURRENT)))
REFRESH = RefreshManager()

# 扩展最后联系时间（GUI 据此判断扩展是否活着，用于"续链没人理"的诊断）
LAST_EXTENSION_CONTACT = {"ts": 0.0, "ua": ""}
CONTACT_LOCK = threading.Lock()


def touch_extension(ua: str = "") -> None:
    with CONTACT_LOCK:
        LAST_EXTENSION_CONTACT["ts"] = time.time()
        LAST_EXTENSION_CONTACT["ua"] = ua


def extension_info() -> dict:
    with CONTACT_LOCK:
        ts = LAST_EXTENSION_CONTACT["ts"]
    return {
        "last_contact": ts,
        "seconds_ago": (time.time() - ts) if ts else None,
        "alive": bool(ts and (time.time() - ts) < 90),
    }


# ---------------- 下载执行 ----------------

def task_to_link(task: Task) -> DirectLink:
    item = task._payload
    return DirectLink(
        url=item["url"],
        cookies=item.get("cookies") or {},
        headers=item.get("headers") or {},
        size=int(item.get("size") or 0),
        sha1=(item.get("sha1") or "").lower(),
        md5=(item.get("md5") or "").lower(),
        filename=item.get("filename") or task.name or "download.bin",
    )


_DEST_LOCK = threading.Lock()
_RESERVED_DESTS: set[str] = set()

# Windows 保留设备名，做文件名会直接创建失败
_RESERVED_STEMS = ({"con", "prn", "aux", "nul"}
                   | {f"com{i}" for i in range(1, 10)}
                   | {f"lpt{i}" for i in range(1, 10)})


def safe_basename(name: str) -> str:
    """把任意来源的文件名收敛成一个安全的**文件名**（不含任何目录）。

    ⚠️ 为什么必须做：接管浏览器下载时，扩展拿到的 `item.filename` 是
    浏览器给的**完整本地路径**（如 `C:\\Users\\x\\Downloads\\a.zip`）。
    `Path(save_dir) / "C:/Users/x/Downloads/a.zip"` 在 Windows 上会把
    整个 save_dir 替换掉 —— 文件被写到下载目录之外。另外浏览器文件名
    里可能带 `..`、`<>:"|?*`，同样要清掉。
    """
    s = str(name or "").replace("\\", "/").split("/")[-1]
    s = "".join(ch for ch in s if ch not in '<>:"|?*' and ord(ch) >= 32)
    s = s.rstrip(". ")
    if not s:
        return "download.bin"
    stem = s.partition(".")[0]
    if stem.lower() in _RESERVED_STEMS:
        s = "_" + s
    return s[:180]


def unique_dest(save_dir: Path, name: str) -> Path:
    """分配一个未被占用的目标路径。

    ⚠️ 必须加锁 + 记住已分配的路径。否则两个同名任务同时进来，
    都看到"文件不存在"，就会用同一个 dest，进而共用同一个
    `<dest>.parts` 目录 —— 分段文件互相覆盖，合并后文件会多出字节
    （实测：29,701,389 字节的文件合成了 29,832,461，多 131,072）。
    """
    with _DEST_LOCK:
        name = safe_basename(name)
        stem, suf = os.path.splitext(name)
        dest = save_dir / name
        i = 1
        while dest.exists() or str(dest) in _RESERVED_DESTS:
            dest = save_dir / f"{stem}({i}){suf}"
            i += 1
        _RESERVED_DESTS.add(str(dest))
        return dest


def release_dest(dest: Path) -> None:
    with _DEST_LOCK:
        _RESERVED_DESTS.discard(str(dest))


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:80]


# 哪些错误值得尝试续链？
#   实测：不同平台/不同过期情形返回的状态码并不统一 —— 百度分享直链过期
#   可能是 400/403，夸克是 412，微云是 401。所以不能只认 401/403。
_EXPIRE_HINTS = (
    "400", "401", "403", "404", "410", "412", "416", "http 4",
    "expired", "invalid", "过期", "失效", "sign", "token",
    "authorization", "forbidden", "unauthorized",
)


def _looks_like_expired(e: Exception) -> bool:
    """粗判：是不是「换条直链就能好」的错误。

    宁可多续一次也不要漏 —— 续链失败会退回 failed，代价可控；
    漏判则用户必须手动重来。
    """
    from dlink.models import LinkExpired
    if isinstance(e, LinkExpired):
        return True
    msg = str(e).lower()
    return any(h in msg for h in _EXPIRE_HINTS)


def run_task(task: Task) -> None:
    """由 Queue 调用：真正下载一个任务。"""
    from dlink.downloader import download

    item = task._payload
    with SETTINGS_LOCK:
        save_dir = Path(SETTINGS["save_dir"]).expanduser()
    save_dir.mkdir(parents=True, exist_ok=True)

    link = task_to_link(task)

    # 普通链接常有 size=0（扩展没探测）。这里先探测并填好，
    # 避免下载器内部 _probe_size 用 180s 超时长时间挂起。
    if not link.size and (task.platform or "direct") == "direct":
        try:
            from dlink.platforms.direct import DirectAdapter
            probed = DirectAdapter().probe(link.url, timeout=15)
            if probed.get("size"):
                link.size = probed["size"]
                task.total = link.size
            if probed.get("filename") and not link.filename:
                link.filename = probed["filename"]
        except Exception as e:
            task.error = f"探测失败：{e}"

    dest = unique_dest(save_dir, link.filename)
    task.path = str(dest)

    # ★ 按平台取策略。item 显式给了就以 item 为准。
    #   这是修复「所有任务都按默认 4 路跑，百度因此触发风控」的关键。
    prof = get_profile(task.platform or "direct")
    if item.get("workers"):
        workers = int(item["workers"])
        workers_src = "任务指定"
    else:
        workers, workers_src = learn_workers(link.url, prof.effective_workers())
    task.workers_src = workers_src
    seg_size = int(item.get("seg_size") or prof.seg_size)
    timeout = float(item.get("timeout") or prof.timeout)
    task.workers = workers

    def progress(done: int, total: int) -> None:
        task.done = done
        if total:
            task.total = total

    try:
        res = download(link, str(dest), workers=workers, seg_size=seg_size,
                       timeout=timeout, progress=progress)
    except Exception as e:
        # 判断是否属于「直链可能失效」类错误 → 值得尝试续链
        if _looks_like_expired(e):
            attempts = int(task.refresh_attempts or 0)
            if REFRESH.request(task.id, item.get("refresh"), attempts):
                task.status = NEED_REFRESH
                task.refresh_attempts = attempts + 1
                task.error = (f"直链可能已失效（{_short(e)}），"
                              f"已请求扩展重新取链（第 {attempts + 1} 次）")
                return
            task.error = (f"直链失效且无法自动续链：{_short(e)}"
                          "（超出重试次数，或该平台没有 refresh 信息）")
            raise
        raise

    # 按主机学习：只在普通链接上做（网盘有平台级策略，不需要按 CDN 域名学）
    if (task.platform or "direct") == "direct":
        try:
            segs = int(res.get("segments") or 0)
            if segs > 0:
                ratio_bad = 0 if res.get("ok") else segs
                record_host(link.url, workers=workers, attempted=segs,
                            failed=ratio_bad,
                            speed=(task.done / max(1e-6, time.time() - task.started)))
        except Exception:
            pass

    if not res.get("ok"):
        task.error = res.get("error") or "下载失败"
    else:
        # 下载成功 → 按设置决定是否清理转存产生的临时文件
        with SETTINGS_LOCK:
            do_clean = SETTINGS.get("auto_cleanup_after_download")
        transfer = item.get("transfer")
        if do_clean and transfer:
            try:
                from dlink.cleanup import cleanup_transfer
                r = cleanup_transfer(transfer, item.get("cookies") or {},
                                     item.get("headers") or {})
                task.cleanup = r.get("message", "")
            except Exception as e:
                task.cleanup = f"清理异常：{e}"


QUEUE.set_runner(run_task)


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"DLink/{VERSION}"

    def log_message(self, fmt, *args):
        """请求日志。

        ⚠️ 必须防 sys.stderr 为 None：打包成 exe（console=False）后，
        windowed 进程里 sys.stderr 是 None。直接 `.write()` 会抛
        AttributeError，而 BaseHTTPRequestHandler 是在**每个请求的线程**
        里调 log_message 的 —— 异常会把线程带走，客户端看到的是
        「连接被重置且没有任何响应」。实测：exe 里所有 POST 全挂，
        而 GET /api/health 因为跳过日志所以看起来一切正常，极难排查。
        """
        if self.path.split("?")[0] == "/api/health":
            return
        try:
            stream = sys.stderr
            if stream is not None:
                stream.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")
        except Exception:
            pass

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ---------- GET ----------

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/health":
            touch_extension(self.headers.get("User-Agent", ""))
            self._json(200, {
                "app": "dlink", "version": VERSION, "ok": True,
                "queue": QUEUE.stats(),
                "extension": extension_info(),
            })
        elif path == "/api/tasks":
            self._json(200, {"tasks": QUEUE.all(), "stats": QUEUE.stats()})
        elif path == "/api/profiles":
            self._json(200, {
                "profiles": {n: asdict(p) for n, p in all_profiles().items()},
            })
        elif path == "/api/settings":
            with SETTINGS_LOCK:
                self._json(200, {"settings": dict(SETTINGS)})
        elif path == "/api/pending-refresh":
            # 扩展轮询：拿需要续链的任务
            self._json(200, {"pending": REFRESH.pending(),
                             "stats": REFRESH.stats()})
        elif path == "/api/extension-status":
            self._json(200, extension_info())
        elif path == "/api/refresh":
            self._handle_refresh(payload)
        elif path == "/api/hosts":
            self._json(200, {"hosts": all_hosts()})
        else:
            self._json(404, {"error": "not found"})

    # ---------- POST ----------

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        if path == "/api/task":
            touch_extension(self.headers.get("User-Agent", ""))
            self._handle_task(payload)
        elif path == "/api/profiles":
            self._handle_profile_update(payload)
        elif path == "/api/settings":
            self._handle_settings(payload)
        elif path == "/api/task/action":
            self._handle_task_action(payload)
        elif path == "/api/refresh":
            self._handle_refresh(payload)
        elif path == "/api/hosts":
            h = payload.get("host")
            if payload.get("_all") or not h:
                from dlink.host_learn import forget_all as _fa
                pth = _fa()
            else:
                pth = forget_host(h)
            self._json(200, {"ok": True, "path": str(pth), "hosts": all_hosts()})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.split("?")[0] == "/api/tasks/clear":
            n = QUEUE.clear_finished()
            self._json(200, {"ok": True, "cleared": n})
        else:
            self._json(404, {"error": "not found"})

    # ---------- handlers ----------

    def _handle_task(self, payload: dict) -> None:
        items = payload.get("items") or []
        if not items:
            self._json(400, {"error": "items 为空"})
            return

        platform = payload.get("platform") or "direct"
        ids, skipped = [], []

        for idx, item in enumerate(items):
            tid = f"{int(time.time() * 1000)}-{idx}"
            # 接管浏览器下载时 filename 是完整本地路径，列表里不能显示整条路径
            raw_name = item.get("filename") or item.get("name") or ""
            name = safe_basename(raw_name) if raw_name else "(未知)"

            # 没有 url 的条目（取链失败/超单次上限）也落成失败任务，
            # 否则用户以为文件丢了（设计文档矛盾 3）
            if not item.get("url"):
                t = Task(id=tid, name=name, size=item.get("size") or 0,
                         platform=item.get("platform") or platform)
                t.status = FAILED
                t.error = item.get("error") or "未取到直链"
                QUEUE._tasks[tid] = t
                skipped.append(tid)
                ids.append(tid)
                continue

            t = Task(id=tid, name=name, size=item.get("size") or 0,
                     total=item.get("size") or 0,
                     platform=item.get("platform") or platform,
                     status=QUEUED,
                     expires_at=float(item.get("expires_at") or 0))
            t._payload = item
            ids.append(tid)
            QUEUE.add(t)

        with SETTINGS_LOCK:
            save_dir = SETTINGS["save_dir"]
        self._json(200, {
            "ok": True,
            "accepted": len(ids) - len(skipped),
            "skipped": len(skipped),
            "task_ids": ids,
            "skipped_ids": skipped,
            "save_dir": save_dir,
        })

    def _handle_profile_update(self, payload: dict) -> None:
        platform = payload.get("platform")
        if not platform:
            self._json(400, {"error": "缺少 platform"})
            return
        if payload.get("_reset"):
            path = reset_profile(platform)
        else:
            fields = {k: payload[k] for k in
                      ("workers", "seg_size", "max_retries", "timeout",
                       "force_single", "respect_ranges", "note")
                      if k in payload}
            path = save_profile(platform, **fields)
        self._json(200, {"ok": True, "path": str(path),
                         "profile": asdict(get_profile(platform))})

    def _handle_settings(self, payload: dict) -> None:
        """更新设置并**持久化到磁盘**。

        关键：save_dir 必须落盘。否则用户改了下载目录，重启 exe 就还原
        （这是实测确认过的 bug）。
        """
        patch = {}
        with SETTINGS_LOCK:
            for k in ("save_dir", "max_concurrent", "verify",
                      "open_folder_when_done", "auto_cleanup_after_download"):
                if k in payload:
                    SETTINGS[k] = payload[k]
                    patch[k] = payload[k]
            if "max_concurrent" in payload:
                try:
                    QUEUE.set_max_concurrent(int(payload["max_concurrent"]))
                except (TypeError, ValueError):
                    pass
            snap = dict(SETTINGS)

        saved_path = ""
        save_error = ""
        if patch:
            try:
                saved_path = str(save_settings(patch))
            except OSError as e:
                # 磁盘只读/权限不足时不能让整个请求失败，但要如实告知
                save_error = f"设置已生效但未能保存到磁盘：{e}"

        resp = {"ok": True, "settings": snap, "saved_to": saved_path}
        if save_error:
            resp["warning"] = save_error
        self._json(200, resp)

    def _handle_refresh(self, payload: dict) -> None:
        """扩展取到新直链后回填。payload: {task_id, url, cookies, headers, size, expires_at}"""
        tid = payload.get("task_id")
        url = payload.get("url")
        if not tid or not url:
            self._json(400, {"error": "缺少 task_id 或 url"})
            return

        task = QUEUE.get(tid)
        if not task:
            REFRESH.cancel(tid)
            self._json(404, {"error": f"任务 {tid} 不存在"})
            return

        # 更新任务的下载凭证
        item = dict(task._payload or {})
        item["url"] = url
        if payload.get("cookies"):
            item["cookies"] = payload["cookies"]
        if payload.get("headers"):
            item["headers"] = payload["headers"]
        if payload.get("expires_at"):
            task.expires_at = float(payload["expires_at"])
        task._payload = item

        REFRESH.fulfill(tid, item)

        # 重新入队（保留 .parts，会从断点续传）
        QUEUE.requeue(tid, front=True)

        self._json(200, {"ok": True, "task_id": tid,
                         "attempt": task.refresh_attempts,
                         "note": "已用新直链重新入队，将从断点续传"})

    def _handle_task_action(self, payload: dict) -> None:
        tid = payload.get("id")
        action = payload.get("action")
        if not tid or not action:
            self._json(400, {"error": "缺少 id 或 action"})
            return
        fn = {"pause": QUEUE.pause, "resume": QUEUE.resume,
              "cancel": QUEUE.cancel, "remove": QUEUE.remove}.get(action)
        if not fn:
            self._json(400, {"error": f"未知 action: {action}"})
            return
        self._json(200, {"ok": bool(fn(tid))})


def serve() -> None:
    last_err = None
    for port in PORT_RANGE:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            last_err = e
            continue
        print(f"DLink 下载器已启动：http://127.0.0.1:{port}")
        print(f"  保存目录：{SETTINGS['save_dir']}")
        print(f"  并发任务：{QUEUE.max_concurrent}")
        print(f"  健康检查：http://127.0.0.1:{port}/api/health")
        print("等待扩展发送任务…（Ctrl+C 退出）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")
            httpd.shutdown()
        return
    raise SystemExit(f"端口 {PORT_RANGE} 全部被占用：{last_err}")


if __name__ == "__main__":
    serve()
