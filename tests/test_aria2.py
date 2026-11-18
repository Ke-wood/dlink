"""aria2c 命令解析测试。"""

from __future__ import annotations

from dlink.aria2 import looks_like_aria2, parse_aria2

BAIDU = ("aria2c \"https://d.pcs.baidu.com/file/abc?fid=1&sign=FDtAERK-x&access_token=123.abc\" "
         "--out \"PC.7z\" --header \"User-Agent:pan.baidu.com\"")


def test_single_command_basic() -> None:
    items = parse_aria2(BAIDU)
    assert len(items) == 1
    it = items[0]
    assert it.url.startswith("https://d.pcs.baidu.com/file/abc")
    assert it.filename == "PC.7z"
    assert it.headers == {"User-Agent": "pan.baidu.com"}
    assert it.cookies == {}


def test_url_with_query_kept_whole() -> None:
    items = parse_aria2(BAIDU)
    assert "access_token=123.abc" in items[0].url
    assert "&sign=" in items[0].url


def test_cookie_header_becomes_cookies() -> None:
    cmd = ('aria2c "https://x.test/f.bin" '
           '--header "Cookie: FTN5K=abc; uin=42"')
    it = parse_aria2(cmd)[0]
    assert it.cookies == {"FTN5K": "abc", "uin": "42"}
    assert "Cookie" not in it.headers


def test_referer_and_user_agent_shortcuts() -> None:
    cmd = ('aria2c "https://x.test/f.bin" --referer "https://pan.baidu.com/" '
           '-U "netdisk"')
    it = parse_aria2(cmd)[0]
    assert it.headers["Referer"] == "https://pan.baidu.com/"
    assert it.headers["User-Agent"] == "netdisk"


def test_multiple_headers() -> None:
    cmd = ('aria2c "https://x.test/f.bin" -H "A: 1" -H "B: 2"')
    it = parse_aria2(cmd)[0]
    assert it.headers == {"A": "1", "B": "2"}


def test_workers_from_x_and_split() -> None:
    assert parse_aria2('aria2c "https://x.test/a" -x 6')[0].workers == 6
    assert parse_aria2('aria2c "https://x.test/a" -s 4')[0].workers == 4
    # -x 优先于 -s
    assert parse_aria2('aria2c "https://x.test/a" -s 2 -x 8')[0].workers == 8


def test_multiple_commands_one_per_line() -> None:
    text = ('aria2c "https://x.test/a.bin" --out "a.bin"\n'
            'aria2c "https://x.test/b.bin" --out "b.bin"\n')
    items = parse_aria2(text)
    assert [i.filename for i in items] == ["a.bin", "b.bin"]


def test_backslash_continuation() -> None:
    text = 'aria2c "https://x.test/a.bin" \\\n  --out "a.bin" \\\n  -x 8'
    items = parse_aria2(text)
    assert len(items) == 1
    assert items[0].filename == "a.bin"
    assert items[0].workers == 8


def test_dir_only_affects_hint_not_name() -> None:
    cmd = 'aria2c "https://x.test/dir/file.zip" --dir "D:\\out\\x"'
    it = parse_aria2(cmd)[0]
    assert it.filename == "file.zip"
    assert it.dir_hint == "D:\\out\\x"


def test_out_with_path_takes_basename() -> None:
    cmd = 'aria2c "https://x.test/a" --out "C:\\tmp\\real.7z"'
    assert parse_aria2(cmd)[0].filename == "real.7z"


def test_plain_urls_still_work() -> None:
    items = parse_aria2("https://a.test/1.zip\nhttps://a.test/2.zip")
    assert [i.filename for i in items] == ["1.zip", "2.zip"]
    assert all(not i.headers for i in items)


def test_unknown_option_does_not_eat_url() -> None:
    cmd = 'aria2c --some-unknown-flag "https://x.test/keep.bin"'
    items = parse_aria2(cmd)
    assert len(items) == 1
    assert items[0].url == "https://x.test/keep.bin"


def test_option_with_equals_from() -> None:
    cmd = 'aria2c --out=eq.bin --header="User-Agent:ua" "https://x.test/f"'
    it = parse_aria2(cmd)[0]
    assert it.filename == "eq.bin"
    assert it.headers["User-Agent"] == "ua"


def test_ignores_dangerous_options() -> None:
    # --input-file / --load-cookies 不能被当成值解析、更不能执行
    cmd = ('aria2c "https://x.test/a.bin" --input-file "C:\\evil.txt" '
           '--load-cookies "C:\\c.txt"')
    it = parse_aria2(cmd)[0]
    assert it.url == "https://x.test/a.bin"
    assert it.dir_hint == ""


def test_filename_falls_back_to_url_path() -> None:
    it = parse_aria2('aria2c "https://x.test/dir/%E6%B5%8B%E8%AF%95.7z?sig=1"')[0]
    assert it.filename == "测试.7z"


def test_looks_like_aria2() -> None:
    assert looks_like_aria2(BAIDU)
    assert looks_like_aria2('"https://x/y" --out a')
    assert not looks_like_aria2("https://a.test/1.zip")
    assert not looks_like_aria2("")


def test_empty_input() -> None:
    assert parse_aria2("") == []
    assert parse_aria2("   \n  ") == []


def test_to_dict_shape() -> None:
    d = parse_aria2(BAIDU)[0].to_dict()
    assert d["url"].startswith("https://")
    assert d["filename"] == "PC.7z"
    assert d["headers"] == {"User-Agent": "pan.baidu.com"}
    assert "cookies" not in d
