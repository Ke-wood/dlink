"""网盘返回的哈希字段清洗。

为什么要单独做这件事：
    百度 API 返回的 `md5` 字段是**内部格式**，不是标准 MD5。
    实测：sample.bin 的 API md5 = `a1b2c3d4e5o6f7a8b9c0d1e2f3a4b5c6`
          （第 10 位是字母 `o`，不是十六进制）
          而真实文件 MD5  = `e3b0c44298fc1c149afbf4c8996fb924`
          （响应头 Content-MD5 也是这个）

    如果直接拿 API 的 md5 去校验，必然 IntegrityError，白下一遍。

规则：
    · 标准 MD5/SHA1 只允许 [0-9a-f]，长度分别 32 / 40
    · 不合法就**丢弃**（返回空串），让下载器退回用 size + 魔数校验
    · 宁可不校验，也不能用错的期望值把好文件判成坏文件
"""

from __future__ import annotations

import re

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def clean_md5(value) -> str:
    """清洗 MD5。不合法返回空串。"""
    if not value:
        return ""
    s = str(value).strip().lower()
    return s if _HEX32.match(s) else ""


def clean_sha1(value) -> str:
    """清洗 SHA1。不合法返回空串。"""
    if not value:
        return ""
    s = str(value).strip().lower()
    return s if _HEX40.match(s) else ""


def clean_sha256(value) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    return s if _HEX64.match(s) else ""


def clean_hash(value, kind: str = "md5") -> str:
    kind = (kind or "").lower()
    if kind == "md5":
        return clean_md5(value)
    if kind == "sha1":
        return clean_sha1(value)
    if kind == "sha256":
        return clean_sha256(value)
    # 未知类型：按长度猜
    s = str(value or "").strip().lower()
    for f in (_HEX32, _HEX40, _HEX64):
        if f.match(s):
            return s
    return ""
