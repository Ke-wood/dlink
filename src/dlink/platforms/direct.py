"""普通 HTTP/HTTPS 直链适配器。

用户需求：**能抓常规下载链接，用并行加速**。
这是「通用下载器」能力，也是与网盘并列的一等公民。

核心工作：探测服务端能力（大小 / 是否支持 Range / 文件名），
然后把结果包装成 DirectLink 交给通用下载器。

关键判定：服务端**是否真的支持 Range**。
- 很多服务器声称 Accept-Ranges: bytes 但实际返回 200 全量
- 也有服务器不声明但实际支持 206
→ 所以以**实际 Range 请求的响应**为准，不以头部声明为准。
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlparse

import requests

from ..models import DirectLink, DLinkError, RemoteFile

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class DirectError(DLinkError):
    code = "E_DIRECT"


def filename_from_url(url: str) -> str:
    """从 URL 推断文件名。"""
    path = urlparse(url).path
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if not name:
        return "download.bin"
    # 去掉明显的查询串残留
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def filename_from_disposition(header: str) -> Optional[str]:
    """从 Content-Disposition 解析文件名。支持 RFC 5987 的 filename*。"""
    if not header:
        return None
    # filename*=UTF-8''xxx
    m = re.search(r"filename\*\s*=\s*([^;]+)", header, re.I)
    if m:
        v = m.group(1).strip().strip('"')
        if "''" in v:
            v = v.split("''", 1)[1]
        try:
            return unquote(v)
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', header, re.I)
    if m:
        return m.group(1).strip()
    return None


class DirectAdapter:
    """普通直链适配器。match() 基本上对所有 http(s) 返回 True（兜底）。"""

    name = "direct"
    display_name = "普通链接"

    @staticmethod
    def match(url: str) -> bool:
        return url.startswith(("http://", "https://"))

    @staticmethod
    def parse_id(url: str) -> str:
        # 直链没有"ID"概念，用 URL 本身
        return url

    # ---------- 探测 ----------

    def probe(self, url: str, timeout: float = 20.0) -> dict:
        """探测服务端能力。

        先 HEAD（快），失败或信息不全则用 Range GET 探测。
        HEAD 被很多站禁（405/403），所以要能降级。
        """
        info = {
            "size": 0,
            "supports_range": False,
            "filename": filename_from_url(url),
            "content_type": "",
            "final_url": url,
            "note": "",
        }

        headers = {"User-Agent": UA, "Accept": "*/*"}

        # 1) HEAD
        head_ok = False
        try:
            r = requests.head(url, headers=headers, timeout=timeout,
                              allow_redirects=True)
            if r.status_code in (200, 301, 302, 303, 307, 308):
                head_ok = True
                info["final_url"] = r.url
                cl = r.headers.get("content-length")
                if cl and cl.isdigit():
                    info["size"] = int(cl)
                info["content_type"] = r.headers.get("content-type", "")
                fn = filename_from_disposition(r.headers.get("content-disposition", ""))
                if fn:
                    info["filename"] = fn
                # HEAD 无法确认 Range，先记下声明
                if (r.headers.get("accept-ranges", "").lower() == "bytes"):
                    info["supports_range"] = True
        except Exception as e:
            info["note"] = f"HEAD 失败（{type(e).__name__}），改用 Range 探测"

        # 2) Range GET 实探（以实际行为为准）
        try:
            r = requests.get(url, headers={**headers, "Range": "bytes=0-1023"},
                             timeout=timeout, stream=True, allow_redirects=True)
            st = r.status_code
            cr = r.headers.get("content-range", "")
            body = r.raw.read(1024, decode_content=True)
            r.close()

            if st == 206 and cr:
                info["supports_range"] = True
                m = re.search(r"/(\d+)$", cr)
                if m:
                    info["size"] = int(m.group(1))
            elif st == 200:
                # 忽略 Range，返回全量 → 不支持分段
                info["supports_range"] = False
                cl = r.headers.get("content-length")
                if cl and cl.isdigit():
                    info["size"] = int(cl)
                if not info["note"]:
                    info["note"] = "服务器忽略 Range（返回 200 全量），将使用单连接"
            elif st in (401, 403):
                raise DirectError(
                    f"服务器拒绝访问（HTTP {st}）",
                    hint="可能需要登录或 Referer；可尝试用浏览器打开确认")
            else:
                if not info["note"]:
                    info["note"] = f"Range 探测返回 HTTP {st}"

            info["content_type"] = info["content_type"] or r.headers.get("content-type", "")
            fn = filename_from_disposition(r.headers.get("content-disposition", ""))
            if fn:
                info["filename"] = fn
        except DirectError:
            raise
        except Exception as e:
            if not info["note"]:
                info["note"] = f"Range 探测失败（{type(e).__name__}）"

        if not head_ok and info["size"] == 0:
            info["note"] = (info["note"] + "；大小未知").strip("；")

        return info

    # ---------- 平台接口 ----------

    def resolve(self, url: str, referer: str = "", cookie: str = "",
                filename: str = "", **_) -> list[RemoteFile]:
        """直链不需要"列目录"，返回单文件。"""
        return [RemoteFile(
            file_id=url, name=filename or filename_from_url(url), size=0,
            extra={"url": url, "referer": referer, "cookie": cookie},
        )]

    def get_direct_link(self, url: str, f: RemoteFile | None = None,
                        referer: str = "", cookie: str = "",
                        filename: str = "") -> DirectLink:
        """探测并返回可直接下载的凭证。"""
        info = self.probe(url)
        headers = {"User-Agent": UA, "Referer": referer or info["final_url"]}
        cookies = {}
        if cookie:
            for kv in cookie.split(";"):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    cookies[k.strip()] = v.strip()

        return DirectLink(
            url=info["final_url"],
            cookies=cookies,
            headers=headers,
            size=info["size"],
            filename=filename or info["filename"],
            expires_at=None,
            extra={
                "supports_range": info["supports_range"],
                "content_type": info["content_type"],
                "note": info["note"],
            },
        )

    # ---------- 策略 ----------

    def recommended_workers(self) -> int:
        """普通 HTTP 默认 8 路：多数服务器不限速，并行能吃满带宽。"""
        return 8
