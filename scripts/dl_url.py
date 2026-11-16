#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 URL 直接分段并行下载（微云 ftn_handler 实测可用）。

用法示例：
    python scripts/dl_url.py --cookie FTN5K=c443b62d ^
        --task "417|LustFriend|https://.../ftn_handler/xxx/PC.7z?fname=PC.7z&from=30151&version=3.3.3.3" ^
        --task "619|不可思议之国的淫魔|https://..." ^
        --task "1328|别被诱惑所迷|https://..."

每个 --task 形如  `<大小MB>|<子目录名>|<url>`，输出到 <out-root>/<子目录名>/<文件名>。
分段 32MB × 8 路，支持断点续传（分段落盘在 <dest>.parts/）。
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def human(n: float) -> str:
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or u == "GB":
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} GB"


class SegDownloader:
    def __init__(self, url: str, dest: Path, size: int, cookie: str,
                 workers: int = 8, seg_mb: int = 32) -> None:
        self.url = url
        self.dest = dest
        self.size = size
        self.cookie = cookie
        self.workers = workers
        self.seg = seg_mb * 1048576
        self.parts = dest.parent / (dest.name + ".parts")
        self.done = 0
        self.lock = threading.Lock()
        self.failed: list[str] = []

    def plan(self) -> list[tuple[int, int]]:
        out, s = [], 0
        while s < self.size:
            e = min(s + self.seg, self.size) - 1
            out.append((s, e))
            s = e + 1
        return out

    def _one(self, idx: int, start: int, end: int) -> None:
        part = self.parts / f"{idx:04d}"
        want = end - start + 1
        have = part.stat().st_size if part.exists() else 0
        if have >= want:
            with self.lock:
                self.done += want
            return
        for attempt in range(5):
            try:
                headers = {
                    "User-Agent": UA,
                    "Referer": "https://www.weiyun.com/",
                    "Cookie": self.cookie,
                    "Range": f"bytes={start + have}-{end}",
                }
                r = requests.get(self.url, headers=headers, timeout=60, stream=True)
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                mode = "ab" if (r.status_code == 206 and have) else "wb"
                if mode == "wb":
                    have = 0
                with open(part, mode) as f:
                    for chunk in r.iter_content(262144):
                        if chunk:
                            f.write(chunk)
                            with self.lock:
                                self.done += len(chunk)
                if part.stat().st_size >= want:
                    return
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    self.failed.append(f"段{idx}: {e}")
                    return
                time.sleep(1.0 + attempt)
                have = part.stat().st_size if part.exists() else 0

    def run(self) -> bool:
        self.parts.mkdir(parents=True, exist_ok=True)
        plan = self.plan()
        # 复用已有分段
        for i, (s, e) in enumerate(plan):
            p = self.parts / f"{i:04d}"
            if p.exists():
                with self.lock:
                    self.done += min(p.stat().st_size, e - s + 1)
        print(f"  分段 {len(plan)} × {self.seg // 1048576}MB，{self.workers} 路，"
              f"已有 {human(self.done)}")
        q = list(range(len(plan)))
        qlock = threading.Lock()
        stop = threading.Event()

        def report() -> None:
            last, t0 = self.done, time.time()
            while not stop.wait(3.0):
                with self.lock:
                    d = self.done
                dt = max(1e-6, time.time() - t0)
                spd = (d - last) / dt
                last, t0 = d, time.time()
                print(f"\r    {d / self.size * 100:5.1f}%  {human(d)}/{human(self.size)}"
                      f"  {human(spd)}/s", end="", flush=True)

        th = threading.Thread(target=report, daemon=True)
        th.start()

        def worker() -> None:
            while True:
                with qlock:
                    if not q:
                        return
                    i = q.pop(0)
                s, e = plan[i]
                self._one(i, s, e)

        ts = [threading.Thread(target=worker) for _ in range(self.workers)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        stop.set()
        print()

        if self.failed:
            print("  有分段失败：")
            for f in self.failed[:5]:
                print("   ", f)
            return False

        self.dest.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dest, "wb") as out:
            for i in range(len(plan)):
                out.write((self.parts / f"{i:04d}").read_bytes())
        size = self.dest.stat().st_size
        ok = size == self.size
        print(f"  合并完成：{self.dest.name}  {human(size)}"
              f"{'' if ok else f' ⚠️ 期望 {human(self.size)}'}")
        return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", action="append", required=True,
                    help="<大小MB>|<子目录名>|<cookie>|<url>；cookie 写成 FTN5K=xxxx")
    ap.add_argument("--cookie", default="", help="全局默认 cookie（任务里没给时用）")
    ap.add_argument("--out-root", default=".")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seg-mb", type=int, default=32)
    a = ap.parse_args()

    root = Path(a.out_root).resolve()
    tasks = []
    for spec in a.task:
        parts = spec.split("|", 3)
        if len(parts) == 3:
            size_mb, folder, url = parts
            cookie = a.cookie
        else:
            size_mb, folder, cookie, url = parts
        tasks.append((int(float(size_mb) * 1048576), folder, cookie.strip(), url.strip()))

    for size, folder, cookie, url in tasks:
        # 用 HEAD/Range 校准真实大小
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Range": "bytes=0-0",
                                           "Cookie": cookie,
                                           "Referer": "https://www.weiyun.com/"},
                             timeout=30)
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                real = int(cr.split("/")[-1])
                if real and real != size:
                    print(f"  [校准] {folder}: {size} → {real}")
                    size = real
            if r.status_code not in (200, 206):
                print(f"❌ {folder}: 直链不可用 HTTP {r.status_code}")
                continue
        except Exception as e:  # noqa: BLE001
            print(f"❌ {folder}: 探测失败 {e}")
            continue

        dest = root / folder / "PC.7z"
        print(f"\n=== {folder} → {dest}  ({human(size)}) ===")
        dl = SegDownloader(url, dest, size, cookie,
                           workers=a.workers, seg_mb=a.seg_mb)
        dl.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
