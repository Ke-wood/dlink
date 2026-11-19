#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Cloudreve（v4）分享直链解析 + 8 路下载。

实测站点：base-cloud.acgmomo.com（Cloudreve v4 + Cloudflare R2 存储）

链路：
    GET  /api/v4/share/info/<shareId>
    GET  /api/v4/file?uri=cloudreve://<shareId>@share
    POST /api/v4/file/url  {"uris":["cloudreve://<shareId>@share/<file>"], "download":true}
         → data.urls[0].url = R2 预签名地址（带 X-Amz-Expires，会过期）

要点：
    · 公开分享不需要登录、不需要 cookie
    · 直链是对象存储的预签名 URL，**不能复用太久**（默认几小时），过期重跑本脚本即可
    · 下载走对象存储，不限速、支持 Range → 可以放心多线程

用法：
    python scripts/dl_cloudreve.py https://base-cloud.acgmomo.com/home?path=cloudreve%3A%2F%2FGB7CY%40share
    python scripts/dl_cloudreve.py https://base-cloud.acgmomo.com/s/GB7CY --out-root D:/dl --workers 8
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def parse_share(url: str) -> tuple[str, str]:
    """从各种 Cloudreve 分享链接里取出 (origin, shareId)。"""
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    origin = f"{p.scheme}://{p.netloc}"
    raw = unquote(u)
    m = re.search(r"cloudreve://([A-Za-z0-9]+)@", raw)
    if m:
        return origin, m.group(1)
    m = re.search(r"/s/([A-Za-z0-9]+)", p.path)
    if m:
        return origin, m.group(1)
    raise SystemExit(f"认不出 Cloudreve 分享链接：{url!r}")


class SegDownloader:
    def __init__(self, url: str, dest: Path, size: int,
                 workers: int = 8, seg_mb: int = 32) -> None:
        self.url, self.dest, self.size = url, dest, size
        self.workers, self.seg = workers, seg_mb * 1048576
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
                r = requests.get(self.url, timeout=60, stream=True, headers={
                    "User-Agent": UA,
                    "Range": f"bytes={start + have}-{end}",
                })
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
        for i, (s, e) in enumerate(plan):
            p = self.parts / f"{i:04d}"
            if p.exists():
                with self.lock:
                    self.done += min(p.stat().st_size, e - s + 1)
        q = list(range(len(plan)))
        ql = threading.Lock()
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
                with ql:
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
            for f in self.failed[:5]:
                print("   ", f)
            return False
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dest, "wb") as out:
            for i in range(len(plan)):
                out.write((self.parts / f"{i:04d}").read_bytes())
        print(f"  完成：{self.dest.name}  {human(self.dest.stat().st_size)}")
        return self.dest.stat().st_size == self.size


def resolve(origin: str, share_id: str) -> tuple[str, str, int, dict]:
    """返回 (下载直链, 文件名, 大小, 分享信息)。"""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Referer": origin + "/"})
    info = s.get(f"{origin}/api/v4/share/info/{share_id}", timeout=20).json()
    files = s.get(f"{origin}/api/v4/file",
                  params={"uri": f"cloudreve://{share_id}@share"}, timeout=20).json()
    items = (files.get("data") or {}).get("files") or []
    if not items:
        raise SystemExit("分享里没有文件")
    f = items[0]
    path, name, size = f["path"], f["name"], int(f.get("size") or 0)
    r = s.post(f"{origin}/api/v4/file/url",
               json={"uris": [path], "download": True}, timeout=30).json()
    urls = (r.get("data") or {}).get("urls") or []
    if not urls or not urls[0].get("url"):
        raise SystemExit(f"取直链失败：{json.dumps(r, ensure_ascii=False)[:200]}")
    return urls[0]["url"], name, size, (info.get("data") or {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("share", help="Cloudreve 分享链接")
    ap.add_argument("--out-root", default=".")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seg-mb", type=int, default=32)
    ap.add_argument("--link-only", action="store_true", help="只打印直链不下载")
    a = ap.parse_args()

    origin, share_id = parse_share(a.share)
    print(f"站点 {origin}  分享 {share_id}")
    url, name, size, info = resolve(origin, share_id)
    print(f"文件：{name}")
    print(f"大小：{human(size)}")
    print(f"直链：{url[:90]}…（{len(url)} 字符，预签名，会过期）")
    if a.link_only:
        print(url)
        return 0

    dest = Path(a.out_root).resolve() / name
    # 校准真实大小
    try:
        r = requests.head(url, timeout=30, allow_redirects=True,
                          headers={"User-Agent": UA})
        real = int(r.headers.get("Content-Length") or 0)
        if real and real != size:
            print(f"[校准] {size} → {real}")
            size = real
    except Exception:  # noqa: BLE001
        pass

    print(f"目标：{dest}")
    dl = SegDownloader(url, dest, size, workers=a.workers, seg_mb=a.seg_mb)
    return 0 if dl.run() else 1


if __name__ == "__main__":
    raise SystemExit(main())
