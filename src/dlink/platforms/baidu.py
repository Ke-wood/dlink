"""百度网盘平台适配。

关键实测结论（详见 docs/BAIDU-BYPASS-FINAL.md）：

  1. 限速是**账号级配额**（约 80KB/s），并发无效甚至触发风控。
     → 本平台的 `recommended_workers()` 返回 1。
  2. 分享直链可以**直接取**，无需转存，但需要两个**页面全局变量**：
     `jsToken` 和 `sekey`（不在 cookie 里）。
  3. 直链 `expires=8h`，且与 cookie 强绑定。
  4. 网页接口需要 `BDUSS` + **`STOKEN`**（缺 STOKEN 一律 errno:-6），
     外加 `bdstoken`（从 /api/gettemplatevariable 取）。

本模块只做"取直链"，不做下载。下载交给通用 downloader。
"""

from __future__ import annotations

import json
import random
import re
import string
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

from ..hashing import clean_md5
from ..models import (
    AuthRequired,
    DirectLink,
    DLinkError,
    NotLoggedIn,
    RemoteFile,
    ShareInfo,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE = "https://pan.baidu.com"
APP_ID = "250528"
CHANNEL = "chunlei"


class BaiduError(DLinkError):
    code = "E_BAIDU"


class BaiduSession:
    """百度登录态容器。

    必需：BDUSS（身份）+ STOKEN（网页接口鉴权）。
    STOKEN 在 `.passport.baidu.com` 域下，很容易漏，缺了会一律 -6。
    """

    REQUIRED = ("BDUSS",)

    def __init__(self, cookies: dict, ua: str = "") -> None:
        self.cookies = dict(cookies or {})
        self.ua = ua or UA
        self._bdstoken: str = ""

    @property
    def is_logged_in(self) -> bool:
        return bool(self.cookies.get("BDUSS"))

    def has_stoken(self) -> bool:
        return bool(self.cookies.get("STOKEN"))

    def missing(self) -> list[str]:
        out = [k for k in ("BDUSS", "STOKEN") if not self.cookies.get(k)]
        return out

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class BaiduPlatform:
    name = "baidu"

    # ---------- 平台识别 ----------

    @staticmethod
    def match(url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in ("pan.baidu.com", "yun.baidu.com", "eyun.baidu.com")

    @staticmethod
    def parse_share_key(url: str) -> str:
        """从分享链接取 surl（如 1AbCdEfGhIjKlMnOpQrS）。"""
        u = urlparse(url)
        m = re.search(r"/s/([A-Za-z0-9_\-]+)", u.path)
        if not m:
            raise BaiduError(
                f"无法从链接解析出分享 ID: {url}",
                hint="期望形如 https://pan.baidu.com/s/1xxxxxxxx 的链接",
            )
        return m.group(1)

    @staticmethod
    def parse_pwd_from_url(url: str) -> Optional[str]:
        u = urlparse(url)
        q = parse_qs(u.query)
        for k in ("pwd", "password"):
            if q.get(k):
                return q[k][0]
        # #list/path=%2F&pwd=xxxx 这种
        frag = u.fragment or ""
        m = re.search(r"pwd=([A-Za-z0-9]{4})", frag)
        return m.group(1) if m else None

    # ---------- 基础请求 ----------

    def __init__(self, session: BaiduSession | None = None) -> None:
        self.session = session or BaiduSession({})
        self._bdstoken: str = ""
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": self.session.ua,
            "Cookie": self.session.cookie_header(),
            "Referer": f"{BASE}/disk/main",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 百度偶尔返回损坏的 gzip 流（zlib: incorrect header check），
            # 关掉压缩最省事，代价是流量略大。
            "Accept-Encoding": "identity",
        })

    def _check_login(self) -> None:
        if not self.session.is_logged_in:
            raise NotLoggedIn(
                "百度网盘未登录：缺少 BDUSS",
                hint="在浏览器登录 pan.baidu.com，用扩展导出 cookie（需要 BDUSS 与 STOKEN）",
            )
        if not self.session.has_stoken():
            raise NotLoggedIn(
                "百度网盘缺少 STOKEN（在 .passport.baidu.com 域下）",
                hint="只给 BDUSS 时网页接口一律返回 errno:-6，必须同时提供 STOKEN",
            )

    def bdstoken(self) -> str:
        """取 bdstoken（网页接口多数需要）。"""
        if self._bdstoken:
            return self._bdstoken
        self._check_login()
        try:
            r = self._http.get(
                f"{BASE}/api/gettemplatevariable",
                params={"fields": '["bdstoken"]'}, timeout=20)
            j = r.json()
        except Exception as e:
            raise BaiduError(f"取 bdstoken 失败: {e}") from e
        if j.get("errno") != 0:
            raise BaiduError(
                f"取 bdstoken 失败：errno={j.get('errno')}",
                hint="errno=-6 通常表示 cookie 不完整或已失效",
            )
        self._bdstoken = (j.get("result") or {}).get("bdstoken", "")
        return self._bdstoken

    def _get(self, path: str, **params) -> dict:
        params.setdefault("channel", CHANNEL)
        params.setdefault("web", 1)
        params.setdefault("app_id", APP_ID)
        params.setdefault("clienttype", 0)
        params.setdefault("bdstoken", self.bdstoken())
        r = self._http.get(f"{BASE}{path}", params=params, timeout=20)
        try:
            return r.json()
        except Exception:
            raise BaiduError(
                f"{path} 返回了非 JSON（可能被重定向到验证页）",
                hint="通常意味着 cookie 不完整或触发了风控",
            ) from None

    def _post(self, path: str, data: dict, **params) -> dict:
        params.setdefault("channel", CHANNEL)
        params.setdefault("web", 1)
        params.setdefault("app_id", APP_ID)
        params.setdefault("clienttype", 0)
        r = self._http.post(f"{BASE}{path}", params=params, data=data, timeout=20)
        try:
            return r.json()
        except Exception:
            raise BaiduError(f"{path} 返回非 JSON") from None

    # ---------- 分享解析 ----------

    def get_share_info(self, surl: str, pwd: Optional[str] = None) -> ShareInfo:
        """取分享元信息。

        先用 `/share/tplconfig`（免登录可用）拿基础信息，
        再访问分享页 HTML 提取 yunData 中的 shareid / share_uk / fs_id。
        """
        # 提取码校验（有 pwd 时）
        if pwd:
            self._verify_pwd(surl, pwd)

        html = self._fetch_share_page(surl, pwd)
        yun = self._extract_yundata(html)
        if not yun:
            raise BaiduError(
                "分享页里找不到 yunData",
                hint="可能是分享已失效、需要提取码，或页面结构变了",
            )

        files = yun.get("file_list") or []
        total_files = sum(1 for f in files if not f.get("isdir"))
        total_dirs = sum(1 for f in files if f.get("isdir"))
        title = ""
        if files:
            title = files[0].get("server_filename", "")
        return ShareInfo(
            platform=self.name,
            share_key=surl,
            title=title,
            owner=str(yun.get("share_uk", "")),
            need_pwd=bool(pwd is None and "请输入提取码" in html),
            root_key="",
            total_dirs=total_dirs,
            total_files=total_files,
            extra={"shareid": yun.get("shareid"), "share_uk": yun.get("share_uk")},
        )

    def _fetch_share_page(self, surl: str, pwd: Optional[str]) -> str:
        params = {"pwd": pwd} if pwd else {}
        r = self._http.get(f"{BASE}/s/{surl}", params=params, timeout=20)
        return r.text

    def _extract_yundata(self, html: str) -> dict:
        """从页面 HTML 抠 yunData。

        实测教训：百度现在的分享页是 SPA，`yunData` 是 JS 运行时注入的，
        静态 HTML 里往往没有。所以先试正则，失败则退回用接口自己拼出来。
        """
        m = re.search(r"(?:var\s+)?yunData\s*=\s*(\{.*?\});\s*(?:var|window|</script>)",
                      html, re.S)
        if not m:
            m2 = re.search(r'(\{"[^{}]*"shareid".*?\})\s*;', html, re.S)
            if not m2:
                return {}
            raw = m2.group(1)
        else:
            raw = m.group(1)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _share_context(self, surl: str, pwd: Optional[str]) -> dict:
        """不依赖页面 HTML，用接口拼出 {shareid, share_uk, file_list}。

        实测：`/api/shorturlinfo` 直接给出 shareid 与 uk，比扒 HTML 稳得多。
        """
        params = {"shorturl": surl, "root": 1,
                  "channel": CHANNEL, "web": 1, "app_id": APP_ID, "clienttype": 0}
        if pwd:
            params["pwd"] = pwd
        r = self._http.get(f"{BASE}/api/shorturlinfo", params=params, timeout=20)
        try:
            j = r.json()
        except Exception:
            raise BaiduError("shorturlinfo 返回非 JSON") from None
        if j.get("errno") != 0:
            errno = j.get("errno")
            # -9: 提取码验证失败；105: 需要提取码
            if errno in (-9, 105):
                raise AuthRequired(
                    f"shorturlinfo errno={errno}（提取码问题）",
                    hint="分享需要提取码，或提取码错误。浏览器已通过提取码时，"
                         "把 BDCLND cookie 一并带上可跳过校验")
            raise BaiduError(f"shorturlinfo errno={errno}")

        # 结构：{errno:0, file_list:[...], shareid, uk, ...}
        ctx = {
            "shareid": j.get("shareid"),
            "share_uk": j.get("uk") or j.get("share_uk"),
            "file_list": j.get("file_list") or [],
        }
        if not ctx["shareid"] or not ctx["share_uk"]:
            raise BaiduError("shorturlinfo 缺 shareid / uk")
        return ctx

    def _verify_pwd(self, surl: str, pwd: str) -> None:
        """校验提取码。

        注意：百度现在的分享页把提取码放在 URL (?pwd=) 就够了，服务端会种
        BDCLND cookie。`/share/verify` 是老接口，参数形态已变，调用它反而会
        引入假失败。所以这里改为「带 pwd 访问分享页，看 yunData 是否出现」。

        只有明确拿到「需要提取码」的信号才抛 AuthRequired。
        """
        html = self._fetch_share_page(surl, pwd)
        if "请输入提取码" in html and "yunData" not in html:
            raise AuthRequired(
                f"提取码无效或未被接受：{pwd}",
                hint="确认提取码正确；部分分享的提取码需通过 ?pwd= 参数传递",
            )

    # ---------- 文件列表 ----------

    def list_files(self, surl: str, pwd: Optional[str] = None,
                   recursive: bool = True) -> list[RemoteFile]:
        """列分享内全部文件。走 /share/list（免登录即可）。"""
        html = self._fetch_share_page(surl, pwd)
        yun = self._extract_yundata(html)
        if not yun:
            raise BaiduError("解析分享页失败，找不到 yunData")
        shareid = yun.get("shareid")
        share_uk = yun.get("share_uk")
        if not shareid or not share_uk:
            raise BaiduError("yunData 里缺 shareid / share_uk")

        out: list[RemoteFile] = []
        seen_dirs: set[str] = set()
        queue: list[tuple[str, int]] = [("/", 0)]

        while queue:
            path, depth = queue.pop(0)
            if depth > 16:
                continue
            items = self._share_list(surl, shareid, share_uk, path, pwd)
            for it in items:
                if it.get("isdir"):
                    sub = it.get("path") or ""
                    if sub and sub not in seen_dirs:
                        seen_dirs.add(sub)
                        if recursive:
                            queue.append((sub, depth + 1))
                else:
                    out.append(RemoteFile(
                        file_id=str(it.get("fs_id", "")),
                        name=it.get("server_filename", ""),
                        size=int(it.get("size") or 0),
                        parent_key=path,
                        md5=clean_md5(it.get("md5")),
                        extra={"path": it.get("path"), "shareid": shareid,
                               "share_uk": share_uk},
                    ))
        return out

    def _share_list(self, surl: str, shareid, share_uk, dir_path: str,
                    pwd: Optional[str]) -> list[dict]:
        params = {
            "uk": str(share_uk), "shareid": str(shareid), "order": "other", "desc": 1,
            "showempty": 0, "web": 1, "page": 1, "num": 100,
            "dir": dir_path, "t": f"0.{random.random()}",
            "channel": CHANNEL, "app_id": APP_ID,
        }
        if pwd:
            params["pwd"] = pwd
        try:
            j = self._get("/share/list", **params)
        except BaiduError:
            return []
        if j.get("errno") != 0:
            if j.get("errno") in (105, -9):  # 提取码错
                raise AuthRequired(f"列目录失败，可能需要提取码: errno={j.get('errno')}")
            return []
        return j.get("list") or []

    # ---------- 取直链（核心）----------

    def get_direct_link(self, surl: str, f: RemoteFile,
                        pwd: Optional[str] = None) -> DirectLink:
        """取单个文件的直链。

        优先「分享直下」（无需转存，需 jsToken + sekey）；
        失败则退回「转存 + 取自己网盘直链」。
        """
        self._check_login()
        try:
            return self._share_direct_link(surl, f, pwd)
        except BaiduError as first_err:
            try:
                return self._transfer_then_link(surl, f, pwd)
            except BaiduError as second_err:
                raise BaiduError(
                    f"取直链失败。分享直下：{first_err.message} / 转存：{second_err.message}",
                    hint="两个通道都失败，通常是分享已失效或账号风控",
                ) from second_err

    def _share_direct_link(self, surl: str, f: RemoteFile,
                           pwd: Optional[str]) -> DirectLink:
        """分享直下。需要页面里的 jsToken 与 sekey（不在 cookie 中）。"""
        # 用接口拿上下文，不依赖 SPA 注入的 HTML
        ctx = self._share_context(surl, pwd)
        shareid, share_uk = ctx["shareid"], ctx["share_uk"]

        js_token, sekey = self._extract_js_token(self._fetch_share_page(surl, pwd))
        if not js_token:
            raise BaiduError("页面里找不到 jsToken（分享直下需要它）")

        # 取 sign / timestamp
        cfg = self._http.get(
            f"{BASE}/share/tplconfig",
            params={"surl": surl, "fields": "sign,timestamp,jsToken", "view_mode": 1,
                    "channel": CHANNEL, "web": 1, "app_id": APP_ID, "bdstoken": ""},
            timeout=20)
        try:
            cj = cfg.json()
        except Exception:
            raise BaiduError("tplconfig 返回非 JSON")
        if cj.get("errno") != 0:
            raise BaiduError(f"tplconfig 失败: errno={cj.get('errno')}")
        data = cj.get("data") or {}
        sign, ts = data.get("sign"), data.get("timestamp")

        extra = json.dumps({"sekey": sekey or ""}, ensure_ascii=False)
        j = self._post(
            "/api/sharedownload",
            data={
                "encrypt": "0", "product": "share",
                "uk": str(share_uk), "primaryid": str(shareid),
                "fid_list": f"[{f.file_id}]", "path_list": "",
                "extra": extra, "jsToken": js_token,
            },
            sign=sign, timestamp=ts,
        )
        if j.get("errno") != 0:
            raise BaiduError(
                f"sharedownload errno={j.get('errno')} {j.get('show_msg','')}")

        lst = j.get("list") or []
        if not lst:
            raise BaiduError("sharedownload 返回空 list")
        return self._to_direct_link(lst[0], f)

    def _extract_js_token(self, html: str) -> tuple[str, str]:
        """从分享页 HTML 抠 jsToken 与 sekey。"""
        js_token = ""
        m = re.search(r'jsToken["\']?\s*[:=]\s*["\']([0-9a-fA-F]{16,})["\']', html)
        if m:
            js_token = m.group(1)
        sekey = ""
        for pat in (r'currentSekey\s*[:=]\s*["\']([A-Za-z0-9+/=_\-]{20,})["\']',
                    r'"sekey"\s*:\s*"([A-Za-z0-9+/=_\-]{20,})"'):
            m2 = re.search(pat, html)
            if m2:
                sekey = m2.group(1)
                break
        return js_token, sekey

    def _transfer_then_link(self, surl: str, f: RemoteFile,
                            pwd: Optional[str]) -> DirectLink:
        """兜底：转存到自己网盘，再取自己网盘的直链。会污染网盘（需自行清理）。"""
        ctx = self._share_context(surl, pwd)
        shareid, share_uk = ctx["shareid"], ctx["share_uk"]

        cfg = self._http.get(
            f"{BASE}/share/tplconfig",
            params={"surl": surl, "fields": "sign,timestamp", "view_mode": 1,
                    "channel": CHANNEL, "web": 1, "app_id": APP_ID, "bdstoken": ""},
            timeout=20).json()
        data = cfg.get("data") or {}

        # 转存：参数必须走 form-data，且 fsidlist 是 JSON 数组字符串
        r = self._http.post(
            f"{BASE}/share/transfer",
            params={
                "shareid": shareid, "from": share_uk, "sekey": "",
                "ondup": "newcopy", "async": 1,
                "channel": CHANNEL, "web": 1, "app_id": APP_ID, "clienttype": 0,
                "bdstoken": self.bdstoken(),
                "sign": data.get("sign"), "timestamp": data.get("timestamp"),
            },
            data={"fsidlist": json.dumps([int(f.file_id)]), "path": "/"},
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=30,
        )
        try:
            j = r.json()
        except Exception:
            raise BaiduError("转存返回非 JSON")
        if j.get("errno") != 0:
            raise BaiduError(f"转存失败 errno={j.get('errno')} {j.get('show_msg','')}")

        to_fs_id = None
        for it in (j.get("extra") or {}).get("list") or []:
            if it.get("to_fs_id"):
                to_fs_id = it["to_fs_id"]
                break
        if not to_fs_id:
            raise BaiduError("转存成功但拿不到 to_fs_id")

        j2 = self._get("/api/filemetas", dlink=1, fsids=f"[{to_fs_id}]",
                       thumb=1, wpfile=1)
        if j2.get("errno") != 0:
            raise BaiduError(f"filemetas errno={j2.get('errno')}")
        info = (j2.get("info") or [{}])[0]
        return self._to_direct_link(info, f)

    def _to_direct_link(self, info: dict, f: RemoteFile) -> DirectLink:
        url = info.get("dlink") or ""
        if not url:
            raise BaiduError("响应里没有 dlink")
        # 直链必须带 BDUSS 系 cookie，否则 403 error_code:31045
        down_cookies = {k: v for k, v in self.session.cookies.items()
                        if k.startswith("BDUSS") or k in ("STOKEN", "BAIDUID", "BIDUPSID")}
        return DirectLink(
            url=url,
            cookies=down_cookies,
            headers={"Referer": f"{BASE}/", "User-Agent": self.session.ua},
            size=int(info.get("size") or f.size or 0),
            md5=clean_md5(info.get("md5")) or clean_md5(f.md5),
            filename=info.get("server_filename") or f.name,
            expires_at=None,  # 平台给 expires=8h，但不给绝对时间戳；取链后立即下载
            extra={"note": "百度直链 expires=8h"},
        )

    # ---------- 策略 ----------

    def recommended_workers(self) -> int:
        """百度是账号级配额，并发只会触发风控（实测 8 路 6 个 403）。

        BaiduPCS-Go 作者建议：max_parallel=1。
        """
        return 1
