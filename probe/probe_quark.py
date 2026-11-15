#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘 / UC 网盘 直链探针 (research 用途)

依据来源（均已源码确认，见 research/05-quark-uc.md）：
  - AList quark_uc 驱动  drivers/quark_uc/{driver.go,util.go,meta.go,uc_token.go}
  - OpenList quark_uc 驱动（无 ut token 的旧版）
  - markgeen/quark-to-gopeed 用户脚本（免登录分享直链）
  - zmj159809/quark-downloader（转存 + 直链）

用法：
  python probe_quark.py url  "https://pan.quark.cn/s/xxxx?pwd=1234"
  python probe_quark.py speed "https://download.quark.cn/.../xxx?sig=..."   # 测 Range/限速
"""
import base64
import json
import re
import struct
import sys
import time

import requests

# ---------------------------------------------------------------- 常量（源码确认）

QUARK = {
    "api":     "https://pan.quark.cn/1/clouddrive",       # 网页端 API
    "api_pc":  "https://drive-pc.quark.cn/1/clouddrive",  # PC 客户端 API（取直链用这个）
    "referer": "https://pan.quark.cn/",
    "pr":      "ucpro",
    # 取直链必须伪装夸克 PC 客户端，否则判为游客并触发 23018 大小限制
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 "
           "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"),
}

UC = {
    "api":     "https://pc-api.uc.cn/1/clouddrive",
    "referer": "https://drive.uc.cn/",
    "pr":      "UCBrowser",
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
           "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"),
}

# UC 下载 token 的固定密钥 = md5("C88EAC2B-F07C-4FE2-ABDF-C8F049C46C7B")[:16]
# 已用 AList 官方测试向量验证：utdid=0123456789abcdefghijklmn
#   -> NelUvDsfgQ5vcbK/BaZ1HoSnF9RePiGAuxFxhYc/dPslAA==
UC_AES_KEY = b"2106f55fd41e800c"
UC_TOKEN_MAGIC = 13801  # 0x35E9


# ---------------------------------------------------------------- UC ut token

def uc_download_token(utdid: str) -> str:
    """UC 专用：把 24 字节 UTDID 加密成 ut 查询参数。需要 pycryptodome 或 cryptography。"""
    if len(utdid) != 24:
        raise ValueError("UC UTDID 必须是 24 字节")
    pad = 16 - len(utdid) % 16
    plaintext = utdid.encode() + bytes([pad]) * pad
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        enc = Cipher(algorithms.AES(UC_AES_KEY), modes.CBC(UC_AES_KEY)).encryptor()
        ct = enc.update(plaintext) + enc.finalize()
    except ImportError:
        from Crypto.Cipher import AES  # type: ignore
        ct = AES.new(UC_AES_KEY, AES.MODE_CBC, UC_AES_KEY).encrypt(plaintext)
    return base64.b64encode(struct.pack(">H", UC_TOKEN_MAGIC) + ct).decode()


def new_utdid() -> str:
    import os
    return base64.b64encode(os.urandom(18)).decode()


# ---------------------------------------------------------------- 分享链接解析

def parse_share_url(url: str) -> dict:
    """支持 https://pan.quark.cn/s/<pwd_id>[?pwd=1234][#/list/share/<fid>]"""
    clean = re.sub(r"\[.*?\]", "", url).strip()
    m = re.search(r"/s/([A-Za-z0-9]+)", clean)
    if m:
        pwd_id = m.group(1)
    elif re.fullmatch(r"[A-Za-z0-9]+", clean) and len(clean) > 10:
        pwd_id = clean
    else:
        raise ValueError("无法解析分享 ID")
    pwd = re.search(r"[?&](?:pwd|password|pw)=([A-Za-z0-9]{4})", clean)
    d = re.search(r"#/list/share/([A-Za-z0-9]+)", clean)
    return {
        "pwd_id": pwd_id,
        "passcode": pwd.group(1) if pwd else "",
        "pdir_fid": d.group(1) if d else "0",
    }


# ---------------------------------------------------------------- HTTP 封装

def api_get(url, conf, cookie="", extra=None):
    h = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Referer": conf["referer"],
        "Origin": conf["referer"].rstrip("/"),
        "User-Agent": conf["ua"],
    }
    if extra:
        h.update(extra)
    r = requests.get(url, headers=h, timeout=30)
    return r.status_code, safe_json(r)


def api_post(url, conf, body, cookie="", extra=None):
    h = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "Referer": conf["referer"],
        "Origin": conf["referer"].rstrip("/"),
        "Content-Type": "application/json;charset=UTF-8",
        "User-Agent": conf["ua"],
    }
    if extra:
        h.update(extra)
    r = requests.post(url, headers=h, json=body, timeout=30)
    return r.status_code, safe_json(r)


def safe_json(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:500], "_status": r.status_code}


# ---------------------------------------------------------------- 完整调用链

def share_token(pwd_id, passcode="", conf=QUARK):
    """1. 换取 stoken。code 31001=需提取码, 31002=分享失效, 41006=不存在"""
    url = f"{conf['api']}/share/sharepage/token?pr={conf['pr']}&fr=pc"
    return api_post(url, conf, {"pwd_id": pwd_id, "passcode": passcode})


def share_detail(pwd_id, stoken, pdir_fid="0", page=1, size=100, conf=QUARK):
    """2. 列目录。返回 data.list[]，每项含 fid / file_name / share_fid_token / size / dir"""
    url = (f"{conf['api']}/share/sharepage/detail?pr={conf['pr']}&fr=pc"
           f"&pwd_id={pwd_id}&stoken={requests.utils.quote(stoken)}"
           f"&pdir_fid={pdir_fid}&_page={page}&_size={size}"
           f"&_sort=file_type:asc,updated_at:desc")
    return api_get(url, conf)


def share_download(pwd_id, stoken, fids, fid_tokens, conf=QUARK, with_ua=True):
    """3. 取直链（游客模式，无需 cookie）。
    返回 data[].download_url。
    code 23018 = 游客大小限制（UA 没伪装成 PC 客户端，或文件超限）
    """
    # 注意：/share/sharepage/download 路径不存在（实测 404），用的是 /file/download
    url = f"{conf['api_pc']}/file/download?entry=ft&fr=pc&pr={conf['pr']}"
    extra = {"User-Agent": conf["ua"]} if with_ua else None
    body = {"fids": fids, "fids_token": fid_tokens, "pwd_id": pwd_id, "stoken": stoken}
    return api_post(url, conf, body, extra=extra)


def own_download(fids, cookie, conf=QUARK, utdid=None):
    """已转存 / 自己网盘的文件取直链（需要登录 cookie）。UC 需额外 ut 参数。"""
    url = f"{conf['api_pc']}/file/download?pr={conf['pr']}&fr=pc"
    if conf is UC or (utdid and conf.get("pr") == "UCBrowser"):
        ut = uc_download_token(utdid or new_utdid())
        url += f"&ut={requests.utils.quote(ut)}"
    return api_post(url, conf, {"fids": fids}, cookie=cookie)


# ---------------------------------------------------------------- Range / 测速

def probe_range(url, conf=QUARK, cookie="", nbytes=4 * 1024 * 1024):
    """检查直链是否支持 Range(206) 并测单连接速度。直链必须带 Cookie+Referer+UA。"""
    h = {"Cookie": cookie, "Referer": conf["referer"], "User-Agent": conf["ua"],
         "Range": "bytes=0-0"}
    t0 = time.time()
    r = requests.get(url, headers=h, timeout=30)
    print(f"[head] status={r.status_code} (206=支持Range) "
          f"len={r.headers.get('Content-Length')} "
          f"CR={r.headers.get('Content-Range')} "
          f"Accept-Ranges={r.headers.get('Accept-Ranges')} "
          f"server={r.headers.get('Server')} {time.time()-t0:.2f}s")
    print(f"[head] url host={requests.utils.urlparse(url).netloc}")

    h2 = dict(h, Range=f"bytes=0-{nbytes-1}")
    t0 = time.time()
    got = 0
    with requests.get(url, headers=h2, stream=True, timeout=30) as resp:
        print(f"[get ] status={resp.status_code} CR={resp.headers.get('Content-Range')}")
        for chunk in resp.iter_content(65536):
            got += len(chunk)
            if got >= nbytes:
                break
    dt = time.time() - t0
    print(f"[get ] {got/1048576:.2f} MB in {dt:.2f}s = {got/1048576/dt:.2f} MB/s (单连接)")
    return got / 1048576 / dt if dt > 0 else 0


def probe_multi(url, conf=QUARK, cookie="", conns=8, nbytes=4 * 1024 * 1024):
    """多连接并行拉同一文件的前 N MB，看聚合是否线性增长（判断连接级 vs 账号级限速）"""
    import concurrent.futures as cf

    def one(i):
        h = {"Cookie": cookie, "Referer": conf["referer"], "User-Agent": conf["ua"],
             "Range": f"bytes={i*nbytes}-{i*nbytes+nbytes-1}"}
        t0 = time.time()
        got = 0
        try:
            with requests.get(url, headers=h, stream=True, timeout=30) as resp:
                st = resp.status_code
                for c in resp.iter_content(65536):
                    got += len(c)
                    if got >= nbytes:
                        break
        except Exception as e:
            return i, 0, 0, type(e).__name__
        return i, got, st, time.time() - t0

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=conns) as ex:
        res = list(ex.map(one, range(conns)))
    wall = time.time() - t0
    total = sum(r[1] for r in res)
    for i, got, st, dt in res:
        if isinstance(dt, float):
            print(f"  conn{i}: {got/1048576:.2f}MB {dt:.2f}s st={st}")
        else:
            print(f"  conn{i}: FAIL {dt}")
    print(f"[multi] {conns} 连接合计 {total/1048576:.2f}MB / {wall:.2f}s "
          f"= {total/1048576/wall:.2f} MB/s 聚合")


# ---------------------------------------------------------------- CLI

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "url" and len(sys.argv) > 2:
        p = parse_share_url(sys.argv[2])
        print("解析:", p)
        st, body = share_token(p["pwd_id"], p["passcode"])
        print("token:", st, json.dumps(body, ensure_ascii=False)[:400])
        if body.get("code") != 0:
            return
        stoken = body["data"]["stoken"]
        print("stoken:", stoken[:16] + "...")
        st, body = share_detail(p["pwd_id"], stoken, p["pdir_fid"])
        print("detail:", st, json.dumps(body, ensure_ascii=False)[:600])
        lst = (body.get("data") or {}).get("list") or []
        if not lst:
            return
        fids = [x["fid"] for x in lst[:5]]
        toks = [x["share_fid_token"] for x in lst[:5]]
        st, body = share_download(p["pwd_id"], stoken, fids, toks)
        print("download:", st, json.dumps(body, ensure_ascii=False)[:900])
    elif cmd == "speed" and len(sys.argv) > 2:
        probe_range(sys.argv[2])
        probe_multi(sys.argv[2])
    elif cmd == "uctoken":
        did = sys.argv[2] if len(sys.argv) > 2 else "0123456789abcdefghijklmn"
        print("utdid:", did)
        print("ut   :", uc_download_token(did))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
