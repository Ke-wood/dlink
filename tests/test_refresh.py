"""续链（RefreshManager）的单元测试（不联网）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from dlink.refresh import MAX_REFRESH_ATTEMPTS, RefreshManager


def test_request_without_refresh_info_returns_false():
    """普通链接没有 refresh 信息，无法续链。"""
    m = RefreshManager()
    assert m.request("t1", None) is False
    assert m.request("t1", {}) is False
    assert m.request("t1", {"shareUrl": ""}) is False


def test_request_and_pending():
    m = RefreshManager()
    ok = m.request("t1", {"platform": "baidu",
                          "shareUrl": "https://pan.baidu.com/s/x", "fid": "123"})
    assert ok is True
    p = m.pending()
    assert len(p) == 1
    assert p[0]["task_id"] == "t1"
    assert p[0]["platform"] == "baidu"
    assert p[0]["fid"] == "123"


def test_attempts_limit():
    """超过最大续链次数就不再请求，避免无限循环。"""
    m = RefreshManager()
    assert m.request("t1", {"shareUrl": "x"}, attempts=MAX_REFRESH_ATTEMPTS) is False
    assert m.request("t1", {"shareUrl": "x"},
                     attempts=MAX_REFRESH_ATTEMPTS - 1) is True


def test_fulfill_then_pop():
    m = RefreshManager()
    m.request("t1", {"shareUrl": "s"})
    assert m.fulfill("t1", {"url": "https://new/link"}) is True
    # 已兑现的不再出现在 pending
    assert m.pending() == []
    got = m.pop_fulfilled("t1")
    assert got == {"url": "https://new/link"}
    # 只能取一次
    assert m.pop_fulfilled("t1") is None


def test_fulfill_unknown_task():
    m = RefreshManager()
    assert m.fulfill("nope", {"url": "x"}) is False


def test_timeout_marks_timed_out(monkeypatch):
    """超过超时时间的请求要标记 timed_out 并从 pending 移除。"""
    m = RefreshManager()
    m.request("t1", {"shareUrl": "s"})
    # 把 created 拨回 200 秒前
    m._pending["t1"].created = time.time() - 200
    p = m.pending()
    assert len(p) == 1
    assert p[0]["timed_out"] is True
    # 已被移除
    assert m.pending() == []


def test_cancel():
    m = RefreshManager()
    m.request("t1", {"shareUrl": "s"})
    m.cancel("t1")
    assert m.pending() == []
    assert m.pop_fulfilled("t1") is None


def test_stats():
    m = RefreshManager()
    m.request("t1", {"shareUrl": "s"})
    m.request("t2", {"shareUrl": "s"})
    st = m.stats()
    assert st["pending"] == 2
    assert set(st["pending_ids"]) == {"t1", "t2"}
