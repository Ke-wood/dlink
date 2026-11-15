"""夸克网盘 / UC 网盘平台适配。

两者同属阿里，API 结构一致，差异在于：
  · 域名：quark = drive-pc.quark.cn；UC = pc-api.uc.cn
  · pr 参数：ucpro / UCBrowser
  · UA：必须伪装各自 PC 客户端，否则被判游客（触发 23018 大小限制）
  · UC 多一个 `ut` 查询参数（AES-128-CBC 加密的 UTDID）

关键实测/源码事实（详见 research/05-quark-uc.md）：
  1. 取直链接口是 `POST /1/clouddrive/file/download`，body `{"fids":[...]}`
     ⚠️ 网上流传的 `/share/sharepage/download` **不存在（404）**。
  2. **分享文件直取需要登录**（`31001 require login [share missing]`）。
  3. **cookie 快照必须与取链时一致**——下载 URL 的签名基于取链时携带的 cookie 生成，
     下载请求头必须与之完全相同，否则 403（会在中途 token 刷新时踩到）。
  4. 限速是「二阶段」：
     · 窗口期：多连接有效，可达 15-20 MB/s
     · 限流期（实测多见于 18:00-23:00）：总量约 1.8 MB/s 恒定，约 100KB/s/连接
     → 因此 workers 给 6，但要求 downloader 在限流期能降级。
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

from ..models import (
    AuthRequired,
    DirectLink,
    DLinkError,
    NotLoggedIn,
    RemoteFile,
    ShareInfo,
)


class QuarkError(DLinkError):
    code = "E_QUARK"


# ---------- 平台常量 ----------

QUARK = {
    "name": "quark",
    "api": "https://drive-pc.quark.cn/1/clouddrive",
    "referer": "https://pan.quark.cn/",
    "pr": "ucpro",
    "hosts": ("pan.quark.cn",),
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 "
           "Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch"),
}

UC = {
    "name": "uc",
    "api": "https://pc-api.uc.cn/1/clouddrive",
    "referer": "https://drive.uc.cn/",
    "pr": "UCBrowser",
    "hosts": ("drive.uc.cn", "fast.uc.cn"),
    "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
           "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"),
}

# UC 的 ut 参数：AES-128-CBC，固定 key = md5("C88EAC2B-F07C-4FE2-ABDF-C8F049C46C7B")[:16]
# 已用 AList 官方测试向量核对：
#   utdid=0123456789abcdefghijklmn -> NelUvDsfgQ5vcbK/BaZ1HoSnF9RePiGAuxFxhYc/dPslAA==
UC_AES_KEY = b"2106f55fd41e800c"
UC_TOKEN_MAGIC = 0x35E9


def uc_download_token(utdid: str) -> str:
    """把 24 字节 UTDID 加密成 UC 的 `ut` 查询参数。"""
    if len(utdid) != 24:
        raise QuarkError(f"UC 的 UTDID 必须是 24 字节，收到 {len(utdid)}")
    pad = 16 - len(utdid) % 16
    plaintext = utdid.encode() + bytes([pad]) * pad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = Cipher(algorithms.AES(UC_AES_KEY), modes.CBC(UC_AES_KEY)).encryptor()
    ct = enc.update(plaintext) + enc.finalize()
    return base64.b64encode(struct.pack(">H", UC_TOKEN_MAGIC) + ct).decode()


# ---------- 会话 ----------

class QuarkSession:
    """夸克/UC 登录态。cookie 需要包含 `__puus`（核心凭证）与 `__pus`。"""

    def __init__(self, cookies: dict, ua: str = "", utdid: str = "") -> None:
        self.cookies = dict(cookies or {})
        self.utdid = utdid or self.cookies.get("__puus", "")[:24] or ""
        self._ua = ua

    def is_logged_in(self) -> bool:
        return bool(self.cookies.get("__puus") or self.cookies.get("__pus"))

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


# ---------- 平台类 ----------

class QuarkPlatform:
    """夸克与 UC 共用，通过 CONFIG 区分。"""

    CONFIG = QUARK
    name = "quark"

    @classmethod
    def match(cls, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in cls.CONFIG["hosts"]

    @staticmethod
    def parse_share_key(url: str) -> str:
        m = re.search(r"/s/([A-Za-z0-9]+)", urlparse(url).path)
        if not m:
            raise QuarkError(f"无法解析分享 ID: {url}",
                             hint="期望 https://pan.quark.cn/s/xxxxxxxx")
        return m.group(1)

    @staticmethod
    def parse_pwd_from_url(url: str) -> Optional[str]:
        u = urlparse(url)
        q = parse_qs(u.query)
        for k in ("pwd", "passcode", "password"):
            if q.get(k):
                return q[k][0]
        return None

    def __init__(self, session: QuarkSession | None = None) -> None:
        self.session = session or QuarkSession({})
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": self.session._ua or self.CONFIG["ua"],
            "Cookie": self.session.cookie_header(),
            "Referer": self.CONFIG["referer"],
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        })

    # ---------- 底层请求 ----------

    def _url(self, path: str, **extra) -> str:
        from urllib.parse import urlencode
        params = {"pr": self.CONFIG["pr"], "fr": "pc", "uc_param_str": ""}
        if self.name == "uc" and self.session.utdid:
            try:
                params["ut"] = uc_download_token(self.session.utdid)
            except Exception:
                pass
        params.update(extra)
        return f"{self.CONFIG['api']}{path}?{urlencode(params)}"

    def _req(self, method: str, path: str, body: dict | None = None,
             **extra) -> dict:
        url = self._url(path, **extra)
        r = self._http.request(method, url, data=json.dumps(body) if body else None,
                               timeout=25)
        try:
            return r.json()
        except Exception:
            raise QuarkError(f"{path} 返回非 JSON") from None

    def _need_login(self) -> None:
        if not self.session.is_logged_in():
            raise NotLoggedIn(
                f"{self.CONFIG['name']} 未登录：缺少 __puus cookie",
                hint="在浏览器登录后，用扩展导出 cookie",
            )

    # ---------- 分享解析 ----------

    def get_share_info(self, pwd_id: str, passcode: Optional[str] = None) -> ShareInfo:
        data = self._share_token(pwd_id, passcode)
        return ShareInfo(
            platform=self.name,
            share_key=pwd_id,
            title=(data.get("title") or ""),
            owner=((data.get("author") or {}).get("nickname") or ""),
            need_pwd=False,
            total_files=0,
            extra={"stoken": data.get("stoken")},
        )

    def _share_token(self, pwd_id: str, passcode: Optional[str]) -> dict:
        j = self._req("POST", "/share/sharepage/token",
                      {"pwd_id": pwd_id, "passcode": passcode or ""})
        if j.get("code") != 0:
            code = j.get("code")
            if code in (41008, 41009, 41010):  # 需要/错误提取码
                raise AuthRequired(f"提取码问题：code={code} {j.get('message','')}",
                                   hint="检查提取码是否正确")
            raise QuarkError(f"取 stoken 失败：code={code} {j.get('message','')}")
        return j.get("data") or {}

    def list_files(self, pwd_id: str, passcode: Optional[str] = None,
                   recursive: bool = True, max_items: int = 2000) -> list[RemoteFile]:
        stoken = self._share_token(pwd_id, passcode).get("stoken", "")
        out: list[RemoteFile] = []
        queue = [("0", "")]
        seen: set[str] = set()
        while queue and len(out) < max_items:
            pdir, prefix = queue.pop(0)
            if pdir in seen:
                continue
            seen.add(pdir)
            items = self._share_detail(pwd_id, stoken, pdir)
            for it in items:
                fid = it.get("fid", "")
                name = it.get("file_name", "")
                is_dir = bool(it.get("dir"))
                if is_dir:
                    # 即使不递归，也把目录本身作为条目返回，避免"根目录只有文件夹"时得到空列表
                    if recursive:
                        queue.append((fid, f"{prefix}{name}/"))
                    else:
                        out.append(RemoteFile(
                            file_id=fid, name=name, size=0, parent_key=pdir,
                            extra={"is_dir": True, "path": f"{prefix}{name}/"},
                        ))
                else:
                    out.append(RemoteFile(
                        file_id=fid,
                        name=name,
                        size=int(it.get("size") or 0),
                        parent_key=pdir,
                        extra={"path": f"{prefix}{name}", "pwd_id": pwd_id,
                               "stoken": stoken},
                    ))
        return out

    def _share_detail(self, pwd_id: str, stoken: str, pdir: str) -> list[dict]:
        j = self._req(
            "GET", "/share/sharepage/detail",
            pwd_id=pwd_id, stoken=stoken, pdir_fid=pdir, force=0,
            _page=1, _size=200, _fetch_banner=1, _fetch_share=1,
            _fetch_total=1, _sort="file_type:asc,updated_at:desc",
        )
        if j.get("code") != 0:
            return []
        return ((j.get("data") or {}).get("list")) or []

    # ---------- 取直链 ----------

    def get_direct_link(self, pwd_id: str, f: RemoteFile,
                        passcode: Optional[str] = None) -> DirectLink:
        """取直链。

        注意：分享文件直取需要登录（31001）。未登录时必须先转存。
        """
        self._need_login()
        # 取链前对 cookie 拍快照 —— 签名与这份 cookie 绑定，下载必须用它
        cookie_snapshot = self.session.cookie_header()

        j = self._req("POST", "/file/download", {"fids": [f.file_id]})
        code = j.get("code")
        if code == 31001:
            raise NotLoggedIn(
                f"分享文件取直链需要登录：{j.get('message','')}",
                hint="先在网页转存到自己网盘，再取直链",
            )
        if code == 23018:
            raise QuarkError(
                f"被判为游客（23018 大小限制）：{j.get('message','')}",
                hint="取直链必须使用 PC 客户端 UA，且 cookie 必须完整（含 __puus）",
            )
        if code != 0:
            raise QuarkError(f"取直链失败：code={code} {j.get('message','')}")

        data = (j.get("data") or [])
        if not data:
            raise QuarkError("响应里没有直链")
        url = data[0].get("download_url") or ""
        if not url:
            raise QuarkError("download_url 为空")

        return DirectLink(
            url=url,
            cookies=self._snapshot_dict(cookie_snapshot),
            headers={
                "Referer": self.CONFIG["referer"],
                "User-Agent": self.session._ua or self.CONFIG["ua"],
            },
            size=int(f.size or 0),
            md5="",
            filename=f.name,
            expires_at=None,
            extra={"note": "夸克直链与取链时的 cookie 强绑定，不能用新 cookie"},
        )

    @staticmethod
    def _snapshot_dict(cookie_header: str) -> dict:
        out = {}
        for kv in cookie_header.split("; "):
            if "=" in kv:
                k, _, v = kv.partition("=")
                out[k] = v
        return out

    # ---------- 策略 ----------

    def recommended_workers(self) -> int:
        """夸克二阶段限速：窗口期多连接有效，限流期总量恒定。

        给 6 路：窗口期能吃满带宽；进入限流期后由 downloader 检测到
        「聚合不再增长」时降级到 1 路。
        """
        return 6


class UCPlatform(QuarkPlatform):
    CONFIG = UC
    name = "uc"


class QuarkUC:
    """按 URL 自动分派到 Quark 或 UC。"""

    @staticmethod
    def detect(url: str) -> Optional[QuarkPlatform]:
        if QuarkPlatform.match(url):
            return QuarkPlatform()
        if UCPlatform.match(url):
            return UCPlatform()
        return None
