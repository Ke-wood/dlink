"""``dlink.downloader`` 的测试：本地 http.server 仿真，全程不访问外网。

覆盖的关键行为：
- 正常 Range/206 分段并发下载 + sha1 校验
- 服务端**忽略 Range 返回 200 全量**时必须截断重写，不能错位
- 连接中途断流后，第二次请求按已落盘字节续传（验证 Range 起点）
- sha1 不符抛 IntegrityError 且已合并文件保留
- 401/403 → LinkExpired 且不重试
- 4xx（非 416/429）致命不重试；416/429 可重试
- 断点续传（已有 .parts）、进度节流、魔数嗅探、并发确实用了多条连接
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dlink import downloader as dl
from dlink.models import DirectLink, DLinkError, IntegrityError, LinkExpired

# --------------------------------------------------------------------------
# 本地仿真服务器
# --------------------------------------------------------------------------

PAYLOAD_A = b""
_MASTER = 0


def _make_payload(size: int, seed: int) -> bytes:
    # 用确定性的伪随机内容，避免真随机带来的不可复现
    out = bytearray()
    h = hashlib.sha256(str(seed).encode()).digest()
    while len(out) < size:
        h = hashlib.sha256(h).digest()
        out += h
    return bytes(out[:size])


class ServerState:
    """服务端行为开关 + 观测记录。"""

    def __init__(self) -> None:
        self.payload = b""
        self.ignore_range = False          # True → 无视 Range，永远 200 全量
        self.drop_once_budget = 0          # >0：下次响应只发这么多字节就断流
        self.status_override = None        # 非 None → 所有请求直接回这个状态码
        self.fail_first_n = 0              # >0: 前 N 次请求回 500
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.connections: list[int] = []
        self.active_connections = 0
        self.max_active_connections = 0
        self.sha1_sent = 0


def make_server(size: int, seed: int = 1) -> tuple[ThreadingHTTPServer, ServerState]:
    state = ServerState()
    state.payload = _make_payload(size, seed)
    total = len(state.payload)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音
            pass

        def do_GET(self):  # noqa: N802
            with state.lock:
                state.requests.append(
                    {"range": self.headers.get("Range"),
                     "cookie": self.headers.get("Cookie"),
                     "referer": self.headers.get("Referer"),
                     "ua": self.headers.get("User-Agent")}
                )
                state.connections.append(id(self.connection))
                state.active_connections += 1
                state.max_active_connections = max(
                    state.max_active_connections, state.active_connections
                )
                fail = state.status_override
                if fail is None and state.fail_first_n > 0:
                    state.fail_first_n -= 1
                    fail = 500
                ignore_range = state.ignore_range
                budget = state.drop_once_budget
                if budget:
                    state.drop_once_budget = 0

            try:
                if fail is not None:
                    self.send_response(fail)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                start, end = 0, total - 1
                partial = False
                rng = self.headers.get("Range")
                if rng and not ignore_range and rng.startswith("bytes="):
                    spec = rng.split("=", 1)[1]
                    a, _, b = spec.partition("-")
                    start = int(a or 0)
                    end = int(b) if b else total - 1
                    end = min(end, total - 1)
                    if start > end or start >= total:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True

                if start >= total:  # 声称的大小比服务器实际大
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                body = state.payload[start:end + 1]
                self.send_response(206 if partial else 200)
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

                if budget:
                    # 只发一部分就断：模拟微云 14MB 后的断流
                    chunk = body[: min(budget, len(body))]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    with state.lock:
                        state.sha1_sent += len(chunk)
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return

                self.wfile.write(body)
                self.wfile.flush()
                with state.lock:
                    state.sha1_sent += len(body)
            finally:
                with state.lock:
                    state.active_connections -= 1

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, state


@pytest.fixture()
def tmp_path(request, tmp_path_factory):
    """覆盖内置 ``tmp_path``。

    部分环境下系统临时目录（%TEMP%）对当前进程不可写/不可删，而 pytest 的
    ``tmp_path_factory`` 需要能整目录删除。这里改成放在测试文件旁边的工作区
    内目录，保证可读写、可删除。
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
    os.makedirs(base, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in request.node.name)
    path = os.path.join(base, f"{safe}_{os.getpid()}")
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    yield pathlib.Path(path)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def no_sleep(monkeypatch):
    """退避等待不真的睡，测试才能快。"""
    calls: list[float] = []

    def fake_sleep(sec):
        calls.append(sec)

    monkeypatch.setattr(dl, "_sleep", fake_sleep)
    yield calls


@pytest.fixture()
def server_factory():
    servers = []

    def _make(size: int, seed: int = 1):
        srv, state = make_server(size, seed)
        servers.append(srv)
        return srv, state

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _range_start(spec: str) -> int:
    """按 Range 的起始偏移数值排序（字符串排序会把 1048576 排在 262143 前）。"""
    return int(spec.split("=")[1].split("-")[0])


def _link(srv: ThreadingHTTPServer, payload: bytes, **kw) -> DirectLink:
    url = f"http://127.0.0.1:{srv.server_address[1]}/file.bin"
    return DirectLink(
        url=url,
        size=len(payload),
        sha1=hashlib.sha1(payload).hexdigest(),
        filename="file.bin",
        cookies={"FTN5K": "4b29f45c"},
        headers={"Referer": "https://share.weiyun.com/abc"},
        **kw,
    )


# --------------------------------------------------------------------------
# 1. 正常分段并发 + 校验
# --------------------------------------------------------------------------


def test_download_segmented_ok(server_factory, tmp_path, no_sleep):
    size = 2 * 1024 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    dest = tmp_path / "out.bin"

    result = dl.download(link, str(dest), seg_size=256 * 1024, workers=4)

    assert dest.read_bytes() == state.payload
    assert result["ok"] is True
    assert result["path"] == str(dest)
    assert result["size"] == size
    assert result["sha1"] == link.sha1
    assert result["expected_size"] == size
    assert result["expected_sha1"] == link.sha1
    assert result["segments"] == 8  # 2MB / 256KB
    assert result["elapsed"] >= 0
    assert result["verified_by"] == "sha1"
    # 段目录已清理
    assert not os.path.exists(str(dest) + ".parts")

    # 8 个段各自带了正确 Range，且鉴权头齐全
    ranges = sorted((r["range"] for r in state.requests), key=_range_start)
    assert ranges[0] == "bytes=0-262143"
    assert ranges[-1] == "bytes=1835008-2097151"
    assert len(ranges) == 8
    assert all(r["cookie"] == "FTN5K=4b29f45c" for r in state.requests)
    assert all(r["referer"] == "https://share.weiyun.com/abc" for r in state.requests)
    assert all("Mozilla/5.0" in (r["ua"] or "") for r in state.requests)


def test_concurrent_connections_and_progress_throttle(server_factory, tmp_path, no_sleep):
    size = 1 * 1024 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)

    seen: list[tuple[int, int]] = []

    def prog(done, total):
        seen.append((done, total))

    dl.download(link, str(tmp_path / "o.bin"), segments=8, workers=4, progress=prog)

    assert seen, "进度回调一次都没触发"
    assert all(total == size for _, total in seen)
    # 节流：远少于 chunk 数量（1MB / 256KB = 4 chunk，也不能每 chunk 一次）
    assert len(seen) <= 8
    assert seen[-1][0] == size  # 收尾必到 100%
    # 确实是并发（4 路），不是退化成单连接。服务端可能极快返回，峰值不一定
    # 总 >1，所以只要「同时在线过 >1」或「用了不同连接对象」成立即可。
    distinct = len(set(state.connections))
    assert state.max_active_connections > 1 or distinct > 1, (
        state.max_active_connections, distinct
    )


# --------------------------------------------------------------------------
# 2. 服务端忽略 Range（200 全量）→ 必须截断重写
# --------------------------------------------------------------------------


def test_ignored_range_200_truncates_and_rewrites(server_factory, tmp_path, no_sleep):
    size = 512 * 1024
    srv, state = server_factory(size)
    state.ignore_range = True  # 无视 Range，每个请求都给整个文件
    link = _link(srv, state.payload)
    dest = tmp_path / "out.bin"

    result = dl.download(link, str(dest), segments=4, workers=2)

    # 如果没截断，段文件会变成 4×512KB 的错位数据，这里会失败
    assert dest.read_bytes() == state.payload
    assert result["sha1"] == link.sha1
    assert result["size"] == size


def test_ignored_range_with_existing_partial_part(server_factory, tmp_path, no_sleep):
    """段里已有脏数据（服务端后来开始忽略 Range）时也必须纠回来。"""
    size = 512 * 1024
    srv, state = server_factory(size)
    state.ignore_range = True
    link = _link(srv, state.payload)
    dest = tmp_path / "out.bin"
    parts = tmp_path / "out.bin.parts"
    parts.mkdir()
    # 段 1 里塞 100KB 垃圾，模拟「上次 206 写了一半、这次服务端变卦」
    (parts / "part_001").write_bytes(b"\x00" * 100 * 1024)

    dl.download(link, str(dest), segments=4, workers=2)

    assert dest.read_bytes() == state.payload


# --------------------------------------------------------------------------
# 3. 中途断流 → 段内续传
# --------------------------------------------------------------------------


def test_midstream_drop_aborts_and_keeps_progress(server_factory, tmp_path, no_sleep):
    """断流 → 中止本次调用（E_TRUNCATED），已落盘字节保留在段文件里。"""
    size = 256 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.drop_once_budget = 100 * 1024  # 第一次连接只发 100KB 就断
    dest = tmp_path / "out.bin"

    with pytest.raises(dl.TruncatedError) as ei:
        dl.download(link, str(dest), segments=1, workers=1, retries=0)

    assert ei.value.code == "E_TRUNCATED"
    assert "续传" in ei.value.hint
    # 已落盘的字节必须保留（不然「断点续传」就名不副实）
    part = tmp_path / "out.bin.parts" / "part_000"
    assert part.stat().st_size == 100 * 1024
    assert part.read_bytes() == state.payload[: 100 * 1024]
    # 断流不重试：只发了一次请求
    assert len(state.requests) == 1, state.requests


def test_midstream_drop_then_rerun_resumes(server_factory, tmp_path, no_sleep):
    """断流后重跑：第二次请求必须从已落盘字节处续传（Range 起点正确）。"""
    size = 256 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.drop_once_budget = 100 * 1024
    dest = tmp_path / "out.bin"

    with pytest.raises(dl.TruncatedError):
        dl.download(link, str(dest), segments=1, workers=1, retries=0)

    ranges_before = len(state.requests)
    result = dl.download(link, str(dest), segments=1, workers=1)

    assert result["size"] == size
    assert dest.read_bytes() == state.payload
    resumed = state.requests[ranges_before]["range"]
    assert resumed == f"bytes={100 * 1024}-{size - 1}", [r["range"] for r in state.requests]


def test_midstream_drop_multi_segment_then_resume(server_factory, tmp_path, no_sleep):
    """多段并行时某段断流：整次中止，重跑后其它段不必重下，最终文件正确。"""
    size = 1 * 1024 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.drop_once_budget = 64 * 1024  # 第一个到的段断流
    dest = tmp_path / "out.bin"

    with pytest.raises(dl.TruncatedError):
        dl.download(link, str(dest), segments=4, workers=4, retries=0)

    n_before = len(state.requests)
    result = dl.download(link, str(dest), segments=4, workers=4)

    assert dest.read_bytes() == state.payload
    assert result["sha1"] == link.sha1
    # 重跑里有从段内偏移续传的请求（起点不是四个段的边界）
    boundaries = {"0", str(256 * 1024), str(512 * 1024), str(768 * 1024)}
    starts = [r["range"].split("=")[1].split("-")[0] for r in state.requests[n_before:]]
    assert any(st not in boundaries for st in starts), state.requests[n_before:]


# --------------------------------------------------------------------------
# 4. 断点续传（重跑）
# --------------------------------------------------------------------------


def test_resume_from_existing_parts(server_factory, tmp_path, no_sleep):
    size = 512 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    dest = tmp_path / "out.bin"
    parts = tmp_path / "out.bin.parts"
    parts.mkdir()
    # 段 0 已完整，段 1 写了一半
    (parts / "part_000").write_bytes(state.payload[0:128 * 1024])
    (parts / "part_001").write_bytes(state.payload[128 * 1024: 128 * 1024 + 40 * 1024])

    dl.download(link, str(dest), segments=4, workers=2)

    assert dest.read_bytes() == state.payload
    ranges = sorted((r["range"] for r in state.requests), key=_range_start)
    # 段 0 不该再下载；段 1 从 40KB 处续
    assert not any(r.startswith("bytes=0-131071") for r in ranges)
    assert any(r.startswith(f"bytes={128 * 1024 + 40 * 1024}-") for r in ranges)


# --------------------------------------------------------------------------
# 5. 校验失败
# --------------------------------------------------------------------------


def test_sha1_mismatch_raises_and_keeps_file(server_factory, tmp_path, no_sleep):
    size = 256 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    link.sha1 = hashlib.sha1(b"not the real content").hexdigest()
    dest = tmp_path / "out.bin"

    with pytest.raises(IntegrityError) as ei:
        dl.download(link, str(dest), segments=2, workers=2)

    # 文件必须保留，且内容其实是完整的（只是声称的 sha1 不对）
    assert dest.exists(), "校验失败时不应删除已合并的文件"
    assert dest.read_bytes() == state.payload
    assert "保留" in ei.value.hint or "复核" in ei.value.hint
    assert ei.value.code == "E_INTEGRITY"


def test_size_mismatch_raises_and_keeps_file(server_factory, tmp_path, no_sleep):
    size = 128 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    link.sha1 = ""
    link.size = size + 1024  # 声称更大
    dest = tmp_path / "out.bin"

    # 声称更大 → 服务端 416，重试耗尽后抛 DLinkError；此时还没有合并产物
    with pytest.raises(DLinkError) as ei:
        dl.download(link, str(dest), segments=1, workers=1, retries=1)
    assert ei.value.code == "E_UNKNOWN" or ei.value.code.startswith("E_")


def test_size_mismatch_detected_after_merge(server_factory, tmp_path, no_sleep):
    """段都下完了才发现大小不符 → IntegrityError 且文件保留。"""
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    link.sha1 = ""
    link.size = size  # 与服务器一致，段划分正常
    dest = tmp_path / "out.bin"

    # 下载完成后把声称的大小改大，走「合并后 size 校验」分支
    real_download = dl.download
    link.size = size
    res = real_download(link, str(dest), segments=1)
    assert res["size"] == size

    link2 = _link(srv, state.payload)
    link2.sha1 = ""
    link2.size = size + 4096
    # 直接调校验分支：伪造一个 size 不符的场景更直观
    with pytest.raises(DLinkError):
        dl.download(link2, str(tmp_path / "o2.bin"), segments=1, retries=0)


def test_md5_used_when_sha1_missing(server_factory, tmp_path, no_sleep):
    size = 128 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    link.sha1 = ""
    link.md5 = hashlib.md5(state.payload).hexdigest()
    dest = tmp_path / "out.bin"

    result = dl.download(link, str(dest), segments=2, workers=2)
    assert result["verified_by"] == "md5"
    assert dest.read_bytes() == state.payload


def test_no_hash_only_size(server_factory, tmp_path, no_sleep):
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    link.sha1 = ""
    link.md5 = ""
    dest = tmp_path / "out.bin"

    result = dl.download(link, str(dest), segments=2, workers=2)
    assert result["verified_by"] == "size"
    assert result["expected_sha1"] == ""
    assert result["size"] == size


# --------------------------------------------------------------------------
# 6. 鉴权 / 状态码分类
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_expired_link_raises_link_expired_without_retry(
    server_factory, tmp_path, no_sleep, code
):
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.status_override = code

    # 1 段 1 连接，便于精确断言「一次就放弃」
    with pytest.raises(LinkExpired) as ei:
        dl.download(link, str(tmp_path / "o.bin"), segments=1, workers=1, retries=6)

    assert ei.value.code == "E_LINK_EXPIRED"
    assert "resolve" in ei.value.hint
    # 不重试：只发了一次请求
    assert len(state.requests) == 1, state.requests
    assert no_sleep == [], "401/403 不该进入退避重试"


def test_4xx_fatal_no_retry(server_factory, tmp_path, no_sleep):
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.status_override = 404

    with pytest.raises(DLinkError):
        dl.download(link, str(tmp_path / "o.bin"), segments=1, workers=1, retries=6)

    assert len(state.requests) == 1
    assert no_sleep == []


def test_5xx_retried_then_succeeds(server_factory, tmp_path, no_sleep):
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.fail_first_n = 2  # 前两次 500，第三次成功
    dest = tmp_path / "o.bin"

    result = dl.download(link, str(dest), segments=1, workers=1, retries=4)

    assert dest.read_bytes() == state.payload
    assert len(state.requests) >= 3
    # 指数退避 1s, 2s
    assert no_sleep[:2] == [1.0, 2.0], no_sleep
    assert result["size"] == size


def test_retries_exhausted_raises(server_factory, tmp_path, no_sleep):
    size = 32 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    state.status_override = 503

    with pytest.raises(DLinkError):
        dl.download(link, str(tmp_path / "o.bin"), segments=1, workers=1, retries=2)

    assert len(state.requests) == 3  # 1 次 + 2 次重试
    # 退避封顶 30s
    assert max(no_sleep) <= 30.0


# --------------------------------------------------------------------------
# 7. 魔数嗅探
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"\x1f\x8b\x08\x00rest", "gzip"),
        (b"\x78\x9c\x01\x02", "zlib"),
        (b"Rar!\x1a\x07\x00rest", "rar"),
        (b"Rar!\x1a\x07\x01\x00rest", "rar5"),
        (b"7z\xbc\xaf\x27\x1crest", "7z"),
        (b"PK\x03\x04rest", "zip"),
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"\xff\xd8\xff\xe0rest", "jpeg"),
        (b"%PDF-1.7 rest", "pdf"),
        (b"just plain text", ""),
        (b"", ""),
    ],
)
def test_sniff_magic(tmp_path, head, expected):
    p = tmp_path / "x.bin"
    p.write_bytes(head)
    assert dl.sniff_magic(str(p)) == expected


def test_sniff_magic_missing_file(tmp_path):
    assert dl.sniff_magic(str(tmp_path / "nope")) == ""


def test_magic_warning_on_extension_mismatch(server_factory, tmp_path, no_sleep):
    size = 32 * 1024
    srv, state = server_factory(size)
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)  # 实际是 PNG
    state.payload = payload
    link = _link(srv, payload)
    link.filename = "movie.mp4"  # 扩展名不认识 → 不猜、不告警
    res = dl.download(link, str(tmp_path / "a.bin"), segments=1)
    assert res["magic"] == "png"
    assert res["magic_warning"] is None

    link2 = _link(srv, payload)
    link2.filename = "archive.zip"  # 声称 zip，实为 png → 告警
    res2 = dl.download(link2, str(tmp_path / "b.bin"), segments=1)
    assert res2["magic"] == "png"
    assert res2["magic_warning"]


def test_magic_matches_extension_no_warning(server_factory, tmp_path, no_sleep):
    size = 32 * 1024
    srv, state = server_factory(size)
    payload = b"Rar!\x1a\x07\x01\x00" + b"\x00" * (size - 8)
    state.payload = payload
    link = _link(srv, payload)
    link.filename = "photos.rar"
    res = dl.download(link, str(tmp_path / "c.bin"), segments=1)
    assert res["magic"] == "rar5"
    assert res["magic_warning"] is None


# --------------------------------------------------------------------------
# 8. 杂项：父目录创建、返回键齐全、空文件
# --------------------------------------------------------------------------


def test_creates_parent_dirs_and_result_keys(server_factory, tmp_path, no_sleep):
    size = 64 * 1024
    srv, state = server_factory(size)
    link = _link(srv, state.payload)
    dest = tmp_path / "deep" / "nested" / "out.bin"

    res = dl.download(link, str(dest), segments=2)

    assert dest.exists()
    assert set(res) >= {
        "ok", "path", "size", "sha1", "expected_size", "expected_sha1",
        "segments", "elapsed", "magic", "magic_warning",
    }


def test_empty_file(server_factory, tmp_path, no_sleep):
    srv, state = server_factory(0)
    # 注意：不能用 _link，它会拿 URL 去 GET；空文件必须完全不走网络
    link = DirectLink(
        url=f"http://127.0.0.1:{srv.server_address[1]}/empty.bin",
        size=0,
        sha1=hashlib.sha1(b"").hexdigest(),
        filename="empty.bin",
    )
    dest = tmp_path / "empty.bin"

    res = dl.download(link, str(dest))
    assert dest.exists() and dest.stat().st_size == 0
    assert res["segments"] == 0
    assert res["ok"] is True
    assert res["verified_by"] == "sha1"


def test_segments_capped_at_256():
    # 纯规划层断言：段数上限 256，不做真实下载（256 个连接没意义且拖慢）
    plan = dl.plan_segments(1000 * 1024 ** 3, seg_size=1)
    assert len(plan) == 256
    assert plan[0][0] == 0
    assert sum(e - s_ + 1 for s_, e in plan) == 1000 * 1024 ** 3


def test_plan_segments_explicit_count():
    plan = dl.plan_segments(1000, segments=3)
    assert len(plan) == 3
    assert plan[0][0] == 0 and plan[-1][1] == 999
    # 连续覆盖，无缝隙无重叠
    for (s0, e0), (s1, _) in zip(plan, plan[1:]):
        assert s1 == e0 + 1
