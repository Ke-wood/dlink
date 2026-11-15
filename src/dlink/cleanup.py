"""网盘侧的清理：下载完成后删除「为取直链而转存」的文件。

背景：
    夸克的分享文件不能直接取直链，必须转存到自己网盘再取。
    不清理的话，网盘根目录会越堆越多「为下载而存在的垃圾」。

设计：
    · 默认**关闭**（用户明确可选）
    · 开关在 settings 里：`auto_cleanup_after_download`
    · 清理是 best-effort：失败只记日志，不影响任务状态
    · 只删**本次转存产生的**文件（靠 transfer.fid 精确指定），绝不按名字批量删

安全原则：**只删扩展明确告诉我们的那个 fid**，不做任何推断。
"""

from __future__ import annotations

import json
from typing import Optional

import requests


def _quark_delete(api_base: str, cookie_header: str, ua: str,
                  pdir_fid: str, fids: list[str], timeout: float = 20.0) -> dict:
    """夸克/UC：先移到回收站，再可选彻底删除。

    接口：POST /file/delete?pr=ucpro&fr=pc
    body: {"action_type": 2, "filelist": [...], "exclude_fids": []}
    action_type: 2 = 移入回收站
    """
    url = f"{api_base}/file/delete?pr=ucpro&fr=pc&uc_param_str="
    body = {
        "action_type": 2,
        "filelist": fids,
        "exclude_fids": [],
    }
    r = requests.post(url, json=body, timeout=timeout,
                      headers={"Cookie": cookie_header, "User-Agent": ua,
                               "Referer": "https://pan.quark.cn/"})
    try:
        return r.json()
    except Exception:
        return {"code": -1, "message": r.text[:200]}


def cleanup_transfer(transfer: dict, cookies: dict, headers: dict) -> dict:
    """删除一个由转存产生的文件。返回 {ok, message}。"""
    if not transfer or not transfer.get("fid"):
        return {"ok": False, "message": "没有转存信息，跳过清理"}

    platform = transfer.get("platform")
    if platform not in ("quark", "uc"):
        return {"ok": False, "message": f"暂不支持清理 {platform}"}

    api_base = transfer.get("apiBase") or (
        "https://pc-api.uc.cn/1/clouddrive" if platform == "uc"
        else "https://drive-pc.quark.cn/1/clouddrive")
    # UC 的 pr 不同
    if platform == "uc":
        api_base = api_base.replace("?pr=ucpro", "?pr=UCBrowser")

    cookie_header = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())
    ua = (headers or {}).get("User-Agent", "")

    try:
        j = _quark_delete(api_base, cookie_header, ua,
                          transfer.get("pdirFid") or "0", [transfer["fid"]])
    except Exception as e:
        return {"ok": False, "message": f"清理请求失败：{e}"}

    if j.get("code") == 0:
        return {"ok": True, "message": "已移入回收站"}
    return {"ok": False, "message": f"清理失败：{j.get('message') or j.get('code')}"}
