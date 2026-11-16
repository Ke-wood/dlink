"""DLink 单文件启动入口（打包用）。

为什么要这个文件：
    打包成 exe 时，用户双击后应该**同时**拉起后端和 GUI，
    而不是让他开两个程序。

设计：
    · 后端跑在后台线程，GUI 在主线程（Qt 必须在主线程）
    · 单实例：已有实例在跑时，只唤起窗口(?)，这里简化为允许重复启动，
      交由端口探测处理（后端会自动顺延端口）
    · 关闭窗口 = 最小化到托盘，后端继续跑
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


def _bootstrap_path() -> None:
    """打包后 src 已被收进 exe，开发态需要把 src 加进 sys.path。"""
    if getattr(sys, "frozen", False):
        return
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if src.is_dir():
        sys.path.insert(0, str(src))


_bootstrap_path()


def _already_running() -> bool:
    """检测是否已有 DLink 在运行（探测 8765 端口）。"""
    for port in (8765, 8766, 8767, 8768, 8769):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.35):
                return True
        except OSError:
            continue
    return False


def _start_backend() -> None:
    """在后台线程启动本地 HTTP 服务。"""
    from dlink import server as server_mod

    def run():
        try:
            server_mod.serve()
        except SystemExit as e:
            print(f"[后端] 退出: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[后端] 异常: {e}")

    t = threading.Thread(target=run, name="dlink-backend", daemon=True)
    t.start()

    # 等它起来（最多 8 秒）
    for _ in range(80):
        time.sleep(0.1)
        for port in (8765, 8766, 8767, 8768, 8769):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    print(f"[后端] 已就绪 :{port}")
                    return
            except OSError:
                continue
    print("[后端] 启动超时（GUI 仍会尝试连接）")


def main() -> int:
    # 高 DPI（Qt6 默认已开启，这里只做兼容）
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    if _already_running():
        print("[启动] 检测到已有实例在运行，本进程只打开窗口")
    else:
        _start_backend()

    # 启动 GUI（主线程）
    from dlink.gui.__main__ import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
