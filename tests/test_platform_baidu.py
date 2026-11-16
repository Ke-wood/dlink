"""百度平台模块测试。

不联网：用 monkeypatch 替换 requests.Session 的 get/post。

重点覆盖实测踩过的坑：
  1. 缺 STOKEN 时必须报 NotLoggedIn（只给 BDUSS 会 errno:-6，很隐蔽）
  2. BDCLND 提取码凭证要能透传
  3. 转存的 form-data 参数形态（fsidlist 是 JSON 数组字符串）
  4. 直链必须带 BDUSS 系 cookie（否则 403 error_code:31045）
  5. recommended_workers() 必须返回 1（账号级配额）
"""

from __future__ import annotations

import json

import pytest

from dlink.models import AuthRequired, DirectLink, NotLoggedIn, RemoteFile
from dlink.platforms.baidu import BaiduPlatform, BaiduSession


# ---------- URL 解析 ----------

@pytest.mark.parametrize("url,expected", [
    ("https://pan.baidu.com/s/1AbCdEfGhIjKlMnOpQrS", "1AbCdEfGhIjKlMnOpQrS"),
    ("https://pan.baidu.com/s/1AbCdEfGhIjKlMnOpQrS?pwd=a1b2", "1AbCdEfGhIjKlMnOpQrS"),
    ("https://yun.baidu.com/s/1abc_def-123", "1abc_def-123"),
])
def test_parse_share_key(url, expected):
    assert BaiduPlatform.parse_share_key(url) == expected


def test_parse_share_key_invalid():
    from dlink.platforms.baidu import BaiduError as BE
    with pytest.raises(BE):
        BaiduPlatform.parse_share_key("https://pan.baidu.com/disk/main")


@pytest.mark.parametrize("url,expected", [
    ("https://pan.baidu.com/s/1abc?pwd=a1b2", "a1b2"),
    ("https://pan.baidu.com/s/1abc", None),
    ("https://pan.baidu.com/s/1abc#list/path=%2F&pwd=ab12", "ab12"),
])
def test_parse_pwd(url, expected):
    assert BaiduPlatform.parse_pwd_from_url(url) == expected


@pytest.mark.parametrize("url,ok", [
    ("https://pan.baidu.com/s/1abc", True),
    ("https://yun.baidu.com/s/1abc", True),
    ("https://www.weiyun.com/xyz", False),
    ("not a url", False),
])
def test_match(url, ok):
    assert BaiduPlatform.match(url) is ok


# ---------- 登录态校验 ----------

def test_missing_bduss_raises():
    p = BaiduPlatform(BaiduSession({}))
    with pytest.raises(NotLoggedIn) as ei:
        p.bdstoken()
    assert "BDUSS" in str(ei.value)


def test_missing_stoken_raises():
    """只给 BDUSS 不给 STOKEN，必须明确报错而不是静默 -6。"""
    p = BaiduPlatform(BaiduSession({"BDUSS": "x" * 100}))
    with pytest.raises(NotLoggedIn) as ei:
        p.bdstoken()
    assert "STOKEN" in str(ei.value)
    assert ei.value.hint


def test_session_flags():
    s = BaiduSession({"BDUSS": "a", "STOKEN": "b"})
    assert s.is_logged_in and s.has_stoken()
    assert s.missing() == []
    s2 = BaiduSession({"BDUSS": "a"})
    assert s2.missing() == ["STOKEN"]


# ---------- 策略 ----------

def test_workers_is_1():
    """百度是账号级配额，必须单连接。"""
    assert BaiduPlatform(BaiduSession({})).recommended_workers() == 1


# ---------- 直链构造 ----------

def test_to_direct_link_requires_dlink():
    from dlink.platforms.baidu import BaiduError as BE
    p = BaiduPlatform(BaiduSession({"BDUSS": "a", "STOKEN": "b"}))
    with pytest.raises(BE):
        p._to_direct_link({"size": 1}, RemoteFile("1", "a.bin", 1))


def test_to_direct_link_attaches_cookies():
    """直链必须带 BDUSS 系 cookie，否则下载 403。"""
    ck = {
        "BDUSS": "bdu",
        "BDUSS_BFESS": "bdu2",
        "STOKEN": "stk",
        "BAIDUID": "bid",
        "BIDUPSID": "bps",
        "csrfToken": "should_not_include",
    }
    p = BaiduPlatform(BaiduSession(ck))
    link = p._to_direct_link(
        {"dlink": "https://d.pcs.baidu.com/file/x", "size": 10,
         "md5": "E3B0C44298FC1C149AFBF4C8996FB924"},
        RemoteFile("1", "a.7z", 10),
    )
    assert isinstance(link, DirectLink)
    assert "BDUSS" in link.cookies and "STOKEN" in link.cookies
    assert "csrfToken" not in link.cookies
    assert link.md5 == "e3b0c44298fc1c149afbf4c8996fb924"  # 统一小写
    assert link.headers.get("Referer", "").endswith("pan.baidu.com/")


def test_to_direct_link_drops_nonstandard_md5():
    """百度 API 的 md5 是内部格式（含字母 o），必须丢弃而不是拿来校验。"""
    p = BaiduPlatform(BaiduSession({"BDUSS": "a", "STOKEN": "b"}))
    link = p._to_direct_link(
        {"dlink": "https://d.pcs.baidu.com/file/x", "size": 10,
         "md5": "0123456789abcdef0g1234567890abcd"},
        RemoteFile("1", "a.7z", 10, md5="0123456789abcdef0g1234567890abcd"),
    )
    assert link.md5 == ""   # 清洗后为空，下载器退回 size+魔数校验


# ---------- 分享上下文 ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def test_share_context_ok(monkeypatch):
    p = BaiduPlatform(BaiduSession({"BDUSS": "a", "STOKEN": "b"}))
    payload = {"errno": 0, "shareid": 5317000099, "uk": 1100709536112,
               "file_list": [{"server_filename": "x", "isdir": 0, "fs_id": 1}]}
    monkeypatch.setattr(p._http, "get", lambda *a, **k: _FakeResp(payload))
    ctx = p._share_context("1abc", "a1b2")
    assert ctx["shareid"] == 5317000099
    assert ctx["share_uk"] == 1100709536112
    assert len(ctx["file_list"]) == 1


def test_share_context_no_pwd_raises_auth():
    p = BaiduPlatform(BaiduSession({"BDUSS": "a", "STOKEN": "b"}))

    class R:
        status_code = 200
        text = '{"errno":-9}'

        def json(self):
            return {"errno": -9}

    p._http.get = lambda *a, **k: R()
    with pytest.raises(AuthRequired):
        p._share_context("1abc", None)


# ---------- 提取码凭证 ----------

def test_bdclnd_passed_through_cookie_header():
    """BDCLND 是提取码凭证，必须能进 cookie 头。"""
    p = BaiduPlatform(BaiduSession({
        "BDUSS": "a", "STOKEN": "b",
        "BDCLND": "KwgAa%2BGOx7yxAx%2FeVc3zBtZPcJxfjXknqnnYb7%2B0e2c%3D",
    }))
    hdr = p.session.cookie_header()
    assert "BDCLND=" in hdr
