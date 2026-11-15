"""限速探针：对任意直链做「单连接衰减 / 新连接冷启动 / 多路并发」三组实测。

为什么单独写：交接文档说微云「前14MB约10MB/s，之后掉到240B/s」，
这条要能复现、要能对任意平台复用，不能靠一次偶然观察。

用法：
  python probe/speed_probe.py --url "<直链>" [--cookie "FTN5K=xxx"] [--size 1638865525]

只读探测：默认只下载前若干 MB，不落盘大文件。
"""

from __future__ import annotations

import argparse
import os
import statistics
import threading
import time
from dataclasses import dataclass, field

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass
class StreamSample:
    total: int = 0
    elapsed: float = 0.0
    windows: list = field(default_factory=list)  # [(seconds, bytes_so_far, inst_speed)]

    @property
    def avg_mbps(self) -> float:
        return (self.total / self.elapsed / 1e6) if self.elapsed else 0.0


def _session(cookie: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if cookie:
        s.headers.update({"Cookie": cookie})
    return s


def stream_once(url: str, cookie: str, start: int, length: int,
                max_seconds: float = 30.0, label: str = "") -> StreamSample:
    """一次连续连接：从 start 读 length 字节，边读边记瞬时速度。"""
    s = _session(cookie)
    end = start + length - 1
    sample = StreamSample()
    t0 = time.perf_counter()
    last_t, last_b = t0, 0
    try:
        r = s.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=(10, 30))
        code = r.status_code
        cr = r.headers.get("content-range")
        print(f"  [{label}] status={code} content-range={cr} len={r.headers.get('content-length')}")
        if code not in (200, 206):
            print(f"  [{label}] 非预期状态码，停止")
            return sample
        for chunk in r.iter_content(65536):
            sample.total += len(chunk)
            now = time.perf_counter()
            if now - last_t >= 1.0:
                inst = (sample.total - last_b) / (now - last_t) / 1e6
                sample.windows.append((round(now - t0, 1), sample.total, round(inst, 3)))
                last_t, last_b = now, sample.total
                if label == "sustained":
                    print(f"    t={now - t0:5.1f}s  cum={sample.total/1e6:7.2f}MB  inst={inst:6.2f}MB/s")
            if now - t0 > max_seconds or sample.total >= length:
                break
    except Exception as e:  # noqa: BLE001
        print(f"  [{label}] 中断: {type(e).__name__}: {e}")
    finally:
        sample.elapsed = time.perf_counter() - t0
        try:
            r.close()
        except Exception:
            pass
        s.close()
    return sample


def probe_single_sustained(url: str, cookie: str, seconds: float = 25.0) -> StreamSample:
    print("\n[1] 单连接持续下载（观察是否「先快后死」）")
    return stream_once(url, cookie, 0, 4 * 1024**3, max_seconds=seconds, label="sustained")


def probe_fresh_start(url: str, cookie: str, size: int, n: int = 5, chunk: int = 4 * 1024**2) -> list:
    print(f"\n[2] 新连接冷启动 ×{n}（每次从不同偏移开新连接，读 {chunk/1e6:.1f}MB）")
    out = []
    for i in range(n):
        offset = int(size * (i + 1) / (n + 2))
        s = stream_once(url, cookie, offset, chunk, max_seconds=15, label=f"fresh#{i+1}@{offset}")
        out.append(s.avg_mbps)
        print(f"    → {s.avg_mbps:.2f} MB/s（{s.total/1e6:.2f}MB / {s.elapsed:.2f}s）")
    return out


def probe_parallel(url: str, cookie: str, size: int, ways: int = 8,
                   chunk: int = 16 * 1024**2) -> float:
    print(f"\n[3] 并发 {ways} 路 × {chunk/1e6:.0f}MB（测聚合吞吐）")
    results = [None] * ways
    lock = threading.Lock()

    def worker(idx: int):
        offset = int(size * (idx + 1) / (ways + 2))
        s = stream_once(url, cookie, offset, chunk, max_seconds=20, label=f"p{idx}")
        with lock:
            results[idx] = s

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(ways)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    total = sum(r.total for r in results if r)
    agg = total / wall / 1e6
    print(f"    聚合: {total/1e6:.1f}MB / {wall:.1f}s = {agg:.2f} MB/s")
    each = [round(r.avg_mbps, 2) for r in results if r]
    print(f"    单路均速: {each}  中位 {statistics.median(each) if each else 0:.2f} MB/s")
    return agg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--cookie", default="")
    ap.add_argument("--size", type=int, default=0)
    ap.add_argument("--ways", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=25.0)
    a = ap.parse_args()

    print("=" * 70)
    print(f"URL: {a.url[:100]}")
    print(f"Cookie: {'有' if a.cookie else '无'}   Size: {a.size or '未知'}")
    print("=" * 70)

    s1 = probe_single_sustained(a.url, a.cookie, a.seconds)
    print(f"\n  单连接平均: {s1.avg_mbps:.2f} MB/s, 共 {s1.total/1e6:.2f}MB, 用时 {s1.elapsed:.1f}s")
    if s1.windows:
        firsts = [w[2] for w in s1.windows[:3]]
        lasts = [w[2] for w in s1.windows[-3:]]
        print(f"  开头瞬时: {firsts}  结尾瞬时: {lasts}")
        decay = (statistics.mean(firsts) / statistics.mean(lasts)) if lasts and statistics.mean(lasts) > 0.001 else 0
        print(f"  衰减倍数: {decay:.1f}x  → {'『先快后死』成立' if decay > 3 else '未见明显衰减'}")

    if a.size:
        fresh = probe_fresh_start(a.url, a.cookie, a.size)
        print(f"  冷启动中位数: {statistics.median(fresh) if fresh else 0:.2f} MB/s")
        agg = probe_parallel(a.url, a.cookie, a.size, ways=a.ways)
        print("\n" + "=" * 70)
        print(f"结论: 单连接 {s1.avg_mbps:.2f} | 冷启动 {statistics.median(fresh) if fresh else 0:.2f} | "
              f"{a.ways}路并发聚合 {agg:.2f} MB/s")
        est = a.size / (agg * 1e6) if agg > 0 else 0
        print(f"预估完整下载（{a.size/1e9:.2f}GB）: {est/60:.1f} 分钟")
    print("=" * 70)


if __name__ == "__main__":
    main()
