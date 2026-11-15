"""夸克 / UC 平台测试（不联网）。"""

from __future__ import annotations

import pytest

from dlink.models import AuthRequired, NotLoggedIn, RemoteFile
from dlink.platforms.quark import (
    QuarkPlatform,
    QuarkSession,
    UCPlatform,
    uc_download_token,
)


# ---------- UC 的 ut token：有官方测试向量，必须逐字节一致 ----------

def test_uc_download_token_matches_official_vector():
    """AList 官方测试向量。这个算法错了 UC 完全不可用。"""
    got = uc_download_token("0123456789abcdefghijklmn")
    assert got == "NelUvDsfgQ5vcbK/BaZ1HoSnF9RePiGAuxFxhYc/dPslAA=="


def test_uc_download_token_length_validation():
    from dlink.platforms.quark import QuarkError as QE
    with pytest.raises(QE):
        uc_download_token("tooshort")


# ---------- URL 解析 ----------

@pytest.mark.parametrize("url,ok", [
    ("https://pan.quark.cn/s/abcdef123456", True),
    ("https://drive.uc.cn/s/abc123", False),
    ("https://pan.baidu.com/s/1abc", False),
])
def test_quark_match(url, ok):
    assert QuarkPlatform.match(url) is ok


@pytest.mark.parametrize("url,ok", [
    ("https://drive.uc.cn/s/abc123", True),
    ("https://pan.quark.cn/s/abcdef123456", False),
])
def test_uc_match(url, ok):
    assert UCPlatform.match(url) is ok


def test_parse_share_key():
    assert QuarkPlatform.parse_share_key("https://pan.quark.cn/s/abcdef123456") == "abcdef123456"
    assert UCPlatform.parse_share_key("https://drive.uc.cn/s/abc123") == "abc123"


def test_parse_pwd():
    assert QuarkPlatform.parse_pwd_from_url("https://pan.quark.cn/s/abc?pwd=1234") == "1234"
    assert QuarkPlatform.parse_pwd_from_url("https://pan.quark.cn/s/abc") is None


# ---------- 登录态 ----------

def test_not_logged_in_raises():
    p = QuarkPlatform(QuarkSession({}))
    with pytest.raises(NotLoggedIn):
        p._need_login()


def test_logged_in_by_puus():
    assert QuarkSession({"__puus": "x"}).is_logged_in()
    assert QuarkSession({"__pus": "x"}).is_logged_in()
    assert not QuarkSession({"other": "x"}).is_logged_in()


# ---------- 策略 ----------

def test_workers():
    """夸克二阶段限速：窗口期给 6 路。"""
    assert QuarkPlatform(QuarkSession({})).recommended_workers() == 6
    assert UCPlatform(QuarkSession({})).recommended_workers() == 6


# ---------- cookie 快照 ----------

def test_cookie_snapshot_becomes_dict():
    """直链与取链时的 cookie 强绑定，快照必须完整转成 dict。"""
    hdr = "__puus=abc; __pus=def; b-user-id=123"
    d = QuarkPlatform._snapshot_dict(hdr)
    assert d == {"__puus": "abc", "__pus": "def", "b-user-id": "123"}


# ---------- 错误码处理 ----------

class _R:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def test_code_31001_means_login_required(monkeypatch):
    """分享文件直取返回 31001，必须映射成 NotLoggedIn 而不是普通错误。"""
    p = QuarkPlatform(QuarkSession({"__puus": "x"}))
    monkeypatch.setattr(p, "_req",
                        lambda *a, **k: {"code": 31001,
                                         "message": "require login [share missing]"})
    f = RemoteFile("fid1", "a.bin", 100)
    with pytest.raises(NotLoggedIn):
        p.get_direct_link("pwdid", f)


def test_code_23018_means_guest_detected(monkeypatch):
    """23018 是被判游客（UA 不对），要提示 UA 问题而不是登录问题。"""
    from dlink.platforms.quark import QuarkError as QE
    p = QuarkPlatform(QuarkSession({"__puus": "x"}))
    monkeypatch.setattr(p, "_req", lambda *a, **k: {"code": 23018, "message": "size limit"})
    with pytest.raises(QE) as ei:
        p.get_direct_link("pwdid", RemoteFile("fid1", "a.bin", 100))
    assert not isinstance(ei.value, NotLoggedIn)
    assert "UA" in ei.value.hint


def test_bad_passcode_raises_auth(monkeypatch):
    p = QuarkPlatform(QuarkSession({}))
    monkeypatch.setattr(p, "_req", lambda *a, **k: {"code": 41008, "message": "need passcode"})
    with pytest.raises(AuthRequired):
        p._share_token("pwdid", None)
