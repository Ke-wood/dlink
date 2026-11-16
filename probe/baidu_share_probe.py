#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探针：摸清一个百度分享「取直链」到底缺什么参数。

背景（2026-11-16）：扩展在 `PC.7z` 上报
    sharedownload errno=-20 验证码输入错误
但页面能正常列出文件。怀疑 `path_list`（子目录里的文件必须带父目录）
和 sekey 来源都有问题。这个脚本不下载、不转存，只发 `/api/sharedownload`
并打印每个参数组合的 errno，用来确定真正的失败原因。

用法：
    python probe/baidu_share_probe.py "https://pan.baidu.com/s/1xxx?pwd=abcd"
    python probe/baidu_share_probe.py <url> --cookie-file cookies.json

cookie 可选：把浏览器里 pan.baidu.com 的 cookie 导出成 {"BDUSS": "...", ...}
（匿名也能试，但部分分享需要登录）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://pan.baidu.com"
APP_ID = "250528"
CHANNEL = "chunlei"


def new_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
    for k, v in (cookies or {}).items():
        s.cookies.set(k, v, domain=".baidu.com")
    return s


def get_html(s: requests.Session, url: str) -> str:
    r = s.get(url, headers={"Accept": "text/html"}, timeout=30, allow_redirects=True)
    print(f"  页面 {r.status_code} len={len(r.text)} final={r.url[:90]}")
    return r.text


def extract(html: str) -> dict:
    out = {}
    m = re.search(r"yunData\s*=\s*(\{.*?\});\s*\n", html, re.S)
    if m:
        try:
            out["yunData"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            out["yunData"] = None
    for key in ("jsToken", "currentSekey"):
        mm = re.search(key + r"""["']?\s*[:=]\s*["']([^"']{8,})["']""", html)
        if mm:
            out[key] = mm.group(1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--cookie-file", default="")
    a = ap.parse_args()

    cookies = {}
    if a.cookie_file:
        cookies = json.loads(Path(a.cookie_file).read_text(encoding="utf-8"))
        print(f"cookie: {sorted(cookies)}")

    s = new_session(cookies)
    print("== 1. 打开分享页（带 pwd）==")
    html = get_html(s, a.url)
    got = extract(html)
    yd = got.get("yunData") or {}
    print(f"  yunData: {'有' if yd else '没有'}  jsToken: {bool(got.get('jsToken'))}"
          f"  currentSekey: {bool(got.get('currentSekey'))}")
    if not yd:
        print("  !! 页面里没 yunData，后面大概率没戏（需要提取码或已失效）")
        return 1

    shareid, uk = yd.get("shareid"), yd.get("share_uk") or yd.get("uk")
    surl = re.search(r"/s/([A-Za-z0-9_-]+)", a.url).group(1)
    print(f"  shareid={shareid} uk={uk} surl={surl}")
    print("  BDCLND cookie:", bool(s.cookies.get("BDCLND")))
    print(f"  sekey 候选: page={bool(got.get('currentSekey'))} "
          f"cookie={bool(s.cookies.get('BDCLND'))}")

    roots = yd.get("file_list") or []
    print(f"\n== 2. 根目录 {len(roots)} 项 ==")
    for it in roots:
        print(f"  [{ 'DIR' if it.get('isdir') else 'FILE'}] {it.get('server_filename')}"
              f" path={it.get('path')} fs_id={it.get('fs_id')}")

    # 递归一层：找到真正的文件
    files = []   # (name, fs_id, parent_dir)
    for it in roots:
        if not it.get("isdir"):
            files.append((it.get("server_filename"), it.get("fs_id"), ""))
            continue
        parent = it.get("path") or ""
        print(f"\n== 3. 列子目录 {parent} ==")
        params = {
            "uk": uk, "shareid": shareid, "order": "other", "desc": "1",
            "showempty": "0", "web": "1", "page": "1", "num": "100", "dir": parent,
            "channel": CHANNEL, "app_id": APP_ID,
        }
        r = s.get(f"{BASE}/share/list", params=params, timeout=20)
        try:
            j = r.json()
        except Exception:
            print("   非 JSON:", r.text[:200])
            continue
        print(f"   errno={j.get('errno')} 条数={len(j.get('list') or [])}")
        for sub in j.get("list") or []:
            print(f"   [{ 'DIR' if sub.get('isdir') else 'FILE'}] "
                  f"{sub.get('server_filename')} path={sub.get('path')} "
                  f"size={sub.get('size')} fs_id={sub.get('fs_id')}")
            if not sub.get("isdir"):
                files.append((sub.get("server_filename"), sub.get("fs_id"), parent))

    if not files:
        print("\n没找到文件，停。")
        return 1

    # tplconfig → sign/timestamp
    print("\n== 4. tplconfig ==")
    cfg = s.get(f"{BASE}/share/tplconfig", params={
        "surl": surl, "fields": "sign,timestamp", "view_mode": 1,
        "channel": CHANNEL, "web": 1, "app_id": APP_ID, "bdstoken": "",
    }, timeout=20).json()
    print("  errno:", cfg.get("errno"))
    sign = (cfg.get("data") or {}).get("sign")
    ts = (cfg.get("data") or {}).get("timestamp")

    # 参数矩阵
    sek_sources = {
        "page": got.get("currentSekey") or "",
        "cookie": s.cookies.get("BDCLND") or "",
        "empty": "",
    }
    name, fid, parent = files[0]
    print(f"\n== 5. sharedownload 参数矩阵：{name} (fs_id={fid}, parent={parent!r}) ==")
    variants = {"empty": "", "parent": json.dumps([parent], ensure_ascii=False),
                "fullpath": json.dumps([f"{parent}/{name}"], ensure_ascii=False)}
    seen = set()
    rows = []
    for sname, sekey in sek_sources.items():
        if sekey in seen and sname != "empty":
            continue
        seen.add(sekey)
        for vname, path_list in variants.items():
            body = {
                "encrypt": "0", "product": "share",
                "uk": str(uk), "primaryid": str(shareid),
                "fid_list": json.dumps([fid]), "path_list": path_list,
                "extra": json.dumps({"sekey": sekey}, ensure_ascii=False),
            }
            js = got.get("jsToken")
            if js:
                body["jsToken"] = js
            r = s.post(f"{BASE}/api/sharedownload", params={
                "sign": sign, "timestamp": ts, "channel": CHANNEL,
                "web": "1", "app_id": APP_ID, "clienttype": "0", "bdstoken": "",
            }, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"},
                timeout=30)
            try:
                j = r.json()
            except Exception:
                j = {"errno": "?", "raw": r.text[:120]}
            errno = j.get("errno")
            has_dlink = bool((j.get("list") or [{}])[0].get("dlink")) if j.get("list") else False
            rows.append((sname, vname, errno, has_dlink, (j.get("show_msg") or "")[:40]))
            print(f"  sekey={sname:<6} path_list={vname:<9} → errno={errno} "
                  f"dlink={'✓' if has_dlink else '×'} {j.get('show_msg') or ''}")

    win = [r for r in rows if r[3]]
    print("\n== 结论 ==")
    if win:
        for r in win:
            print(f"  可用组合：sekey={r[0]} path_list={r[1]}")
    else:
        print("  所有组合都失败 —— 说明不是参数问题（可能是该分享只允许转存/客户端，"
              "或需要登录态）。贴这段输出给我。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
