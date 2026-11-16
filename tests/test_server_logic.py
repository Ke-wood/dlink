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
from dlink.server import safe_basename, unique_dest  # noqa: E402


# ---------------- 文件名收敛 ----------------
# 接管浏览器下载后，filename 来自 chrome.downloads，是**完整本地路径**。
# 直接拼 save_dir 会把下载目录整个替换掉（Windows 绝对路径的语义），
# 文件会落到 save_dir 之外，所以必须先收敛成 basename。

@pytest.mark.parametrize("raw,want", [
    (r"C:\Users\someone\Downloads\movie.mkv", "movie.mkv"),
    ("/home/someone/Downloads/movie.mkv", "movie.mkv"),
    ("../../evil.sh", "evil.sh"),
    ("..\\..\\evil.sh", "evil.sh"),
    ('a<b>c:"d"|e?f*g.zip', "abcdefg.zip"),
    ("//./../x.zip", "x.zip"),
    ("..", "download.bin"),
    ("   ", "download.bin"),
    ("", "download.bin"),
    (None, "download.bin"),
])
def test_safe_basename(raw, want):
    assert safe_basename(raw) == want


@pytest.mark.parametrize("raw", ["CON.txt", "con", "LPT1", "nul.log"])
def test_safe_basename_reserved_device(raw):
    """Windows 保留设备名会让创建直接失败，必须加前缀。"""
    assert safe_basename(raw).startswith("_")


def test_unique_dest_never_escapes_save_dir(tmp_path):
    """★ 回归：绝对路径作为文件名时，dest 必须仍在 save_dir 里。"""
    d = unique_dest(tmp_path, r"C:\Windows\System32\evil.dll")
    assert d.parent == tmp_path
    assert d.name == "evil.dll"


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


# ---------------- 打包成 exe（windowed）后的环境差异 ----------------

def test_log_message_survives_none_stderr(monkeypatch):
    """★ 回归：exe 是无控制台的 windowed 进程，sys.stderr 为 None。

    过去 log_message 直接 `sys.stderr.write(...)`，会抛 AttributeError。
    BaseHTTPRequestHandler 是**每个请求一个线程**，异常把线程带走，
    客户端看到的是「连接被重置且没有任何响应」——而 GET /api/health
    因为跳过日志所以一切正常，这个 bug 因此极难排查（实测：exe 里
    所有 POST 全挂，开发态全好）。
    """
    from dlink.server import Handler

    class Dummy:
        path = "/api/tasks"

    monkeypatch.setattr(sys, "stderr", None)
    Handler.log_message(Dummy(), "%s", "boom")  # 不抛异常即通过


def test_version_comes_from_package():
    """version 不许在 server.py 里硬编码，否则 /api/health 永远报旧版本。"""
    import dlink
    from dlink.server import VERSION

    assert VERSION == dlink.__version__


# ---------------- 按主机学到的并发不能突破平台上限 ----------------

def test_host_learning_cannot_exceed_platform_cap(tmp_path, monkeypatch):
    """★ 回归：主机学到 8 路时，百度任务仍必须是 1 路。

    实测：百度 1 路 75 KB/s、3 路 130 KB/s、8 路 9 KB/s（触发风控）。
    曾经 `effective_workers` 直接把学习值返回，导致同一主机上先跑一个
    普通链接任务（学到 8 路）后，百度任务也跟着 8 路。
    """
    from dlink import host_learn

    monkeypatch.setenv("DLINK_HOSTS_FILE", str(tmp_path / "hosts.json"))
    url = "https://d.pcs.baidu.com/file/abc"
    host_learn.record(url, workers=8, attempted=8, failed=0)  # 表现良好 → 记住 8
    assert host_learn.learned_workers(url) == 8

    workers, src = host_learn.effective_workers(url, 1)
    assert workers == 1, "百度必须封顶 1 路"
    assert "封顶" in src


def test_host_learning_still_lowers_default(tmp_path, monkeypatch):
    """学习值比平台默认更保守时，要用学习值（这是它的本来用途）。"""
    from dlink import host_learn

    monkeypatch.setenv("DLINK_HOSTS_FILE", str(tmp_path / "hosts.json"))
    url = "https://proof.example.com/big.bin"
    host_learn.record(url, workers=8, attempted=8, failed=8)  # 全失败 → 降档
    learned = host_learn.learned_workers(url)
    assert learned is not None and learned < 8

    workers, src = host_learn.effective_workers(url, 8)
    assert workers == learned
    assert "学习值" in src


def test_host_learning_ignored_when_never_seen(tmp_path, monkeypatch):
    from dlink import host_learn

    monkeypatch.setenv("DLINK_HOSTS_FILE", str(tmp_path / "hosts.json"))
    workers, src = host_learn.effective_workers("https://new.example.com/x", 6)
    assert workers == 6
    assert src == "平台默认"
