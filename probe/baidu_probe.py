"""百度网盘直链测速：用导出的 cookie 直接打 dlink，测单连接/冷启动/并发。

cookie 从 .private/baidu_cookies.json 读（不打印值）。
"""
import json, sys, time, threading, statistics
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

def load_cookies(path):
    return json.load(open(path, encoding='utf-8'))

def cookie_header(ck):
    return '; '.join(f'{k}={v}' for k, v in ck.items())

def stream(url, ck, start, length, maxsec=25, label=''):
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Cookie': cookie_header(ck)})
    end = start + length - 1
    total = 0
    wins = []
    t0 = time.perf_counter(); last_t = t0; last_b = 0
    try:
        r = s.get(url, headers={'Range': f'bytes={start}-{end}'}, stream=True, timeout=(10, 30))
        if label == 'main':
            print(f'  status={r.status_code} content-range={r.headers.get("content-range")} len={r.headers.get("content-length")}')
        if r.status_code not in (200, 206):
            print(f'  [{label}] bad status {r.status_code}: {r.text[:200]}')
            return total, time.perf_counter() - t0, wins
        for chunk in r.iter_content(65536):
            total += len(chunk)
            now = time.perf_counter()
            if now - last_t >= 1.0:
                inst = (total - last_b) / (now - last_t) / 1e6
                wins.append((round(now - t0, 1), total, round(inst, 2)))
                if label == 'main':
                    print(f'    t={now-t0:5.1f}s cum={total/1e6:6.2f}MB inst={inst:6.2f}MB/s')
                last_t, last_b = now, total
            if now - t0 > maxsec:
                break
    except Exception as e:
        print(f'  [{label}] 中断 {type(e).__name__}: {str(e)[:120]}')
    finally:
        try: r.close()
        except Exception: pass
        s.close()
    return total, time.perf_counter() - t0, wins

def main():
    dlink = sys.argv[1]; size = int(sys.argv[2])
    ck = load_cookies('.private/baidu_cookies.json')
    print('=' * 72)
    print(f'百度直链测速  size={size/1e6:.1f}MB  cookies={len(ck)} 项')
    print('=' * 72)

    print('\n[1] 单连接持续 25s（看是否先快后死）')
    tot, el, wins = stream(dlink, ck, 0, 4*1024**3, 25, 'main')
    avg = tot/el/1e6 if el else 0
    print(f'  → 平均 {avg:.2f} MB/s，共 {tot/1e6:.2f}MB / {el:.1f}s')
    if wins:
        f = statistics.mean([w[2] for w in wins[:3]]); l = statistics.mean([w[2] for w in wins[-3:]])
        print(f'  开头 {f:.2f} | 结尾 {l:.2f} MB/s')

    print('\n[2] 新连接冷启动 ×4（各读 4MB）')
    fresh = []
    for i in range(4):
        off = int(size*(i+1)/6)
        t, e, _ = stream(dlink, ck, off, 4*1024**2, 15, f'fresh{i+1}')
        sp = t/e/1e6 if e else 0; fresh.append(sp)
        print(f'    #{i+1} offset={off} → {sp:.2f} MB/s ({t/1e6:.2f}MB/{e:.1f}s)')
    med = statistics.median(fresh) if fresh else 0
    print(f'  → 冷启动中位数 {med:.2f} MB/s')

    print('\n[3] 8 路并发 × 3MB')
    res = [None]*8
    def worker(i):
        off = int(size*(i+1)/10)
        res[i] = stream(dlink, ck, off, 3*1024**2, 20, f'p{i}')
    t0 = time.perf_counter()
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ths: t.start()
    for t in ths: t.join()
    wall = time.perf_counter() - t0
    total = sum(r[0] for r in res if r)
    agg = total/wall/1e6
    print(f'  → 聚合 {total/1e6:.1f}MB / {wall:.1f}s = {agg:.2f} MB/s')
    print(f'    单路: {[round(r[0]/r[1]/1e6,2) if r and r[1] else 0 for r in res]}')

    print('\n' + '='*72)
    print(f'结论: 单连接 {avg:.2f} | 冷启动 {med:.2f} | 8路聚合 {agg:.2f} MB/s')
    print(f'加速比: {agg/avg if avg else 0:.1f}x')
    print('='*72)

if __name__ == '__main__':
    main()
