"""微云平台适配层单测：全程不联网，monkeypatch 掉 requests.Session.post。

覆盖用户要求的四类断言：
  * URL 解析（各种分享链接形态）
  * g_tk 算法（自行按公式算期望值）
  * 双重 JSON 信封结构（cmd / req_body 键名）
  * retcode 190011 → NotLoggedIn；无 skey 时先权威路径再降级
"""

from __future__ import annotations

import json

import pytest

from dlink.models import AuthRequired, DirectLink, DLinkError, NotLoggedIn, RemoteFile
from dlink.platforms.weiyun import WeiyunPlatform, WeiyunSession

SHARE_KEY = "AbCdEf123456"


# ------------------------------------------------------------------ 工具


def envelope(rsp_header: dict, rsp_body: dict) -> dict:
    """服务端响应的形态：rsp_header / rsp_body 也是 JSON 字符串（双重编码）。"""
    return {
        "rsp_header": json.dumps(rsp_header),
        "rsp_body": json.dumps(rsp_body),
    }


def ok(rsp_body: dict) -> dict:
    return envelope({"retcode": 0, "retmsg": ""}, rsp_body)


class FakeResp:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else (
            json.dumps(payload) if payload is not None else ""
        )

    def json(self):
        if self._payload is None:
            raise ValueError("empty body")
        return self._payload


class Recorder:
    """记录 post 调用，并按 URL 里出现的接口名返回预置响应。"""

    def __init__(self, routes):
        self.routes = routes  # {(protocol, name): [resp, resp, ...]}
        self.calls = []

    def __call__(self, url, headers=None, data=None, timeout=None, cookies=None, **kw):
        self.calls.append({"url": url, "headers": headers, "data": data})
        for (proto, name), resps in self.routes.items():
            if f"/{proto}/{name}" in url:
                if not resps:
                    raise AssertionError(f"{proto}/{name} 被多调用了一次")
                return resps.pop(0)
        raise AssertionError(f"未预置的 URL: {url}")

    def names(self):
        out = []
        for c in self.calls:
            body = c["url"].split("/webapp/json/", 1)[1].split("?", 1)[0]
            out.append(body)
        return out

    def decoded(self, i=0):
        """返回第 i 次请求的 (req_header_dict, req_body_dict)。"""
        raw = json.loads(self.calls[i]["data"].decode("utf-8"))
        return json.loads(raw["req_header"]), json.loads(raw["req_body"])


@pytest.fixture
def patch_post(monkeypatch):
    def _install(routes) -> Recorder:
        rec = Recorder(routes)
        monkeypatch.setattr("requests.Session.post", rec)
        return rec

    return _install


@pytest.fixture
def logged_in():
    return WeiyunSession({"skey": "@abcdefg", "uin": "o0123456"}, ua="UA-TEST")


# ------------------------------------------------------------------ URL 解析


@pytest.mark.parametrize(
    "url",
    [
        "https://share.weiyun.com/AbCdEf123456",
        "https://share.weiyun.com/AbCdEf123456/",
        "https://share.weiyun.com/AbCdEf123456?pwd=1234",
        "https://share.weiyun.com/AbCdEf123456#pwd=1234",
        "https://share.weiyun.com/AbCdEf123456?ADTAG=copylink#/detail",
        "http://share.weiyun.com/AbCdEf123456",
        "//share.weiyun.com/AbCdEf123456",
        "https://www.weiyun.com/share/AbCdEf123456",
        "share.weiyun.com/AbCdEf123456",
        "AbCdEf123456",
    ],
)
def test_parse_share_key_variants(url):
    assert WeiyunPlatform.parse_share_key(url) == SHARE_KEY


@pytest.mark.parametrize(
    "url",
    [
        "https://share.weiyun.com/AbCdEf123456",
        "https://share.weiyun.com/AbCdEf123456?pwd=1234",
        "https://www.weiyun.com/share/AbCdEf123456",
    ],
)
def test_match_true(url):
    assert WeiyunPlatform.match(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://pan.baidu.com/s/1abc",
        "https://www.aliyundrive.com/s/abc",
        "https://share.weiyun.com.evil.com/AbCdEf123456",
        "ftp://share.weiyun.com/AbCdEf123456",
        "随便一段文字",
    ],
)
def test_match_false(url):
    assert WeiyunPlatform.match(url) is False


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://share.weiyun.com/AbCdEf123456?pwd=1234", "1234"),
        ("https://share.weiyun.com/AbCdEf123456#pwd=xyz", "xyz"),
        ("https://share.weiyun.com/AbCdEf123456", None),
        ("", None),
    ],
)
def test_parse_pwd_from_url(url, expected):
    assert WeiyunPlatform.parse_pwd_from_url(url) == expected


def test_parse_share_key_rejects_garbage():
    with pytest.raises(DLinkError):
        WeiyunPlatform.parse_share_key("https://pan.baidu.com/s/1abc")
    with pytest.raises(DLinkError):
        WeiyunPlatform.parse_share_key("https://share.weiyun.com/")
    with pytest.raises(DLinkError):
        WeiyunPlatform.parse_share_key("")


# ------------------------------------------------------------------ g_tk


def _expected_g_tk(skey: str) -> int:
    """按规范独立再写一遍公式，作为期望值来源。"""
    h = 5381
    for ch in skey:
        h = (h * 33 + ord(ch)) & 0x7FFFFFFF
    return h


@pytest.mark.parametrize(
    "skey",
    ["@abcdefg", "abc", "0", "a", "@v=1&x=2", "中文skey"],
)
def test_g_tk_matches_formula(skey):
    s = WeiyunSession({"skey": skey})
    assert s.g_tk == _expected_g_tk(skey)


def test_g_tk_known_value_hand_computed():
    # "abc": h=5381
    #   'a'(97) -> 5381*33+97 = 177670        -> 177670
    #   'b'(98) -> 177670*33+98 = 5863208     -> 5863208
    #   'c'(99) -> 5863208*33+99 = 193485963  -> 193485963
    assert 5381 * 33 + 97 == 177670
    assert 177670 * 33 + 98 == 5863208
    assert 5863208 * 33 + 99 == 193485963
    assert WeiyunSession({"skey": "abc"}).g_tk == 193485963


def test_g_tk_zero_when_no_skey():
    assert WeiyunSession({}).g_tk == 0
    assert WeiyunSession({"skey": ""}).g_tk == 0
    assert WeiyunSession({"skey": "abc"}).is_logged_in() is True
    assert WeiyunSession({}).is_logged_in() is False
    assert WeiyunSession({"skey": "   "}).is_logged_in() is False


# ------------------------------------------------------------------ 信封


def test_envelope_is_double_encoded(logged_in):
    p = WeiyunPlatform(logged_in)
    env = p.build_envelope(
        "WeiyunShareDirList", 12031, {"share_key": SHARE_KEY, "dir_key": ""}
    )

    assert isinstance(env["req_header"], str)
    assert isinstance(env["req_body"], str)

    hdr = json.loads(env["req_header"])
    assert isinstance(hdr, dict)
    assert hdr["cmd"] == 12031
    assert hdr["type"] == 1
    assert hdr["appid"] == 30151
    assert (hdr["major_version"], hdr["minor_version"], hdr["fix_version"]) == (3, 3, 3)
    # device_info 本身也是 JSON 字符串
    assert isinstance(json.loads(hdr["device_info"]), dict)

    body = json.loads(env["req_body"])
    assert isinstance(body, dict)
    inner = body["ReqMsg_body"]
    assert ".weiyun.WeiyunShareDirListMsgReq_body" in inner
    assert inner[".weiyun.WeiyunShareDirListMsgReq_body"]["share_key"] == SHARE_KEY
    tok = inner["ext_req_head"]["token_info"]
    assert tok == {"token_type": 3, "login_key_type": 1540}
    assert inner["ext_req_head"]["language_info"]["language_type"] == 2052


def test_dirlist_request_wire_format(patch_post, logged_in):
    rec = patch_post(
        {
            ("weiyunShareNoLogin", "WeiyunShareDirList"): [
                FakeResp(
                    ok(
                        {
                            "file_list": [
                                {
                                    "file_id": "f1",
                                    "filename": "a.bin",
                                    "file_size": 123,
                                    "file_sha": "ABCDEF",
                                    "pdir_key": "root",
                                }
                            ]
                        }
                    )
                )
            ]
        }
    )
    p = WeiyunPlatform(logged_in)
    files = p.list_files(SHARE_KEY, "root", recursive=False)

    assert [f.name for f in files] == ["a.bin"]
    assert files[0].sha1 == "abcdef"  # 统一小写
    assert files[0].size == 123

    call = rec.calls[0]
    # g_tk 必须来自 skey，且 Accept / Content-Type 必须齐（缺 Accept = 403 空 body）
    assert f"g_tk={logged_in.g_tk}" in call["url"]
    assert "refer=chrome_windows" in call["url"]
    assert call["headers"]["Accept"] == "application/json, text/plain, */*"
    assert call["headers"]["Content-Type"] == "application/json;charset=utf-8"

    hdr, body = rec.decoded(0)
    assert hdr["cmd"] == 12031
    inner = body["ReqMsg_body"][".weiyun.WeiyunShareDirListMsgReq_body"]
    assert inner["share_key"] == SHARE_KEY
    assert inner["dir_key"] == "root"
    assert "num" in inner and "sort" in inner


# ------------------------------------------------------------------ retcode


def test_retcode_190011_raises_not_logged_in(patch_post):
    rec = patch_post(
        {
            ("weiyunShare", "WeiyunShareBatchDownload"): [
                FakeResp(envelope({"retcode": 190011, "retmsg": "not login"}, {}))
            ],
            ("weiyunShareNoLogin", "WeiyunShareBatchDownload"): [
                FakeResp(envelope({"retcode": 190011, "retmsg": "not login"}, {}))
            ],
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    f = RemoteFile(file_id="fid1", name="x.zip", size=10, parent_key="pk1")

    with pytest.raises(NotLoggedIn) as ei:
        p.get_direct_link(SHARE_KEY, f)
    assert "190011" in str(ei.value)
    assert "not login" in str(ei.value)
    assert "登录" in ei.value.hint
    # 两条路都试过了
    assert rec.names() == [
        "weiyunShare/WeiyunShareBatchDownload",
        "weiyunShareNoLogin/WeiyunShareBatchDownload",
    ]


def test_no_skey_tries_authoritative_then_falls_back(patch_post):
    """无 skey：先打权威路径，失败后降级；降级成功则返回直链。"""
    rec = patch_post(
        {
            ("weiyunShare", "WeiyunShareBatchDownload"): [
                FakeResp(envelope({"retcode": 190011, "retmsg": "need login"}, {}))
            ],
            ("weiyunShareNoLogin", "WeiyunShareBatchDownload"): [
                FakeResp(
                    ok(
                        {
                            "file_list": [
                                {
                                    "https_download_url": "https://dl.weiyun.com/x?sig=1",
                                    "cookie_name": "FTN5K",
                                    "cookie_value": "4b29f45c",
                                    "file_sha": "deadbeef",
                                    "file_size": 999,
                                    "filename": "x.zip",
                                }
                            ]
                        }
                    )
                )
            ],
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    f = RemoteFile(file_id="fid1", name="x.zip", size=999, parent_key="pk1")

    link = p.get_direct_link(SHARE_KEY, f)

    assert isinstance(link, DirectLink)
    assert link.url == "https://dl.weiyun.com/x?sig=1"
    assert link.cookies == {"FTN5K": "4b29f45c"}  # 缺了下载会 401
    assert link.size == 999
    assert link.sha1 == "deadbeef"
    assert link.filename == "x.zip"
    assert rec.names() == [
        "weiyunShare/WeiyunShareBatchDownload",
        "weiyunShareNoLogin/WeiyunShareBatchDownload",
    ]
    # 无 skey → g_tk 0
    assert "g_tk=0" in rec.calls[0]["url"]

    hdr, body = rec.decoded(0)
    assert hdr["cmd"] == 12024
    inner = body["ReqMsg_body"][".weiyun.WeiyunShareBatchDownloadMsgReq_body"]
    assert inner["file_list"][0] == {
        "pdir_key": "pk1",
        "file_id": "fid1",
        "filename": "x.zip",
        "file_size": 999,
    }
    assert inner["download_type"] == 0
    assert inner["file_owner"] is None


def test_logged_in_uses_authoritative_first(patch_post, logged_in):
    rec = patch_post(
        {
            ("weiyunShare", "WeiyunShareBatchDownload"): [
                FakeResp(
                    ok(
                        {
                            "file_list": [
                                {
                                    "download_url": "http://dl/x",
                                    "cookie_name": "FTN5K",
                                    "cookie_value": "v",
                                }
                            ]
                        }
                    )
                )
            ]
        }
    )
    p = WeiyunPlatform(logged_in)
    link = p.get_direct_link(SHARE_KEY, RemoteFile(file_id="i", name="n", size=1))
    assert link.url == "http://dl/x"
    assert rec.names() == ["weiyunShare/WeiyunShareBatchDownload"]


def test_24203_error_message_keeps_raw_retcode(patch_post):
    patch_post(
        {
            ("weiyunShare", "WeiyunShareBatchDownload"): [
                FakeResp(envelope({"retcode": 24203, "retmsg": "服务繁忙"}, {}))
            ],
            ("weiyunShareNoLogin", "WeiyunShareBatchDownload"): [
                FakeResp(envelope({"retcode": 24203, "retmsg": "服务繁忙"}, {}))
            ],
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    with pytest.raises(NotLoggedIn) as ei:
        p.get_direct_link(SHARE_KEY, RemoteFile(file_id="i", name="n", size=1))
    text = str(ei.value)
    assert "24203" in text and "服务繁忙" in text  # 原始 retcode/retmsg 原文


def test_403_empty_body_is_not_reported_as_login(patch_post, logged_in):
    """有 skey 时 403 空 body 要提示「缺 Accept / g_tk 不匹配 / 风控」，不能说成要登录。"""
    patch_post(
        {
            ("weiyunShare", "WeiyunShareBatchDownload"): [
                FakeResp(None, status_code=403, text=""),
            ],
            ("weiyunShareNoLogin", "WeiyunShareBatchDownload"): [
                FakeResp(None, status_code=403, text=""),
            ],
        }
    )
    p = WeiyunPlatform(logged_in)
    # 单接口层面：403 空 body 是 DLinkError（不是 NotLoggedIn）
    with pytest.raises(DLinkError) as ei:
        p._call("weiyunShare", "WeiyunShareBatchDownload", 12024, {})
    assert "403" in ei.value.message
    assert "g_tk" in ei.value.hint and "风控" in ei.value.hint
    assert not isinstance(ei.value, NotLoggedIn)


# ------------------------------------------------------------------ 元数据/提取码


def test_get_share_info_free_of_login(patch_post):
    rec = patch_post(
        {
            ("weiyunShareNoLogin", "WeiyunShareView"): [
                FakeResp(
                    ok(
                        {
                            "share_key": SHARE_KEY,
                            "share_name": "我的分享",
                            "pdir_key": "rootdir",
                            "need_pwd": False,
                            "total_dir_count": 2,
                            "total_file_count": 5,
                            "share_uin": "12345",
                        }
                    )
                )
            ]
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    info = p.get_share_info(SHARE_KEY)
    assert info.platform == "weiyun"
    assert info.title == "我的分享"
    assert info.root_key == "rootdir"
    assert (info.total_dirs, info.total_files) == (2, 5)
    assert rec.names() == ["weiyunShareNoLogin/WeiyunShareView"]


def test_get_share_info_need_pwd_without_pwd_raises_auth_required(patch_post):
    patch_post(
        {
            ("weiyunShareNoLogin", "WeiyunShareView"): [
                FakeResp(ok({"share_key": SHARE_KEY, "need_pwd": True}))
            ]
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    with pytest.raises(AuthRequired):
        p.get_share_info(SHARE_KEY)


def test_pwd_passed_as_share_pwd_in_dirlist(patch_post):
    rec = patch_post(
        {
            ("weiyunShareNoLogin", "WeiyunShareDirList"): [
                FakeResp(ok({"file_list": []}))
            ]
        }
    )
    p = WeiyunPlatform(WeiyunSession({}))
    p.list_files(SHARE_KEY, "root", pwd="ab12", recursive=False)
    _, body = rec.decoded(0)
    inner = body["ReqMsg_body"][".weiyun.WeiyunShareDirListMsgReq_body"]
    assert inner["share_pwd"] == "ab12"


# ------------------------------------------------------------------ 递归/环


def test_list_files_recursive_dedup_and_depth_guard(patch_post):
    """A → B → A 的目录环：dir_key 去重后不会死循环。"""
    root_page = {
        "dir_list": [
            {"dir_key": "A", "dir_name": "a"},
            {"dir_key": "B", "dir_name": "b"},
        ],
        "file_list": [{"file_id": "f0", "filename": "top.txt", "file_size": 1}],
    }
    a_page = {
        "dir_list": [{"dir_key": "B", "dir_name": "b"}],  # 回到已访问的 B
        "file_list": [{"file_id": "f1", "filename": "a.txt", "file_size": 2}],
    }
    b_page = {
        "dir_list": [{"dir_key": "A", "dir_name": "a"}],  # 再回 A
        "file_list": [{"file_id": "f2", "filename": "b.txt", "file_size": 3}],
    }

    class Router(Recorder):
        def __call__(self, url, headers=None, data=None, timeout=None, cookies=None, **kw):
            self.calls.append({"url": url, "headers": headers, "data": data})
            _, body = self.decoded(len(self.calls) - 1)
            dk = body["ReqMsg_body"][".weiyun.WeiyunShareDirListMsgReq_body"]["dir_key"]
            page = {"root": root_page, "A": a_page, "B": b_page}[dk]
            return FakeResp(ok(page))

    rec = Router({})
    import requests

    orig = requests.Session.post
    requests.Session.post = rec
    try:
        p = WeiyunPlatform(WeiyunSession({}))
        files = p.list_files(SHARE_KEY, "root", recursive=True)
    finally:
        requests.Session.post = orig

    names = sorted(f.name for f in files)
    assert names == ["a.txt", "b.txt", "top.txt"]
    # 环被剪掉：每个 dir_key 只请求一次
    assert len(rec.calls) == 3


def test_depth_guard_raises(patch_post, monkeypatch):
    """若每层都产生新 dir_key（无限增长），到 MAX_DEPTH 必须报错而不是跑飞。"""
    counter = {"n": 0}

    class Gen(Recorder):
        def __call__(self, url, headers=None, data=None, timeout=None, cookies=None, **kw):
            counter["n"] += 1
            self.calls.append({"url": url, "headers": headers, "data": data})
            child = f"d{counter['n']}"
            return FakeResp(ok({"dir_list": [{"dir_key": child, "dir_name": child}]}))

    import requests

    orig = requests.Session.post
    requests.Session.post = Gen({})
    try:
        p = WeiyunPlatform(WeiyunSession({}))
        with pytest.raises(DLinkError) as ei:
            p.list_files(SHARE_KEY, "root", recursive=True)
    finally:
        requests.Session.post = orig

    assert "环" in str(ei.value) or "深度" in str(ei.value)
    assert counter["n"] <= 40  # 远小于无限
