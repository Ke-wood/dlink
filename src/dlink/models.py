"""核心数据模型：平台无关。

设计原则（交接文档 §5）：resolver / api 每平台一份，downloader 全平台共用。
所以模型层必须只描述「下载一个文件需要什么」，不含任何平台字段名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class DLinkError(Exception):
    """工具内所有可预期错误的基类，CLI 统一捕获并给出可读提示。"""

    code = "E_UNKNOWN"
    hint = ""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint

    def __str__(self) -> str:
        base = f"[{self.code}] {self.message}"
        return f"{base}\n  → {self.hint}" if self.hint else base


class NotLoggedIn(DLinkError):
    code = "E_NOT_LOGGED_IN"
    hint = "在浏览器里登录网盘账号后重新执行 `dlink session import`"


class LinkExpired(DLinkError):
    code = "E_LINK_EXPIRED"
    hint = "直链有时效，请重新执行 resolve 获取新链接"


class AuthRequired(DLinkError):
    code = "E_AUTH_REQUIRED"
    hint = "该分享需要提取码：加 --pwd <提取码>"


class IntegrityError(DLinkError):
    code = "E_INTEGRITY"
    hint = "校验失败的文件已保留，可用 `dlink verify` 复核"


@dataclass
class RemoteFile:
    """分享里的一个文件（叶子节点）。"""

    file_id: str
    name: str
    size: int
    parent_key: str = ""
    sha1: str = ""
    md5: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sha1 = (self.sha1 or "").lower()
        self.md5 = (self.md5 or "").lower()

    def __repr__(self) -> str:
        return f"<RemoteFile {self.name} {self.size}B>"


@dataclass
class ShareInfo:
    """分享的顶层元信息。"""

    platform: str
    share_key: str
    title: str = ""
    owner: str = ""
    need_pwd: bool = False
    root_key: str = ""
    total_dirs: int = 0
    total_files: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class DirectLink:
    """可直接下载的直链 + 配套鉴权。"""

    url: str
    # 下载时必须携带的 cookie，形如 {"FTN5K": "4b29f45c"}
    cookies: dict = field(default_factory=dict)
    # 非 cookie 类鉴权（UA / Referer / 自定义 header）
    headers: dict = field(default_factory=dict)
    size: int = 0
    sha1: str = ""
    md5: str = ""
    # 平台给的文件名，用于落盘命名
    filename: str = ""
    # 时效（如果平台明确给了）；None 表示未知，按「立刻下载」处理
    expires_at: Optional[float] = None
    extra: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"<DirectLink {self.filename} {self.size}B>"


@dataclass
class ResolveResult:
    """一次 resolve 的完整结果，可直接 JSON 序列化落盘。"""

    share: ShareInfo
    files: list[RemoteFile]
    links: list[DirectLink] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "share": {
                "platform": self.share.platform,
                "share_key": self.share.share_key,
                "title": self.share.title,
                "owner": self.share.owner,
                "need_pwd": self.share.need_pwd,
                "total_dirs": self.share.total_dirs,
                "total_files": self.share.total_files,
            },
            "files": [
                {
                    "file_id": f.file_id,
                    "name": f.name,
                    "size": f.size,
                    "parent_key": f.parent_key,
                    "sha1": f.sha1,
                    "md5": f.md5,
                }
                for f in self.files
            ],
            "links": [
                {
                    "url": l.url,
                    "cookies": l.cookies,
                    "headers": l.headers,
                    "size": l.size,
                    "sha1": l.sha1,
                    "md5": l.md5,
                    "filename": l.filename,
                }
                for l in self.links
            ],
        }
