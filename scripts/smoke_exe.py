#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对打包出来的 DLink.exe 做冒烟测试（不联网、不污染用户设置）。

用法：
    python scripts/smoke_exe.py                 # 默认测 dist/DLink.exe
    python scripts/smoke_exe.py --exe other.exe

检查项：
  1. exe 能起来，/api/health 报的版本 == 包版本（防止硬编码版本回归）
  2. POST /api/task 能收下任务，并且真的把文件下下来（字节数一致）
  3. 同名文件不会互相覆盖（服务端 unique_dest）
  4. platform=baidu 的任务按 1 路跑（账号级限速策略没被破坏）
  5. 无控制台进程里 POST 不被 sys.stderr=None 打死（历史 bug：exe 里所有 POST 全挂）

退出码 0 = 通过。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dlink.task_queue import CANCELED, DONE, FAILED  # noqa: E402

TERMINAL = {DONE, FAILED, CANCELED}


def free_port(start: int = 8899) -> int:
    for p in range(start, start + 20):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    raise SystemExit("找不到空闲端口")


def start_file_server(directory: Path, port: int) -> ThreadingHTTPServer:
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(directory), **kw)

        def log_message(self, *a):  # 静音
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def api(port: int, path: str, payload=None, timeout: float = 10):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def wait_backend(port: int, timeout: float = 40) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return api(port, "/api/health", timeout=2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise SystemExit(f"后端没起来：{last}")


def wait_task(port: int, task_id: str, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        for t in api(port, "/api/tasks")["tasks"]:
            if t["id"] == task_id:
                last = t
                if t["status"] in TERMINAL:
                    return t
        time.sleep(0.3)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(ROOT / "dist" / "DLink.exe"))
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    exe = Path(a.exe).resolve()
    if not exe.exists():
        raise SystemExit(f"找不到 {exe}")

    from dlink import __version__ as pkg_version

    tmp = Path(tempfile.mkdtemp(prefix="dlink-smoke-"))
    save_dir = tmp / "out"
    save_dir.mkdir()
    settings_file = tmp / "settings.json"
    settings_file.write_text(json.dumps({
        "save_dir": str(save_dir),
        "max_concurrent": 2,
        "verify": True,
    }), encoding="utf-8")

    # 造一个 4MB 的本地文件给下载器拉
    payload = os.urandom(4 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    served = tmp / "served"
    served.mkdir()
    (served / "sample.bin").write_bytes(payload)

    port = free_port()
    httpd = start_file_server(served, port)

    # 用临时的 settings/hosts 文件，别污染用户机器上的真实配置
    env = dict(os.environ,
               DLINK_SETTINGS_FILE=str(settings_file),
               DLINK_HOSTS_FILE=str(tmp / "hosts.json"))
    proc = subprocess.Popen([str(exe)], cwd=str(ROOT), env=env)
    failures: list[str] = []
    try:
        health = wait_backend(a.port)
        print(f"  health: version={health.get('version')} app={health.get('app')}")
        if health.get("version") != pkg_version:
            failures.append(f"/api/health 版本 {health.get('version')} != 包版本 {pkg_version}")

        # 1) 普通链接：真的把文件下下来
        r = api(a.port, "/api/task", {
            "version": 1, "platform": "direct", "title": "sample.bin",
            "items": [{
                "url": f"http://127.0.0.1:{port}/sample.bin",
                "filename": "sample.bin",
                "size": len(payload),
            }],
        })
        tid = r["task_ids"][0]
        t = wait_task(a.port, tid)
        print(f"  直链任务: status={t.get('status')} done={t.get('done')} workers={t.get('workers')}")
        if t.get("status") != DONE:
            failures.append(f"直链任务未完成：{t.get('status')} {t.get('error')}")
        else:
            got = Path(t["path"]).read_bytes()
            if hashlib.sha256(got).hexdigest() != digest:
                failures.append("下载内容与源文件不一致")

        # 2) 百度任务必须 1 路（CDN 平台推断错的话这里会变成 8）
        r = api(a.port, "/api/task", {
            "version": 1, "platform": "baidu", "title": "b.bin",
            "items": [{"url": f"http://127.0.0.1:{port}/sample.bin",
                       "filename": "b.bin", "size": len(payload)}],
        })
        tid2 = r["task_ids"][0]
        time.sleep(0.6)
        bt = next(x for x in api(a.port, "/api/tasks")["tasks"] if x["id"] == tid2)
        print(f"  百度任务: workers={bt.get('workers')} src={bt.get('workers_src')}")
        if bt.get("workers") != 1:
            failures.append(f"百度任务应为 1 路，实际 {bt.get('workers')}")

        # 3) 同名文件不覆盖（等于刚才那个 b.bin / sample.bin 再来一次）
        r = api(a.port, "/api/task", {
            "version": 1, "platform": "direct", "title": "sample.bin",
            "items": [{"url": f"http://127.0.0.1:{port}/sample.bin",
                       "filename": "sample.bin", "size": len(payload)}],
        })
        t2 = wait_task(a.port, r["task_ids"][0])
        if t2.get("status") == DONE:
            p = Path(t2["path"])
            print(f"  同名文件: {p.name}")
            if p.name == "sample.bin":
                failures.append("同名文件没有加 (1) 后缀，可能覆盖了原文件")
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        # onefile 的子进程不在 arg 树里，按镜像名兜底
        subprocess.run(["taskkill", "/F", "/IM", exe.name], capture_output=True)
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("\n失败：")
        for f in failures:
            print("  ✗", f)
        return 1
    print("\nexe 冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
