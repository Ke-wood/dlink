"""腾讯微云（share.weiyun.com）平台适配层。

实现依据是 `docs/FINDINGS.md` 的实测结论，不要用"常识"覆盖它。要点：

1. 接口 = ``POST https://share.weiyun.com/webapp/json/{protocol}/{name}?refer=chrome_windows&g_tk={g_tk}&r={rand}``
2. 请求头**必须**带 ``Accept: application/json, text/plain, */*``；
   缺 Accept 会得到 **HTTP 403 空 body**，非常容易被误判成"未登录/风控"。
3. 信封是**双重 JSON 编码**：``{"req_header": "<JSON串>", "req_body": "<JSON串>"}``。
4. ``g_tk`` 按域绑定，必须用 ``skey`` 以 QQ 系算法现算（同一域的 cookie → 同一 g_tk）。
5. 免登录路径：``weiyunShareNoLogin/WeiyunShareView``、``weiyunShareNoLogin/WeiyunShareDirList``。
   取直链：权威路径 ``weiyunShare/WeiyunShareBatchDownload``（要登录），
   降级路径 ``weiyunShareNoLogin/WeiyunShareBatchDownload``（实测端点存在、未验证成功）。
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Iterable, Optional
from urllib.parse import unquote

import requests

from ..models import (
    AuthRequired,
    DirectLink,
    DLinkError,
    LinkExpired,
    NotLoggedIn,
    RemoteFile,
    ShareInfo,
)

__all__ = ["WeiyunSession", "WeiyunPlatform"]

# ---------------------------------------------------------------- 常量

API_BASE = "https://share.weiyun.com/webapp/json/"
REFER = "chrome_windows"
APPID = 30151
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SHARE_HOST = "share.weiyun.com"

# 接口表：protocol / name / cmd
EP_VIEW = ("weiyunShareNoLogin", "WeiyunShareView", 0)  # View 走 GET，不带 cmd
EP_DIRLIST = ("weiyunShareNoLogin", "WeiyunShareDirList", 12031)
EP_DL_AUTH = ("weiyunShare", "WeiyunShareBatchDownload", 12024)
EP_DL_NOLOGIN = ("weiyunShareNoLogin", "WeiyunShareBatchDownload", 12024)

# retcode
RC_OK = 0
RC_NOT_LOGGED_IN = 190011
RC_BUSY = 24203

# Body 里"业务块"的键名，按接口名推导：.weiyun.{name}MsgReq_body
BODY_KEY_FMT = ".weiyun.{}MsgReq_body"

MAX_DEPTH = 32  # 目录递归深度上限，防目录环
PAGE_SIZE = 100  # DirList 单页条数；num 字段

_SKEY_ERROR_HINT = (
    "403 且 body 为空通常不是权限问题：①请求缺 `Accept: application/json, text/plain, */*`；"
    "②带了 skey 但 g_tk 与 cookie 不匹配（g_tk 按域绑定，share.weiyun.com 与 www.weiyun.com 不同）；"
    "③被风控。建议重新 `dlink session import` 导入 share.weiyun.com 域的 cookie。"
)

_GUEST_403_HINT = (
    "游客态下 403 空 body：①先确认请求带了 `Accept: application/json, text/plain, */*`（缺它必 403）；"
    "②若已带仍 403，说明是网关层风控/地域拦截——此时 `https://share.weiyun.com/` 首页仍 200，"
    "但所有 `/webapp/json/*` 端点一律 403 空 body，与信封内容无关。"
    "缓解：用浏览器打开分享页预热 cookie（tgw_l7_route / fingerprint），"
    "或换出口 IP/代理重试；必要时请用手工浏览器会话。"
)


# ---------------------------------------------------------------- g_tk


def calc_g_tk(skey: str) -> int:
    """QQ 系 g_tk 算法。空 skey 返回 0。"""
    h = 5381
    for ch in skey or "":
        h = (h * 33 + ord(ch)) & 0x7FFFFFFF
    return h


# ---------------------------------------------------------------- Session


class WeiyunSession:
    """微云鉴权载体：cookies（至少 skey）+ UA。

    只用 ``requests.Session`` 一层薄封装，方便测试 monkeypatch
    ``requests.Session.post`` / ``requests.Session.request``。
    """

    def __init__(self, cookies: dict[str, str], ua: str = "") -> None:
        self.cookies: dict[str, str] = dict(cookies or {})
        self.ua: str = ua or DEFAULT_UA
        self.http: requests.Session = requests.Session()

    # -- 鉴权 ---------------------------------------------------------

    @property
    def skey(self) -> str:
        return self.cookies.get("skey", "") or ""

    @property
    def g_tk(self) -> int:
        """由 cookies["skey"] 现算；无 skey（含全空白）返回 0（游客 g_tk=0）。"""
        skey = self.skey.strip()
        return calc_g_tk(skey) if skey else 0

    def is_logged_in(self) -> bool:
        return bool(self.skey.strip())

    # -- 工具 ---------------------------------------------------------

    def cookie_jar(self) -> dict[str, str]:
        return dict(self.cookies)

    def __repr__(self) -> str:  # 不要把 cookie 打进日志
        return f"<WeiyunSession logged_in={self.is_logged_in()} g_tk={self.g_tk}>"


# ---------------------------------------------------------------- 平台


class WeiyunPlatform:
    """腾讯微云解析器。`get_share_info` / `list_files` 免登录可用。"""

    name = "weiyun"

    # ------------------------------------------------------------ URL

    _SHARE_HOSTS = ("share.weiyun.com", "www.weiyun.com", "weiyun.com")
    _KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")

    @staticmethod
    def match(url: str) -> bool:
        if not url:
            return False
        u = url.strip()
        # 只认 http(s)（含省略协议的 share.weiyun.com/xxx 写法）
        m = re.match(r"^([a-z][a-z0-9+.-]*)://", u, re.I)
        if m and m.group(1).lower() not in ("http", "https"):
            return False
        host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u.lstrip("/"), flags=re.I).split("/", 1)[0]
        host = host.split("@")[-1].split(":", 1)[0].lower()
        return host in WeiyunPlatform._SHARE_HOSTS or host.endswith(".weiyun.com")

    @staticmethod
    def parse_share_key(url: str) -> str:
        """从各种分享链接形态里抠 share_key。

        支持:
            https://share.weiyun.com/AbCdEf
            https://share.weiyun.com/AbCdEf?pwd=1234
            https://share.weiyun.com/AbCdEf#pwd=1234
            https://share.weiyun.com/AbCdEf?ADTAG=x#/detail   （忽略尾部路由）
            https://www.weiyun.com/share/AbCdEf
            裸 key: AbCdEf...
        """
        if not url:
            raise DLinkError("分享链接为空", hint="传入形如 https://share.weiyun.com/<share_key>")
        u = url.strip()

        # 允许省略协议：share.weiyun.com/<key>；纯裸 key 也接受
        if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I) and not u.startswith("//"):
            if "/" not in u:
                if WeiyunPlatform._KEY_RE.match(u):
                    return u
                raise DLinkError(f"无法识别为微云分享链接：{url!r}")
            u = "https://" + u.lstrip("/")

        body = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u, flags=re.I).lstrip("/")
        path = body.split("/", 1)[1] if "/" in body else ""
        path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
        if not path:
            raise DLinkError(f"微云链接缺少 share_key：{url!r}")

        segs = [s for s in path.split("/") if s]
        # www.weiyun.com/share/<key>
        if len(segs) >= 2 and segs[0].lower() in ("share", "s"):
            segs = segs[1:]
        key = segs[0]
        key = key.split(".", 1)[0] if key.lower().endswith(".html") else key
        if not WeiyunPlatform._KEY_RE.match(key):
            raise DLinkError(
                f"无法从链接解析 share_key：{url!r}",
                hint="期望 https://share.weiyun.com/<share_key>",
            )
        return key

    @staticmethod
    def parse_pwd_from_url(url: str) -> Optional[str]:
        """顺手从 `?pwd=` / `#pwd=` 里取提取码；没给返回 None。"""
        if not url:
            return None
        m = re.search(r"[?&#](?:pwd|extractcode|code)=([^&#]+)", url, re.I)
        return unquote(m.group(1)) if m else None

    # ------------------------------------------------------------ init

    def __init__(self, session: WeiyunSession | None = None) -> None:
        self.session = session or WeiyunSession({})
        # 便于排查：最近一次请求的原始响应
        self.last_response: dict = {}
        # 请求日志（(protocol, name, cmd) → retcode），测试与排障都用得上
        self.call_log: list[tuple[str, str, int]] = []

    # ------------------------------------------------------------ HTTP

    #: 保存最近一次响应的解析结果，便于断言与排障
    last_payload: dict = {}

    def _endpoint_url(self, protocol: str, name: str) -> str:
        r = random.randint(10_000_000, 99_999_999)
        return (
            f"{API_BASE}{protocol}/{name}"
            f"?refer={REFER}&g_tk={self.session.g_tk}&r={r}"
        )

    def _headers(self) -> dict[str, str]:
        # Accept 缺一不可：少了它服务端直接 403 空 body
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=utf-8",
            "Origin": f"https://{SHARE_HOST}",
            "Referer": f"https://{SHARE_HOST}/",
            "User-Agent": self.session.ua,
        }

    def _build_header(self, cmd: int) -> dict:
        return {
            "seq": int(time.time() * 1000) % 1_000_000,
            "type": 1,
            "cmd": cmd,
            "appid": APPID,
            "version": 3,
            "major_version": 3,
            "minor_version": 3,
            "fix_version": 3,
            "wx_openid": "",
            "user_flag": 1,
            "env_id": "",
            "device_info": json.dumps(
                {"platform": "weiyun", "os": "web", "app": "share_link"},
                separators=(",", ":"),
            ),
        }

    def build_envelope(self, name: str, cmd: int, body: dict) -> dict:
        """构造双重 JSON 信封：{"req_header": str, "req_body": str}。"""
        req_body = {
            "ReqMsg_body": {
                "ext_req_head": {
                    "token_info": {"token_type": 3, "login_key_type": 1540},
                    "language_info": {"language_type": 2052},
                },
                BODY_KEY_FMT.format(name): body,
            }
        }
        return {
            "req_header": json.dumps(self._build_header(cmd), ensure_ascii=False),
            "req_body": json.dumps(req_body, ensure_ascii=False),
        }

    def _decode_envelope(self, raw: dict) -> dict:
        """服务端同样回双重 JSON：把 rsp_header / rsp_body 解成 dict。"""
        out: dict[str, Any] = dict(raw or {})
        for k in ("rsp_header", "rsp_body"):
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                try:
                    out[k] = json.loads(v)
                except ValueError:
                    out[k] = {}
        out.setdefault("rsp_header", {})
        out.setdefault("rsp_body", {})
        if not isinstance(out["rsp_header"], dict):
            out["rsp_header"] = {}
        if not isinstance(out["rsp_body"], dict):
            out["rsp_body"] = {}
        return out

    def _fail_kind(self, resp: "requests.Response | None", retcode: int, retmsg: str) -> DLinkError:
        """把 (HTTP, retcode) 组合翻译成带排查提示的异常。"""
        status = getattr(resp, "status_code", 0) or 0
        logged_in = self.session.is_logged_in()

        if 400 <= status < 500 or status == 0:
            base = f"HTTP {status}，body={getattr(resp, 'text', '')!r:.200}"
            if status == 403 and not (getattr(resp, "text", "") or "").strip():
                hint = _SKEY_ERROR_HINT if logged_in else _GUEST_403_HINT
                return DLinkError(f"微云接口 403 空 body（{base}）", hint=hint)
            return DLinkError(f"微云接口请求失败（{base}）", hint="检查网络/代理，或稍后重试")

        if retcode in (RC_NOT_LOGGED_IN,) or 190000 <= retcode < 200000:
            hint = (
                "微云取直链需要登录态：在浏览器打开 https://www.weiyun.com/disk 登录后，"
                "用 `dlink session import` 导入（需含 share.weiyun.com 域的 skey）。"
            )
            if not logged_in:
                hint = "当前无 skey（游客态）。" + hint
            return NotLoggedIn(
                f"微云 retcode={retcode} retmsg={retmsg!r}", hint=hint
            )

        if retcode == RC_BUSY:
            return DLinkError(
                f"微云 retcode=24203（服务器繁忙）retmsg={retmsg!r}",
                hint="24203 多为信封字段不合该接口 schema（cmd/body 键名/必填项不对），"
                "不是真的繁忙；对照 FINDINGS.md §5 检查 req_header.cmd 与 req_body 键名。",
            )

        return DLinkError(
            f"微云返回失败 retcode={retcode} retmsg={retmsg!r}",
            hint="retcode 原文见上；必要 dump 一次原始响应排查（含 rsp_header.retmsg 提示）。",
        )

    def _call(
        self,
        protocol: str,
        name: str,
        cmd: int,
        body: dict,
        *,
        method: str = "POST",
    ) -> dict:
        """发一次请求，返回解码后的信封 dict（rsp_header/rsp_body 已是 dict）。"""
        url = self._endpoint_url(protocol, name)
        headers = self._headers()
        payload = self.build_envelope(name, cmd, body)
        self.call_log.append((protocol, name, cmd))

        resp = None
        try:
            if method.upper() == "GET":
                # View 允许 GET：照样把信封放 query/body，服务端都能吃；这里用 body 保持统一
                resp = self.session.http.get(
                    url, headers=headers, json=payload, timeout=self.timeout, cookies=self.session.cookies
                )
            else:
                resp = self.session.http.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                    cookies=self.session.cookies,
                )
        except requests.RequestException as exc:  # 网络层
            raise DLinkError(f"微云请求异常：{exc}", hint="检查网络/代理后重试") from exc

        text = getattr(resp, "text", "") or ""
        status = getattr(resp, "status_code", 0) or 0
        parsed: dict = {}
        if text.strip():
            try:
                parsed = resp.json()
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}

        if status >= 400 or not parsed:
            # 有 body 但 200 也可能是空对象，一并走失败翻译
            raise self._fail_kind(resp, parsed.get("retcode", -1) if parsed else -1, text[:200])

        data = self._decode_envelope(parsed)
        self.last_payload = data
        hdr = data.get("rsp_header", {})
        retcode = int(hdr.get("retcode", RC_OK) or 0)
        retmsg = hdr.get("retmsg", "") or hdr.get("retmsg_cn", "") or ""
        if retcode != RC_OK:
            err = self._fail_kind(resp, retcode, retmsg)
            err.retcode = retcode  # type: ignore[attr-defined]
            err.retmsg = retmsg  # type: ignore[attr-defined]
            raise err
        return data

    #: HTTP 超时（秒）。测试 monkeypatch 时不影响。
    timeout: float = 20.0

    @staticmethod
    def _rsp_body(data: dict) -> dict:
        """rsp_body.RspMsg_body（注意大小写与信封位置）。"""
        rb = data.get("rsp_body") or {}
        inner = rb.get("RspMsg_body")
        return inner if isinstance(inner, dict) else rb

    # ------------------------------------------------------------ share info

    def get_share_info(self, share_key: str, pwd: str | None = None) -> ShareInfo:
        """免登录读分享元数据。"""
        share_key = self._norm_key(share_key)
        body = {"share_key": share_key}
        if pwd:
            body["share_pwd"] = pwd

        data = self._call(EP_VIEW[0], EP_VIEW[1], EP_VIEW[2], body, method="POST")
        rb = self._rsp_body(data)

        # View 的返回字段各版本有差异，宽松取值
        need_pwd = bool(rb.get("need_pwd") or rb.get("need_pwd_sign"))
        if need_pwd and not pwd:
            raise AuthRequired(
                f"微云分享 {share_key} 需要提取码（need_pwd=true）",
                hint="加 --pwd <提取码>；也支持把 ?pwd=xxxx 直接写在链接里",
            )

        info = ShareInfo(
            platform=self.name,
            share_key=share_key,
            title=str(rb.get("share_name") or rb.get("filename") or ""),
            owner=str(rb.get("share_uin") or rb.get("owner") or ""),
            need_pwd=need_pwd,
            root_key=str(rb.get("pdir_key") or rb.get("dir_key") or ""),
            total_dirs=int(rb.get("total_dir_count") or 0),
            total_files=int(rb.get("total_file_count") or 0),
            extra={"raw": rb},
        )
        return info

    # ------------------------------------------------------------ list

    def _dirlist_page(
        self,
        share_key: str,
        dir_key: str,
        dir_name: str,
        pwd: str | None,
        offset: int,
        num: int = PAGE_SIZE,
    ) -> dict:
        body = {
            "share_key": share_key,
            "share_pwd": pwd or "",
            "dir_key": dir_key,
            "dir_name": dir_name or "",
            "num": num,
            "sort": 0,
        }
        # 兼容式：部分版本用 pwd / offset
        body["pwd"] = pwd or ""
        body["offset"] = offset
        return self._call(EP_DIRLIST[0], EP_DIRLIST[1], EP_DIRLIST[2], body)

    @staticmethod
    def _iter_entries(rb: dict) -> Iterable[tuple[str, dict]]:
        """遍历一页里的 (kind, item)，kind ∈ {"dir","file"}。"""
        # 目录可能叫 dir_list / dirlist；文件叫 file_list / filelist
        for key in ("dir_list", "dirlist", "dirs"):
            val = rb.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        yield "dir", it
        for key in ("file_list", "filelist", "files"):
            val = rb.get(key)
            if isinstance(val, list):
                for it in val:
                    if isinstance(it, dict):
                        yield "file", it

    @staticmethod
    def _to_remote_file(it: dict, parent_key: str) -> RemoteFile:
        fid = str(it.get("file_id") or it.get("_id") or it.get("id") or "")
        name = str(it.get("filename") or it.get("file_name") or it.get("_name") or "")
        size = it.get("file_size", it.get("_file_size", 0))
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            size = 0
        return RemoteFile(
            file_id=fid,
            name=name,
            size=size,
            parent_key=str(it.get("pdir_key") or parent_key or ""),
            sha1=str(it.get("file_sha") or it.get("sha1") or ""),
            md5=str(it.get("file_md5") or it.get("md5") or ""),
            extra={
                "dir_key": str(it.get("dir_key") or it.get("pdir_key") or ""),
                "cid": it.get("cid"),
                "raw": it,
            },
        )

    def list_files(
        self,
        share_key: str,
        root_key: str,
        pwd: str | None = None,
        recursive: bool = True,
    ) -> list[RemoteFile]:
        """列文件。免登录。递归时做深度保护(MAX_DEPTH)与 dir_key 去重（防目录环）。"""
        share_key = self._norm_key(share_key)
        root_key = root_key or ""

        files: list[RemoteFile] = []
        visited: set[str] = set()

        def walk(dir_key: str, dir_name: str, depth: int) -> None:
            if depth > MAX_DEPTH:
                raise DLinkError(
                    f"目录层级超过 {MAX_DEPTH} 层，疑似目录环（dir_key={dir_key}）",
                    hint="检查分享结构；若确为深层嵌套，调大 weiyun.MAX_DEPTH",
                )
            if dir_key in visited:
                return
            visited.add(dir_key)

            seen_ids: set[str] = set()
            offset = 0
            while True:
                data = self._dirlist_page(share_key, dir_key, dir_name, pwd, offset)
                rb = self._rsp_body(data)

                need_pwd = bool(rb.get("need_pwd"))
                if need_pwd and not pwd:
                    raise AuthRequired(
                        f"微云分享 {share_key} 需要提取码（DirList need_pwd=true）",
                        hint="加 --pwd <提取码>；也支持把 ?pwd=xxxx 直接写在链接里",
                    )

                page_count = 0
                for kind, it in self._iter_entries(rb):
                    page_count += 1
                    if kind == "file":
                        f = self._to_remote_file(it, dir_key)
                        if f.file_id and f.file_id in seen_ids:
                            continue
                        seen_ids.add(f.file_id)
                        files.append(f)
                    else:  # dir
                        child_key = str(it.get("dir_key") or it.get("_dir_key") or "")
                        child_name = str(it.get("dir_name") or it.get("_name") or "")
                        if recursive and child_key:
                            walk(child_key, child_name, depth + 1)

                # 分页：服务端没给明确的"还有更多"时，本页满 num 才继续
                has_more = bool(rb.get("has_more"))
                if not has_more and page_count < PAGE_SIZE:
                    break
                if page_count == 0:
                    break
                offset += page_count
                if offset > 100_000:  # 兜底，别无限滚
                    break

        walk(root_key, "", 0)
        return files

    # ------------------------------------------------------------ direct link

    def get_direct_link(
        self, share_key: str, f: RemoteFile, pwd: str | None = None
    ) -> DirectLink:
        """取直链。

        顺序（依 FINDINGS.md §3 的修正结论）：
        1. 有 skey → 先打权威路径 ``weiyunShare/WeiyunShareBatchDownload``；
        2. 权威路径给 190011 / 403 → 降级尝试 ``weiyunShareNoLogin/...``；
        3. 都失败 → 抛 NotLoggedIn，hint 提示去浏览器登录微云。
        无 skey 时同样先试权威路径（拿到明确 retcode 便于诊断），再降级。
        """
        share_key = self._norm_key(share_key)
        body = self._dl_body(share_key, f, pwd)

        attempts: list[tuple[str, str, int]] = [EP_DL_AUTH, EP_DL_NOLOGIN]
        errors: list[str] = []
        got_not_logged_in = False

        for protocol, name, cmd in attempts:
            try:
                data = self._call(protocol, name, cmd, body)
                return self._link_from_response(data, f, protocol=protocol)
            except NotLoggedIn as exc:
                got_not_logged_in = True
                errors.append(f"{protocol}/{name}: {exc.message}")
                continue
            except DLinkError as exc:
                # 403 空 body 也降级试一次 NoLogin；其它错误（如 24203）照样继续试一遍
                errors.append(f"{protocol}/{name}: {exc.message}")
                continue

        detail = " | ".join(errors) or "无响应"
        hint = (
            "微云取直链需要登录态：在浏览器打开 https://www.weiyun.com/disk 登录，"
            "再用 `dlink session import` 导入 cookie（必须含 share.weiyun.com 的 skey）。"
        )
        if not self.session.is_logged_in():
            hint = "当前会话没有 skey（游客态）。" + hint
        if not got_not_logged_in:
            hint += " 另：若为 403 空 body，先确认请求带了 Accept 头，且 g_tk 与 skey 同域匹配。"
        raise NotLoggedIn(f"微云两条取直链路径均失败：{detail}", hint=hint)

    def _dl_body(self, share_key: str, f: RemoteFile, pwd: str | None) -> dict:
        return {
            "share_key": share_key,
            "pwd": pwd or "",
            "file_owner": None,
            "download_type": 0,
            "file_list": [
                {
                    "pdir_key": f.parent_key or "",
                    "file_id": f.file_id,
                    "filename": f.name,
                    "file_size": int(f.size or 0),
                }
            ],
        }

    def _link_from_response(
        self, data: dict, f: RemoteFile, *, protocol: str
    ) -> DirectLink:
        rb = self._rsp_body(data)
        fl = rb.get("file_list") or rb.get("filelist") or []
        if not isinstance(fl, list) or not fl:
            raise DLinkError(
                f"微云 BatchDownload 未返回 file_list（rsp_body 键={list(rb.keys())}）",
                hint="该端点可能未授权此分享；确认登录态或换权威路径重试。",
            )
        item = fl[0] if isinstance(fl[0], dict) else {}

        url = str(item.get("https_download_url") or item.get("download_url") or "")
        if not url:
            rc = (data.get("rsp_header") or {}).get("retcode")
            raise LinkExpired(
                f"微云未返回下载地址（retcode={rc}，file={f.name}）",
                hint="直链申请可能被风控或文件已失效；重试或重新登录后导入 cookie。",
            )

        cookie_name = str(item.get("cookie_name") or "FTN5K")
        cookie_value = str(item.get("cookie_value") or "")
        cookies = {cookie_name: cookie_value} if cookie_value else {}

        try:
            size = int(item.get("file_size") or f.size or 0)
        except (TypeError, ValueError):
            size = f.size
        sha1 = str(item.get("file_sha") or f.sha1 or "").lower()

        return DirectLink(
            url=url,
            cookies=cookies,
            headers={
                "User-Agent": self.session.ua,
                "Referer": f"https://{SHARE_HOST}/",
                # 下载侧同样别丢 Accept
                "Accept": "*/*",
            },
            size=size,
            sha1=sha1,
            md5=str(item.get("file_md5") or f.md5 or "").lower(),
            filename=str(item.get("filename") or f.name or ""),
            extra={"protocol": protocol, "raw": item},
        )

    # ------------------------------------------------------------ util

    @staticmethod
    def _norm_key(share_key: str) -> str:
        """允许直接传整条链接。"""
        k = (share_key or "").strip()
        if not k:
            raise DLinkError("share_key 为空")
        if "/" in k or k.lower().startswith("http"):
            k = WeiyunPlatform.parse_share_key(k)
        return k
