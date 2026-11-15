"""全平台通用的分段并行下载器。

设计动机（见 docs/FINDINGS.md §7）：腾讯微云直链限的是「连接年龄」——单连接
前 ~14MB 约 10MB/s，之后衰减到 ~240B/s 并断流；而新连接冷启动仍有 ~9.7MB/s。
所以这里把文件按固定段长切成若干段，**每段一个独立的 HTTP Range 请求、独立的
Session**，多段并发；段内按已落盘字节数续传。

关键实现点（都是踩过的坑，改之前请先读）：
- 每段独立 ``requests.Session``：共享连接池会让所有段复用同一条 TCP 连接，
  等于退回单连接速度。
- 段文件放 ``dest + ".parts/"``，重跑按文件大小续传，只对剩余区间发 Range。
- 服务端若**忽略 Range 返回 200 全量**，必须先截断段文件再写，否则数据错位、
  校验必失败。
- 401/403 抛 ``LinkExpired``（不重试）；4xx（除 416/429）致命。
- 校验不符抛 ``IntegrityError``，但**保留**已合并的文件供用户复核。
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import requests
import urllib3

from .models import DLinkError, DirectLink, IntegrityError, LinkExpired

__all__ = [
    "download",
    "sniff_magic",
    "plan_segments",
    "parse_content_range",
    "BadResponseError",
    "DEFAULT_UA",
    "MAX_SEGMENTS",
    "EXPECTED_MAGIC",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_SEG_SIZE = 32 * 1024 * 1024
MAX_SEGMENTS = 256
CHUNK = 256 * 1024

# 进度回调节流：时间或字节增量任一达标即回调
PROGRESS_INTERVAL_SEC = 0.5
PROGRESS_INTERVAL_BYTES = 4 * 1024 * 1024

# 退避：1, 2, 4, 8, 16, 30, 30, ...
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

# 退避等待函数。测试可以把它替换掉，避免真的睡 1s/2s/4s。
_sleep: Callable[[float], None] = time.sleep

# 这些 4xx 值得重试：416 表示段已越界/已完整；429 是限流，退避后可能恢复
RETRYABLE_4XX = (416, 429)
# 这些状态码不重试，直接判定链接失效
EXPIRED_STATUS = (401, 403)


class BadResponseError(DLinkError):
    """服务端返回了无法处理的响应（例如分片响应里 Content-Range 起止对不上）。

    属于模型层没有定义的错误类型，所以在这里继承 ``DLinkError``；
    如果想把它并入 models，挪过去即可，用法不变。
    """

    code = "E_BAD_RESPONSE"
    hint = "服务端 Range 响应异常；可尝试重新 resolve 换新直链"


# 扩展名 → 期望魔数。同名常量在 sniff_magic 里复用。
EXPECTED_MAGIC: dict[str, tuple[str, ...]] = {
    ".zlib": ("zlib",),
    ".gz": ("gzip",),
    ".gzip": ("gzip",),
    ".tgz": ("gzip",),
    ".rar": ("rar", "rar5"),
    ".rev": ("rar", "rar5"),
    ".7z": ("7z",),
    ".zip": ("zip",),
    ".png": ("png",),
    ".jpg": ("jpeg",),
    ".jpeg": ("jpeg",),
    ".jpe": ("jpeg",),
    ".pdf": ("pdf",),
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _backoff(attempt: int) -> float:
    """指数退避：1s, 2s, 4s, 8s, 16s, 30s, 30s...（attempt 从 0 开始）。"""
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** min(attempt, 16)))


def parse_content_range(value: str) -> Optional[tuple[int, int, Optional[int]]]:
    """解析 ``Content-Range: bytes 100-199/1234`` → ``(100, 199, 1234)``。

    总长可能是 ``*``，此时第三项为 None。格式不认识返回 None。
    """
    if not value:
        return None
    text = value.strip()
    if text.lower().startswith("bytes"):
        text = text[5:].strip()
    if text.startswith("*/"):
        return None  # 416 的 ``bytes */1234``
    try:
        span, _, total = text.partition("/")
        first, _, last = span.partition("-")
        start = int(first.strip())
        end = int(last.strip())
        size = int(total.strip()) if total.strip() not in ("", "*") else None
        if end < start:
            return None
        return start, end, size
    except (TypeError, ValueError):
        return None


def plan_segments(size: int, segments: Optional[int] = None,
                  seg_size: int = DEFAULT_SEG_SIZE) -> list[tuple[int, int]]:
    """把 ``[0, size)`` 切成若干 ``(start, end)``（闭区间，含两端）。

    - ``segments`` 给定则按它切；否则按 ``seg_size`` 切，段数上限 ``MAX_SEGMENTS``。
    - 段数不会超过 size 个字节；size 为 0 时返回空列表。
    """
    if size <= 0:
        return []
    size = int(size)

    if segments is not None:
        n = max(1, min(int(segments), size, MAX_SEGMENTS))
    else:
        if seg_size is None or seg_size <= 0:
            seg_size = DEFAULT_SEG_SIZE
        n = max(1, -(-size // int(seg_size)))  # ceil
        n = min(n, MAX_SEGMENTS, size)

    # 均分：多出来的余数摊到前 n 段，避免最后一段特别大/特别小
    base, extra = divmod(size, n)
    out: list[tuple[int, int]] = []
    cursor = 0
    for i in range(n):
        length = base + (1 if i < extra else 0)
        if length <= 0:
            continue
        out.append((cursor, cursor + length - 1))
        cursor += length
    return out


def sniff_magic(path: str) -> str:
    """读文件头识别魔数。

    返回 ``zlib`` / ``gzip`` / ``rar`` / ``rar5`` / ``7z`` / ``zip`` /
    ``png`` / ``jpeg`` / ``pdf``，未知返回 ``""``。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""

    # 顺序有讲究：rar5 必须先于 rar 判，否则 ``Rar!\x1a\x07`` 会被 rar 抢先匹配
    if head.startswith(b"Rar!\x1a\x07\x01\x00"):
        return "rar5"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") \
            or head.startswith(b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"\x78\x01") or head.startswith(b"\x78\x9c") \
            or head.startswith(b"\x78\xda"):
        return "zlib"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"%PDF"):
        return "pdf"
    return ""


def _magic_warning(magic: str, filename: str) -> Optional[str]:
    """魔数与（非空的）扩展名不一致时给一句提示；一致或无法判断返回 None。"""
    if not magic or not filename:
        return None
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return None
    expected = EXPECTED_MAGIC.get(ext)
    if not expected:
        return None  # 不认识的扩展名，不猜
    if magic in expected:
        return None
    return (
        f"魔数嗅探为 {magic!r}，与文件名扩展名 {ext!r} 不符"
        f"（期望 {expected[0]!r}）；文件可能被平台包装/加密，或文件名不可信"
    )


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024.0
    return f"{n:.2f}TB"


def part_path(parts_dir: str, index: int) -> str:
    return os.path.join(parts_dir, f"part_{index:03d}")


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --------------------------------------------------------------------------
# 进度汇报（节流）
# --------------------------------------------------------------------------


class _Progress:
    """累加各线程的字节增量，并按时间/字节双阈值节流地回调 ``progress``。"""

    def __init__(self, total: int, callback: Optional[Callable[[int, int], None]]):
        self.total = total
        self.callback = callback
        self.done = 0
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._last_bytes = 0

    def add(self, n: int) -> None:
        """累加字节数；``n`` 可以为负（段被截断重写时回退）。"""
        if not n:
            return
        with self._lock:
            self.done = max(0, self.done + n)
            done = self.done
        if self.callback is None:
            return
        now = time.monotonic()
        if (
            done - self._last_bytes >= PROGRESS_INTERVAL_BYTES
            or now - self._last_call >= PROGRESS_INTERVAL_SEC
        ):
            self._last_call = now
            self._last_bytes = done
            self._emit(done)

    def finish(self) -> None:
        """收尾时无条件回调一次，保证 CLI 能看到 100%。"""
        if self.callback is None:
            return
        with self._lock:
            done = self.done
        self._emit(done)

    def _emit(self, done: int) -> None:
        try:
            self.callback(done, self.total)
        except Exception:  # noqa: BLE001 —— 进度回调出错不能影响下载
            pass

    def reset_part(self, lost: int) -> None:
        """某段被从头重写，把该段先前计入的 ``lost`` 字节扣回去。"""
        if lost > 0:
            self.add(-lost)


# --------------------------------------------------------------------------
# 下载核心
# --------------------------------------------------------------------------


@dataclass
class _Seg:
    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _build_headers(link: DirectLink, start: int, end: int) -> dict:
    """按任务书第 8 条拼装请求头，Range 由调用方按需注入。"""
    headers: dict[str, str] = {}
    # link.headers 全部照加（后加的不覆盖已有关键区分大小写的原始键，用 dict 天然合并）
    for key, value in (link.headers or {}).items():
        if value is not None:
            headers[str(key)] = str(value)

    # UA：除非调用方显式指定，否则用浏览器 UA
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = DEFAULT_UA

    # Referer：link.headers 里有就用（上面已经合过），这里只补默认值
    if not any(k.lower() == "referer" for k in headers):
        headers["Referer"] = "https://www.weiyun.com/"

    # cookies 拼成 Cookie 头（若 headers 里已手写 Cookie，则合并而不是覆盖）
    if link.cookies:
        jar = "; ".join(f"{k}={v}" for k, v in link.cookies.items())
        existing_key = next((k for k in headers if k.lower() == "cookie"), None)
        if existing_key:
            headers[existing_key] = f"{headers[existing_key]}; {jar}"
        else:
            headers["Cookie"] = jar

    headers["Range"] = f"bytes={start}-{end}"
    headers.setdefault("Accept", "*/*")
    headers["Accept-Encoding"] = "identity"  # 要字节精确，不要压缩
    return headers


def _check_status(resp: requests.Response, link: DirectLink, seg: _Seg) -> None:
    """401/403 → LinkExpired；其余交给调用方按可重试/致命分类。"""
    if resp.status_code in EXPIRED_STATUS:
        raise LinkExpired(
            f"下载直链返回 HTTP {resp.status_code}（段 {seg.index}），链接已失效或鉴权缺失",
            hint="直链有时效，请重新执行 resolve 获取新链接"
            "（若是 401，多半是缺 FTN5K 之类的 cookie，重新 resolve 会带上）",
        )


def _partial_offset(resp: requests.Response, seg: _Seg) -> int:
    """从 206 响应里推出「这次响应从段的第几个字节开始」。

    正常应与请求的 offset 一致；少数服务器会把区间夹到文件尾部，所以这里按
    Content-Range 的实际起点对齐，并要求起点落在段内。
    """
    parsed = parse_content_range(resp.headers.get("Content-Range", ""))
    if parsed is None:
        return -1
    first, _, _ = parsed
    return first - seg.start


def _download_segment(
    link: DirectLink,
    seg: _Seg,
    path: str,
    *,
    retries: int,
    timeout: float,
    progress: _Progress,
    session: requests.Session,
    task: "_Task",
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """把一段下载到 ``path``，段内按已落盘字节续传。成功返回，失败抛异常。"""
    attempt = 0
    while True:
        try:
            _download_segment_once(
                link, seg, path, timeout=timeout, progress=progress, session=session
            )
            return
        except LinkExpired:
            # 401/403 不是网络故障，绝不重试
            raise
        except TruncatedError as exc:
            # 断流：进度已在段文件里，由下面的重试逻辑重开连接续传；
            # 重试耗尽后会把 E_TRUNCATED 抛给调用方（重跑同一命令继续续传）。
            failure = exc
        except _RetryableStatus as exc:
            if not exc.retryable:
                raise exc.inner from None
            failure: BaseException = exc.inner
        except (requests.RequestException, ConnectionError, TimeoutError, OSError) as exc:
            failure = exc
        except DLinkError:
            raise

        # 段已完整落地（例如最后一次写入后连接断在收尾阶段）—— 视为成功
        if _file_size(path) >= seg.length:
            return
        if task.stop.is_set():  # 别的段已经致命失败，本段不再重试
            raise DLinkError(f"段 {seg.index} 因其他段失败而中止")

        attempt += 1
        if attempt > retries:
            if isinstance(failure, DLinkError):
                raise failure
            raise DLinkError(
                f"段 {seg.index} 重试 {retries} 次仍失败：{failure}",
                hint="网络不稳定或直链已失效；可重跑同一命令做断点续传",
            ) from failure
        sleep(_backoff(attempt - 1))


class TruncatedError(DLinkError):
    """响应体没发完就断流（微云 ~14MB 后断流的典型形态）。

    已写入的字节**保留**在段文件里，``download`` 会中止本次调用并抛这个异常；
    调用方重跑同一 ``dest`` 即可按已落盘字节续传。
    """

    code = "E_TRUNCATED"
    hint = "已下载的进度已保留；重跑同一命令即可从断点续传"

    def __init__(self, message: str, *, hint: str = "", written: int = 0,
                 expected: int = 0) -> None:
        super().__init__(message, hint=hint)
        self.written = written
        self.expected = expected


class _RetryableStatus(Exception):
    """内部用：把 HTTP 状态错误包装成可分类的异常。"""

    def __init__(self, inner: BaseException, *, retryable: bool) -> None:
        super().__init__(str(inner))
        self.inner = inner
        self.retryable = retryable


def _download_segment_once(
    link: DirectLink,
    seg: _Seg,
    path: str,
    *,
    timeout: float,
    progress: _Progress,
    session: requests.Session,
) -> None:
    have = _file_size(path)
    if have >= seg.length:
        return
    if have < 0:
        have = 0

    req_start = seg.start + have
    resp = session.get(
        link.url,
        headers=_build_headers(link, req_start, seg.end),
        cookies=link.cookies or None,
        timeout=(min(30.0, timeout), timeout),
        stream=True,
        allow_redirects=True,
    )

    try:
        _check_status(resp, link, seg)
        code = resp.status_code

        if code == 206:
            offset = _partial_offset(resp, seg)
            if offset != have:
                raise _RetryableStatus(
                    BadResponseError(
                        f"段 {seg.index} 的 Content-Range 与续传点不符"
                        f"（期望从段内 {have} 开始，得到 "
                        f"{resp.headers.get('Content-Range')!r}）"
                    ),
                    retryable=True,
                )
            _write_stream(resp, path, mode="ab", progress=progress,
                          expect=seg.length - have)
            return

        if code == 200:
            # 服务端忽略了 Range：返回的是整个文件。必须截断段文件从头重写，
            # 否则数据错位、校验必失败（这是最容易出错的地方）。
            if have and progress is not None:
                progress.reset_part(have)
            _write_stream(
                resp,
                path,
                mode="wb",
                progress=progress,
                skip=seg.start,
                limit=seg.length,
            )
            return

        if code == 416:
            if _file_size(path) >= seg.length:
                return
            raise _RetryableStatus(
                DLinkError(
                    f"段 {seg.index} 返回 416（Range 不可满足），"
                    f"本地仅 {_file_size(path)}/{seg.length} 字节",
                    hint="可删除 dest.parts 段目录后重跑",
                ),
                retryable=True,
            )

        if 400 <= code < 500:
            # 4xx（除 416/429）直接致命，不浪费退避时间
            raise _RetryableStatus(
                DLinkError(f"段 {seg.index} 返回 HTTP {code}，不重试"),
                retryable=(code in RETRYABLE_4XX),
            )

        raise _RetryableStatus(
            DLinkError(f"段 {seg.index} 返回 HTTP {code}"), retryable=True
        )
    finally:
        resp.close()


def _write_stream(
    resp: requests.Response,
    path: str,
    *,
    mode: str,
    progress: _Progress,
    skip: int = 0,
    limit: Optional[int] = None,
    expect: Optional[int] = None,
) -> None:
    """把响应体写进段文件。

    - ``mode="ab"``：从段文件末尾续写（段内续传）。
    - ``mode="wb"``：先截断段文件，丢弃响应体前 ``skip`` 字节后写入（服务端
      忽略 Range、返回 200 全量时的正确姿势），最多写 ``limit`` 字节。
    - ``expect``：本段本次期望写入的字节数；响应体不足即视为断流，抛
      ``_RetryableStatus``（让外层按已落盘字节续传）。
    """
    written = 0
    skipped = 0
    with open(path, mode) as fh:
        for chunk in _iter_chunks(resp, path):
            if skipped < skip:
                cut = min(len(chunk), skip - skipped)
                skipped += cut
                chunk = chunk[cut:]
                if not chunk:
                    continue
            if limit is not None:
                room = limit - written
                if room <= 0:
                    break
                chunk = chunk[:room]
            fh.write(chunk)
            written += len(chunk)
            progress.add(len(chunk))
            if limit is not None and written >= limit:
                break

    if expect is not None and written < expect:
        # 断流：已写的字节留在段文件里（不删、不回滚），错误信息里点明可重跑续传
        raise TruncatedError(
            f"连接中途断流：本次只落了 {written}/{expect} 字节",
            hint="已下载的进度已保留在段目录中；重跑同一命令会从断点续传",
            written=written,
            expected=expect,
        )


def _iter_chunks(resp: requests.Response, part: str, size: int = CHUNK) -> Iterator[bytes]:
    """流式读响应体，优先读 ``resp.raw``。

    ``resp.raw.read(n)`` 在连接提前关闭时返回空块而不是抛异常，所以能否读完
    由调用方用 ``expect`` **独立核对**——不能把「返回空」当成读完。为了让这种
    情况尽量少，这里把 read 的块大小压在 256KB（socket 缓冲比默认 128KB 大）。
    """
    raw = getattr(resp, "raw", None)
    try:
        if raw is not None and hasattr(raw, "read"):
            # read1：有多少给多少，不等填满缓冲区。断流时能把已收到的字节先落盘，
            # 续传起点更靠前（也更接近 curl 的流式行为）。
            read1 = getattr(raw, "read1", None)
            while True:
                chunk = read1(size) if read1 is not None else raw.read(size)
                if not chunk:
                    return
                yield chunk
        else:  # 极少数自造 Response（测试替身），退回 iter_content
            for chunk in resp.iter_content(size):
                if chunk:
                    yield chunk
    except (urllib3.exceptions.IncompleteRead, urllib3.exceptions.ProtocolError,
            http.client.IncompleteRead, ConnectionError) as exc:
        # 连接被服务端掐断：urllib3 会抛 IncompleteRead/ProtocolError。
        # 统一转成 TruncatedError，让外层按「已落盘字节续传」处理。
        raise TruncatedError(
            f"读取响应体时连接中断：{exc}",
            hint="已下载的进度已保留在段目录中；重跑同一命令会从断点续传",
        ) from exc


class _Task:
    """一次 download() 调用的共享状态（线程安全）。"""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._fatal: Optional[BaseException] = None

    def record_exception(self, exc: BaseException) -> None:
        with self._lock:
            if self._fatal is None:
                self._fatal = exc
        self.stop.set()

    @property
    def fatal(self) -> Optional[BaseException]:
        return self._fatal


# --------------------------------------------------------------------------
# 合并与校验
# --------------------------------------------------------------------------


def _merge_parts(parts_dir: str, dest: str, count: int) -> None:
    """按序流式拼接段文件（分块读写，不整体 read）。"""
    tmp = dest + ".merging"
    try:
        with open(tmp, "wb") as out:
            for i in range(count):
                src = part_path(parts_dir, i)
                with open(src, "rb") as fh:
                    shutil.copyfileobj(fh, out, CHUNK)
        os.replace(tmp, dest)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _hash_file(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------


def download(
    link: DirectLink,
    dest: str,
    *,
    segments: int | None = None,
    workers: int = 8,
    retries: int = 6,
    seg_size: int = DEFAULT_SEG_SIZE,
    timeout: float = 180.0,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """分段并行下载 ``link`` 到 ``dest``，并做三位一体校验。

    返回::

        {"ok": bool, "path": str, "size": int, "sha1": str,
         "expected_size": int, "expected_sha1": str, "segments": int,
         "elapsed": float, "magic": str, "magic_warning": str|None,
         "verified_by": "sha1"|"md5"|"size"|"none"}

    失败语义：鉴权/时效 → ``LinkExpired``；校验不符 → ``IntegrityError``
    （**已合并的文件保留**）；其余可预期错误 → ``DLinkError``。
    """
    started = time.monotonic()
    dest = os.path.abspath(dest)
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # ``size`` 明确给了正数就用它；明确为 0 且带了任何校验值 → 视为真空文件，
    # 完全不发请求；只有「真的不知道大小」才去探测。
    known_size = int(link.size or 0)
    if known_size > 0:
        total_size = known_size
    elif link.size == 0 and (link.sha1 or link.md5) and os.path.exists(dest) is False:
        # DirectLink.size 默认就是 0，无法区分「空文件」与「未知」；
        # 用 hash 是否为「空内容」的 hash 来判定空文件。
        empty_hashes = {
            hashlib.sha1(b"").hexdigest(),
            hashlib.md5(b"").hexdigest(),
        }
        if (link.sha1 or "").lower() in empty_hashes or                 (link.md5 or "").lower() in empty_hashes:
            total_size = 0
        else:
            total_size = _probe_size(link, timeout=timeout)
    else:
        total_size = _probe_size(link, timeout=timeout)

    plan = plan_segments(total_size, segments, seg_size)
    if not plan:
        # 空文件：直接落盘一个空文件，照样跑校验分支
        with open(dest, "wb"):
            pass
        return _verify_and_result(
            link, dest, segments=0, started=started,
        )

    parts_dir = dest + ".parts"
    os.makedirs(parts_dir, exist_ok=True)

    progress_meter = _Progress(total_size, progress)
    task = _Task()
    segs = [_Seg(i, s, e) for i, (s, e) in enumerate(plan)]

    # 续传：已完整的段先记入进度，跳过下载
    pending: list[_Seg] = []
    for seg in segs:
        have = min(_file_size(part_path(parts_dir, seg.index)), seg.length)
        if have >= seg.length:
            progress_meter.add(seg.length)
        else:
            progress_meter.add(have)  # 续传起点已落盘的字节也算进度
            pending.append(seg)

    n_workers = max(1, min(int(workers), len(pending) or 1))
    if pending:
        # 每段独立 Session：共享连接池会退化成单连接速度
        sessions: dict[int, requests.Session] = {}
        try:
            with ThreadPoolExecutor(max_workers=n_workers,
                                    thread_name_prefix="dlink-seg") as pool:
                futures = {}
                for seg in pending:
                    sess = requests.Session()
                    sess.headers.update({"Connection": "close"})
                    sessions[seg.index] = sess
                    futures[
                        pool.submit(
                            _download_segment,
                            link,
                            seg,
                            part_path(parts_dir, seg.index),
                            retries=retries,
                            timeout=timeout,
                            progress=progress_meter,
                            session=sess,
                            task=task,
                            sleep=_sleep,
                        )
                    ] = seg

                for fut in as_completed(futures):
                    try:
                        exc = fut.exception()
                    except BaseException as err:  # CancelledError 等
                        exc = err if isinstance(err, BaseException) else None
                        continue
                    if exc is not None:
                        task.record_exception(exc)
                        for other in futures:
                            other.cancel()
        finally:
            for sess in sessions.values():
                try:
                    sess.close()
                except Exception:  # noqa: BLE001
                    pass

    if task.fatal is not None:
        raise task.fatal

    progress_meter.finish()

    # 段长度自检（防止中途被截断的数据混进合并）
    for seg in segs:
        p = part_path(parts_dir, seg.index)
        got = _file_size(p)
        if got != seg.length:
            raise DLinkError(
                f"段 {seg.index} 落盘 {got} 字节，期望 {seg.length} 字节",
                hint=f"可删除 {parts_dir} 后重跑",
            )

    _merge_parts(parts_dir, dest, len(segs))
    shutil.rmtree(parts_dir, ignore_errors=True)

    return _verify_and_result(link, dest, segments=len(segs), started=started)


def _verify_and_result(link: DirectLink, dest: str, *, segments: int,
                       started: float) -> dict:
    """三位一体校验并组装返回 dict（空文件与正常路径共用）。"""
    size = os.path.getsize(dest)
    expected_size = int(link.size or 0)
    expected_sha1 = (link.sha1 or "").lower()
    expected_md5 = (link.md5 or "").lower()

    # 三位一体：size + sha1（缺失则 md5）；两者都没有就只校验 size
    if expected_size > 0 and size != expected_size:
        raise IntegrityError(
            f"大小不符：得到 {size} 字节（{_human(size)}），期望 {expected_size} 字节",
            hint=f"校验失败的文件已保留在 {dest}（未删除，便于复核）；"
                 f"可用 `dlink verify` 复核，或删除后重跑",
        )

    actual_sha1 = ""
    verified_by = "size"
    if expected_sha1:
        actual_sha1 = _hash_file(dest, "sha1")
        verified_by = "sha1"
        if actual_sha1 != expected_sha1:
            raise IntegrityError(
                f"SHA1 不符：得到 {actual_sha1}，期望 {expected_sha1}",
                hint=f"校验失败的文件已保留在 {dest}（未删除，便于复核）；"
                     f"直链内容可能被平台改写/截断，可重新 resolve 后重试",
            )
    elif expected_md5:
        actual_md5 = _hash_file(dest, "md5")
        verified_by = "md5"
        if actual_md5 != expected_md5:
            raise IntegrityError(
                f"MD5 不符：得到 {actual_md5}，期望 {expected_md5}",
                hint=f"校验失败的文件已保留在 {dest}（未删除，便于复核）",
            )
    else:
        actual_sha1 = _hash_file(dest, "sha1")
        verified_by = "size"

    magic = sniff_magic(dest)
    warning = _magic_warning(magic, link.filename or os.path.basename(dest))

    return {
        "ok": True,
        "path": dest,
        "size": size,
        "sha1": actual_sha1,
        "expected_size": expected_size,
        "expected_sha1": expected_sha1,
        "segments": segments,
        "elapsed": time.monotonic() - started,
        "magic": magic,
        "magic_warning": warning,
        "verified_by": verified_by,
    }


def _probe_size(link: DirectLink, *, timeout: float) -> int:
    """``link.size`` 未知时用 HEAD（失败退化为 Range: bytes=0-0）探测总长。"""
    headers = _build_headers(link, 0, 0)
    headers.pop("Range", None)
    headers["Accept-Encoding"] = "identity"
    try:
        with requests.Session() as sess:
            resp = sess.head(link.url, headers=headers,
                             cookies=link.cookies or None, timeout=timeout,
                             allow_redirects=True)
            if resp.status_code in EXPIRED_STATUS:
                raise LinkExpired(
                    f"探测文件大小时直链返回 HTTP {resp.status_code}",
                    hint="直链有时效，请重新执行 resolve 获取新链接",
                )
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > 0:
                return int(length)

            resp.close()
            resp = sess.get(link.url, headers=_build_headers(link, 0, 0),
                            cookies=link.cookies or None, timeout=timeout,
                            stream=True, allow_redirects=True)
            if resp.status_code in EXPIRED_STATUS:
                raise LinkExpired(
                    f"探测文件大小时直链返回 HTTP {resp.status_code}",
                    hint="直链有时效，请重新执行 resolve 获取新链接",
                )
            parsed = parse_content_range(resp.headers.get("Content-Range", ""))
            resp.close()
            if parsed and parsed[2]:
                return int(parsed[2])
            length = resp.headers.get("Content-Length")
            if length and length.isdigit():
                return int(length)
    except requests.RequestException as exc:
        raise DLinkError(
            f"无法探测文件大小（直链未给 size）：{exc}",
            hint="请在 DirectLink 里填好 size 字段",
        ) from exc
    raise DLinkError(
        "服务端未返回 Content-Length，无法确定分段方案",
        hint="请在 DirectLink 里填好 size 字段",
    )
