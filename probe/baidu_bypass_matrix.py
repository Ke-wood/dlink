"""百度限速突破探测矩阵。

设计原则：一次只改一个变量，每次都带「基线对照」，
避免「这次比上次快」被误读成突破。

测速方法：固定读取 N 秒，量出字节数。不用整文件（29MB 在 76KB/s 下要 6 分钟）。

变异维度：
  A. 端点主机名   d.pcs / c.pcs / d1.pcs / nj02ct01 等
  B. clienttype   0 / 1 / 3 / 8
  C. UA           浏览器 / 网盘客户端 / pan.baidu.com
  D. HTTP 协议    HTTP/1.1 vs HTTP/2
  E. 老 PCS 接口  /rest/2.0/pcs/file
  F. Range 大小   小段 vs 大段（看是否有段级上限）
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests

UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_PAN = "pan.baidu.com"
UA_CLIENT = "netdisk"

DEFAULT_SECONDS = 8.0


@dataclass
class Result:
    label: str
    ok: bool
    speed: float          # MB/s
    bytes: int
    seconds: float
    status: int = 0
    note: str = ""
    extra: dict = field(default_factory=dict)


def load_ctx():
    ck = json.load(open('.private/baidu_cookies.json', encoding='utf-8'))
    dlink = open('.private/dlink.txt', encoding='utf-8').read().strip()
    return ck, dlink


def cookie_str(ck: dict) -> str:
    return '; '.join(f'{k}={v}' for k, v in ck.items())


def measure(url: str, ck: dict, *, ua: str = UA_BROWSER, seconds: float = DEFAULT_SECONDS,
            rng: str = 'bytes=0-67108863', extra_headers: dict | None = None,
            label: str = '', http2: bool = False) -> Result:
    """固定时长读取，返回平均速度。"""
    s = requests.Session()
    h = {'User-Agent': ua, 'Cookie': cookie_str(ck), 'Referer': 'https://pan.baidu.com/'}
    if extra_headers:
        h.update(extra_headers)
    n = 0
    t0 = time.perf_counter()
    status = 0
    note = ''
    try:
        r = s.get(url, headers={**h, 'Range': rng}, stream=True, timeout=(10, 25))
        status = r.status_code
        if status not in (200, 206):
            note = f'HTTP {status}: ' + r.text[:120].replace('\n', ' ')
            return Result(label, False, 0.0, 0, time.perf_counter() - t0, status, note)
        for c in r.iter_content(65536):
            n += len(c)
            if time.perf_counter() - t0 > seconds:
                break
    except Exception as e:
        note = f'{type(e).__name__}: {str(e)[:100]}'
        return Result(label, False, 0.0, n, time.perf_counter() - t0, status, note)
    finally:
        s.close()
    dt = time.perf_counter() - t0
    return Result(label, True, (n / dt / 1e6) if dt else 0.0, n, dt, status)


def swap_host(dlink: str, host: str) -> str:
    u = urlparse(dlink)
    return urlunparse((u.scheme, host, u.path, u.params, u.query, u.fragment))


def swap_query(dlink: str, **kwargs) -> str:
    u = urlparse(dlink)
    q = parse_qs(u.query, keep_blank_values=True)
    for k, v in kwargs.items():
        q[k] = [str(v)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))


def fmt(r: Result) -> str:
    tag = 'OK ' if r.ok else 'ERR'
    sp = f'{r.speed*1000:7.1f} KB/s' if r.speed < 1 else f'{r.speed:6.2f} MB/s'
    note = f'  {r.note}' if r.note else ''
    return f'  {tag} {r.label:42} {sp}  ({r.bytes/1e6:.2f}MB/{r.seconds:.1f}s){note}'


def main() -> None:
    ck, dlink = load_ctx()
    size = 29620737
    print('=' * 88)
    print('百度限速突破探测矩阵')
    print('=' * 88)

    results: list[Result] = []

    # ---------- 0. 基线 ----------
    print('\n[0] 基线（当前已知最快的做法）')
    for i in range(2):
        r = measure(dlink, ck, label=f'baseline#{i+1} d.pcs + 浏览器UA + clienttype0')
        results.append(r); print(fmt(r))
    base = statistics.median([r.speed for r in results if r.ok]) if any(r.ok for r in results) else 0
    print(f'  → 基线中位数 {base*1000:.1f} KB/s')

    # ---------- A. 换端点主机 ----------
    print('\n[A] 换 CDN 端点（同一 dlink，只改 host）')
    for host in ['d.pcs.baidu.com', 'c.pcs.baidu.com', 'd1.pcs.baidu.com',
                 'nj02ct01.pcs.baidu.com', 'bj01ct01.pcs.baidu.com']:
        u = swap_host(dlink, host)
        r = measure(u, ck, label=f'host={host}', seconds=5)
        results.append(r); print(fmt(r))

    # ---------- B. clienttype ----------
    print('\n[B] 改 clienttype（影响服务端限速策略判定）')
    for ct in [0, 1, 3, 8]:
        u = swap_query(dlink, clienttype=ct)
        r = measure(u, ck, label=f'clienttype={ct}', seconds=5)
        results.append(r); print(fmt(r))

    # ---------- C. UA ----------
    print('\n[C] 改 User-Agent')
    for ua, name in [(UA_BROWSER, 'browser'), (UA_PAN, 'pan.baidu.com'), (UA_CLIENT, 'netdisk')]:
        r = measure(dlink, ck, ua=ua, label=f'UA={name}', seconds=5)
        results.append(r); print(fmt(r))

    # ---------- D. Range 大小 ----------
    print('\n[D] Range 段大小（看是否有段级速率上限）')
    for mb in [1, 4, 16, 64]:
        r = measure(dlink, ck, rng=f'bytes=0-{mb*1024*1024-1}', label=f'Range={mb}MB', seconds=5)
        results.append(r); print(fmt(r))

    # ---------- E. 老 PCS 接口 ----------
    print('\n[E] 老 PCS 接口 /rest/2.0/pcs/file?method=download')
    for ua, name in [(UA_BROWSER, 'browser'), (UA_PAN, 'pan.baidu.com')]:
        u = (f'https://pcs.baidu.com/rest/2.0/pcs/file?method=download'
             f'&app_id=250528&path=%2FPC.7z')
        r = measure(u, ck, ua=ua, label=f'pcs/file UA={name}', seconds=6)
        results.append(r); print(fmt(r))

    # ---------- F. 汇总 ----------
    print('\n' + '=' * 88)
    ok = [r for r in results if r.ok]
    ok.sort(key=lambda r: -r.speed)
    print('速度排名（前 8）：')
    for r in ok[:8]:
        print(fmt(r))
    print('=' * 88)
    if base > 0 and ok:
        best = ok[0]
        ratio = best.speed / base
        print(f'基线 {base*1000:.1f} KB/s ｜ 最佳 {best.label} = {best.speed*1000:.1f} KB/s')
        print(f'提升倍数: {ratio:.2f}x  → ' + ('**可能有突破**' if ratio > 2 else '无明显突破'))
    print('=' * 88)


if __name__ == '__main__':
    main()
