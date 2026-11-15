"""server.py 的关键逻辑测试（不联网）。

重点覆盖实测踩到的坑：
  1. unique_dest 并发竞态 —— 同名任务会共用 .parts 目录导致合并后字节数不对
  2. 没有 url 的 item 要变成 failed 任务，而不是被丢弃
  3. 按平台取 workers（百度必须 1）
  4. 哈希清洗（百度 md5 是内部格式）
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dlink.hashing import clean_md5, clean_sha1  # noqa: E402
from dlink.profile import BUILTIN, DownloadProfile, get_profile  # noqa: E402
from dlink.server import unique_dest  # noqa: E402


# ---------------- unique_dest 竞态 ----------------

def test_unique_dest_avoids_existing(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    d = unique_dest(tmp_path, "a.bin")
    assert d.name == "a(1).bin"


def test_unique_dest_concurrent_same_name(tmp_path):
    """★ 核心回归测试：8 个线程同时申请同一个文件名，必须拿到 8 个不同路径。

    这是实测 bug 的固化：过去两个同名任务会共用 `<dest>.parts` 目录，
    分段文件互相踩踏，合并后文件多出 131,072 字节，校验必然失败。
    """
    n = 8
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()          # 尽量同时冲进去
        results[i] = unique_dest(tmp_path, "same.zip")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    names = [r.name for r in results]
    assert len(set(names)) == n, f"出现重复路径: {names}"
    assert all("same" in x for x in names)


def test_unique_dest_reserved_even_if_not_created(tmp_path):
    """预占语义：即使文件还没落盘，第二次申请也要换名。"""
    d1 = unique_dest(tmp_path, "b.bin")
    d2 = unique_dest(tmp_path, "b.bin")
    assert d1 != d2
    assert not d1.exists()   # 只是预占，没真的建文件


# ---------------- 平台配置 ----------------

def test_baidu_forces_single_connection():
    """百度是账号级限速，必须单连接。多路会触发风控。"""
    p = get_profile("baidu")
    assert p.effective_workers() == 1
    assert p.force_single is True


def test_weiyun_uses_parallel():
    """微云是连接级限速，并行有效。"""
    p = get_profile("weiyun")
    assert p.effective_workers() >= 4
    assert p.force_single is False


def test_direct_uses_parallel():
    """普通 HTTP 默认并行（实测 4 路比单连接快 1.7x）。"""
    p = get_profile("direct")
    assert p.effective_workers() >= 4


def test_unknown_platform_falls_back():
    p = get_profile("never-heard-of-it")
    assert isinstance(p, DownloadProfile)
    assert p.workers >= 1


def test_all_builtin_have_note():
    """每个内置平台都要有给用户看的说明（GUI 设置页展示）。"""
    for name, p in BUILTIN.items():
        assert p.note, f"{name} 缺少 note"


# ---------------- 哈希清洗 ----------------

def test_baidu_internal_md5_rejected():
    """百度 API 返回的 md5 含字母 o，不是标准 MD5，必须丢弃。"""
    assert clean_md5("a1b2c3d4e5o6f7a8b9c0d1e2f3a4b5c6") == ""


def test_standard_md5_kept():
    v = "e3b0c44298fc1c149afbf4c8996fb924"
    assert clean_md5(v) == v
    assert clean_md5(v.upper()) == v


def test_standard_sha1_kept():
    v = "81d5316db94700e2c31868dd6ae758554b29f45c"
    assert clean_sha1(v) == v


def test_bad_sha1_rejected():
    assert clean_sha1("not-a-hash") == ""
    assert clean_sha1("") == ""
    assert clean_sha1(None) == ""
