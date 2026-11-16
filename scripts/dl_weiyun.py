#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""微云分享直下：8 路分段 + 断点续传，一个分享一个文件夹。

为什么单独写这个脚本（而不是走 GUI/DLink）：
    GUI 那条链路要经过「扩展解析 → 任务队列 → 下载器」，
    中间任何一环出问题（g_tk、FTN5K、同名撞分段）都会失败。
    这个脚本把「解析 → 取链 → 8 路下载」放在一个进程里，
    出错信息直接可见，适合「现在就要把文件拿下来」的场景。

用法：
    python scripts/dl_weiyun.py --cookies cookies_weiyun.json \
        https://share.weiyun.com/rAwksPh3 \
        https://share.weiyun.com/EWFj13Lr

    # 输出目录默认是当前工作目录，每个分享一个文件夹（用分享名）
    # --out-root 可改；--workers 8；--seg-mb 32

cookie 文件格式：{"name": "value", ...}，至少要有 skey。
    g_tk 由 skey 计算；没有 skey 时微云接口一律 403 空 body。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dlink.platforms.weiyun import WeiyunPlatform, WeiyunSession  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


class Downloader:
    """把一条直链按 32MB 分段、8 路并行下到文件；支持断点续传。"""

    def __init__(self, url: str, dest: Path, size: int, cookies: dict,
                 workers: int = 8, seg_mb: int = 32) -> None:
        self.url = url
        self.dest = dest
        self.size = size
        self.cookies = cookies
        self.workers = workers
        self.seg = seg_mb * 1024 * 1024
        self.parts = dest.parent / (dest.name + ".parts")
        self.done_bytes = 0
        self.lock = threading.Lock()

    def _plan(self) -> list[tuple[int, int]]:
        out = []
        start = 0
        while start < self.size:
            end = min(start + self.seg, self.size) - 1
            out.append((start, end))
            start = end + 1
        return out

    def _fetch_range(self, idx: int, start: int, end: int) -> None:
        part = self.parts / f"{idx:04d}"
        have = part.stat().st_size if part.exists() else 0
        want = end - start + 1
        if have >= want:
            with self.lock:
                self.done_bytes += want
            return
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.weiyun.com/",
            "Range": f"bytes={start + have}-{end}",
        }
        for attempt in range(4):
            try:
                r = requests.get(self.url, headers=headers, cookies=self.cookies,
                                 timeout=60, stream=True)
                if r.status_code not in (200, 206):
                    body = r.content[:120].decode("utf-8", "replace")
                    raise RuntimeError(f"HTTP {r.status_code} {body}")
                mode = "ab" if (r.status_code == 206 and have) else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as f:
                    for chunk in r.iter_content(262144):
                        if not chunk:
                            continue
                        f.write(chunk)
                        with self.lock:
                            self.done_bytes += len(chunk)
                if part.stat().st_size >= want:
                    return
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                print(f"    段{idx} 重试 {attempt+1}/3：{e}")
                time.sleep(1)
                headers["Range"] = f"bytes={start + (part.stat().st_size if part.exists() else 0)}-{end}"

    def run(self) -> None:
        self.parts.mkdir(parents=True, exist_ok=True)
        plan = self._plan()
        print(f"    分段：{len(plan)} × {self.seg // 1048576}MB，{self.workers} 路并行")
        stop = threading.Event()

        def reporter() -> None:
            last, t0 = 0, time.time()
            while not stop.wait(2.0):
                with self.lock:
                    d = self.done_bytes
                dt = time.time() - t0
                spd = (d - last) / dt if dt else 0
                last, t0 = d, time.time()
                pct = d / self.size * 100 if self.size else 0
                print(f"\r    进度 {pct:5.1f}%  {human(d)} / {human(self.size)}  {human(spd)}/s",
                      end="", flush=True)

        t = threading.Thread(target=reporter, daemon=True)
        t.start()
        try:
            idx = 0
            idx_lock = threading.Lock()

            def worker() -> None:
                nonlocal idx
                while True:
                    with idx_lock:
                        if idx >= len(plan):
                            return
                        i = idx
                        idx += 1
                    s, e = plan[i]
                    self._fetch_range(i, s, e)

            threads = [threading.Thread(target=worker) for _ in range(max(1, self.workers))]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        finally:
            stop.set()
            print()

        with open(self.dest, "wb") as out:
            for i in range(len(plan)):
                out.write((self.parts / f"{i:04d}").read_bytes())
        print(f"    合并完成：{self.dest}  {human(self.dest.stat().st_size)}")


def load_cookies(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "cookies" in data:
        data = {c["name"]: c["value"] for c in data["cookies"]}
    return {str(k): str(v) for k, v in data.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shares", nargs="+", help="微云分享链接")
    ap.add_argument("--cookies", required=True, help="cookie JSON 文件")
    ap.add_argument("--out-root", default=".", help="输出根目录（默认当前目录）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seg-mb", type=int, default=32)
    a = ap.parse_args()

    cookies = load_cookies(a.cookies)
    print(f"cookie：{len(cookies)} 个，" + ("含 skey ✅" if cookies.get("skey") else "**没有 skey**"))
    session = WeiyunSession(cookies)
    print(f"g_tk = {session.g_tk}")
    if not session.g_tk:
        print("⚠️ g_tk=0：微云接口会 403。需要一个带 skey 的 cookie。")

    plat = WeiyunPlatform(session)
    out_root = Path(a.out_root).resolve()

    for share in a.shares:
        print(f"\n=== {share} ===")
        try:
            key = WeiyunPlatform.parse_share_key(share)
            info = plat.get_share_info(key)
            name = (info.title or key).strip() or key
            safe = "".join(c for c in name if c not in '<>:"/\\|?*').strip()[:80]
            files = plat.list_files(key)
            print(f"  分享名：{name}   文件 {len(files)} 个")
            for f in files:
                print(f"    - {f.name}  {human(f.size)}")
            for f in files:
                target_dir = out_root / safe
                target_dir.mkdir(parents=True, exist_ok=True)
                dest = target_dir / f.name
                if dest.exists() and dest.stat().st_size == f.size:
                    print(f"  已存在且大小一致，跳过：{dest}")
                    continue
                link = plat.get_direct_link(key, f)
                print(f"  直链 host: {link.url.split('/')[2] if '://' in link.url else '?'}")
                dl = Downloader(link.url, dest, f.size, link.cookies or {},
                                workers=a.workers, seg_mb=a.seg_mb)
                dl.run()
        except Exception as e:  # noqa: BLE001
            print(f"  失败：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
