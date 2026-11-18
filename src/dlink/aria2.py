"""解析 aria2c 命令行，转成 DLink 的任务条目。

为什么需要：
    OpenAI/百度/微云这些地方的“直链”往往是别人直接给一条 aria2c 命令：
        aria2c "https://d.pcs.baidu.com/file/xxx?..." --out "PC.7z" \
               --header "User-Agent:pan.baidu.com"
    用户不会手动拆 URL / UA / Referer，所以软件要能直接吃这条命令。

设计取舍：
    · 只认**安全的**选项（out/dir/header/referer/user-agent/x/split），
      其它选项一律忽略 —— 不执行、不联网、不读文件（`--input-file` 之类直接跳过）。
    · 未知的 `--foo` 当成开关，不吞后面的 token，避免把 URL 吃掉。
    · 一条文本可含多条命令（每行一个 aria2c）。
    · Cookie 头单独拆成 cookies 字典（下载器分开发，避免和用户自定义头冲突）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

#: 需要“吃掉下一个 token”的选项 -> 归一化名字
_VALUE_OPTS = {
    "-o": "out", "--out": "out",
    "-d": "dir", "--dir": "dir",
    "-H": "header", "--header": "header",
    "-U": "user-agent", "--user-agent": "user-agent",
    "-x": "max-conn", "--max-connection-per-server": "max-conn",
    "-s": "split", "--split": "split",
    "-t": "timeout", "--timeout": "timeout",
    "--referer": "referer",
    "--load-cookies": "ignore-file",
    "--save-cookies": "ignore-file",
    "--input-file": "ignore-file",
    "-i": "ignore-file",
    "-j": "ignore-num", "--max-concurrent-downloads": "ignore-num",
    "--retry-wait": "ignore-num",
    "-m": "ignore-num", "--max-tries": "ignore-num",
}

#: 明确忽略的开关（不吞值）
_FLAG_OPTS = {
    "-c", "--continue", "true", "-q", "--quiet", "true",
    "--check-certificate", "false", "--no-check-certificate", "true",
    "--allow-overwrite", "true", "-V", "true", "--version", "true",
}

_URL_RE = re.compile(r"^https?://", re.I)
_CMD_RE = re.compile(r"(?im)^[ \t]*aria2c(?:\.exe)?\b")


@dataclass
class Aria2Item:
    """一条 aria2c 命令解析出的下载条目。"""

    url: str
    filename: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    workers: int | None = None
    #: 命令里给的 --dir（目前只用来提示，不参与落盘路径）
    dir_hint: str = ""

    def to_dict(self) -> dict:
        out: dict = {"url": self.url}
        if self.filename:
            out["filename"] = self.filename
        if self.headers:
            out["headers"] = dict(self.headers)
        if self.cookies:
            out["cookies"] = dict(self.cookies)
        if self.workers:
            out["workers"] = int(self.workers)
        return out


def looks_like_aria2(text: str) -> bool:
    """粘进来的东西像不像 aria2c 命令（带选项或显式命令名）。"""
    if not text:
        return False
    if _CMD_RE.search(text):
        return True
    low = text.lower()
    return any(k in low for k in ("--out ", "--out=", "--header ", "--header=",
                                  " -o ", " -h ", "--referer", "-x "))


def _split_args(cmd: str) -> list[str]:
    """按空白切分，尊重单双引号；保留引号内的空格与反斜杠。"""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        elif ch == "\\" and i + 1 < n and cmd[i + 1] in ("\n", "\r"):
            # 反斜杠续行
            i += 1
        else:
            buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _norm_header(raw: str) -> tuple[str, str] | None:
    """`User-Agent: pan.baidu.com` -> ("User-Agent", "pan.baidu.com")。"""
    if ":" not in raw:
        return None
    name, _, value = raw.partition(":")
    name, value = name.strip(), value.strip()
    if not name:
        return None
    return name, value


def _parse_cookie_header(value: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            jar[k] = v
    return jar


def _basename(path: str) -> str:
    p = path.replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] if p else ""


def _name_from_url(url: str) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return "download.bin"
    name = unquote(_basename(p.path)) or ""
    return name or "download.bin"


def _parse_one(cmd: str) -> list[Aria2Item]:
    args = _split_args(cmd)
    if args and args[0].lower().startswith("aria2c"):
        args = args[1:]

    out_name = ""
    dir_hint = ""
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    workers: int | None = None
    split: int | None = None
    urls: list[str] = []

    i = 0
    while i < len(args):
        tok = args[i]
        key = ""
        val: str | None = None
        if tok.startswith("-"):
            if "=" in tok:
                opt, _, inline = tok.partition("=")
                key = _VALUE_OPTS.get(opt, "")
                if key:
                    val = inline
            else:
                key = _VALUE_OPTS.get(tok, "")
                if key and i + 1 < len(args):
                    val = args[i + 1]
                    i += 1
                elif tok in _FLAG_OPTS:
                    key = ""
            i += 1
            if not key or val is None:
                continue
            if key == "out":
                out_name = val
            elif key == "dir":
                dir_hint = val
            elif key == "header":
                kv = _norm_header(val)
                if kv:
                    name, v = kv
                    if name.lower() == "cookie":
                        cookies.update(_parse_cookie_header(v))
                    else:
                        headers[name] = v
            elif key == "referer":
                headers.setdefault("Referer", val)
            elif key == "user-agent":
                headers.setdefault("User-Agent", val)
            elif key == "max-conn":
                try:
                    workers = max(1, min(16, int(val)))
                except ValueError:
                    pass
            elif key == "split":
                try:
                    split = max(1, min(16, int(val)))
                except ValueError:
                    pass
            continue
        if _URL_RE.match(tok):
            urls.append(tok)
        i += 1

    if workers is None and split is not None:
        workers = split

    items: list[Aria2Item] = []
    for url in urls:
        name = out_name or _name_from_url(url)
        # --out 可以带目录，只取文件名（落盘目录由设置决定）
        name = _basename(name) or _name_from_url(url)
        items.append(Aria2Item(
            url=url,
            filename=name,
            headers=dict(headers),
            cookies=dict(cookies),
            workers=workers,
            dir_hint=dir_hint,
        ))
    return items


def parse_aria2(text: str) -> list[Aria2Item]:
    """把一段文本解析成若干条目。

    文本可以是：
        · 单条 aria2c 命令（可含反斜杠续行）
        · 多条 aria2c 命令（每行一条）
        · 若干裸 URL（此时 headers 为空，filename 从 URL 推）
    """
    if not text or not text.strip():
        return []

    raw = text.strip()
    chunks = []
    if _CMD_RE.search(raw):
        # 按 `aria2c` 切；每段补回命令行前缀
        pos = [m.start() for m in _CMD_RE.finditer(raw)]
        for idx, start in enumerate(pos):
            end = pos[idx + 1] if idx + 1 < len(pos) else len(raw)
            chunks.append(raw[start:end].strip())
    else:
        # 没有命令名：按行切，多行 = 多条
        chunks = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    items: list[Aria2Item] = []
    for ch in chunks:
        if ch.lower().startswith("aria2c") or not _URL_RE.match(ch):
            items.extend(_parse_one(ch))
        else:
            for tok in _split_args(ch):
                if _URL_RE.match(tok):
                    items.append(Aria2Item(url=tok, filename=_name_from_url(tok)))
    return items
