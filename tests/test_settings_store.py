"""设置持久化测试（不联网）。

核心回归点：
    用户改了「保存目录」→ 写入磁盘 → 重启后仍然是新目录。
    这曾经是个真 bug（只存内存，重启还原）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dlink.settings_store import (  # noqa: E402
    DEFAULTS,
    load_settings,
    save_settings,
    settings_path,
)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """每个测试用独立的设置文件，避免污染真实配置。"""
    p = tmp_path / "settings.json"
    monkeypatch.setenv("DLINK_SETTINGS_FILE", str(p))
    yield p


def test_defaults_when_no_file(isolated_settings):
    assert not isolated_settings.exists()
    s = load_settings()
    assert s["save_dir"] == DEFAULTS["save_dir"]
    assert s["max_concurrent"] == 3


def test_save_then_load_roundtrip(isolated_settings, tmp_path):
    """★ 核心：改保存目录 → 落盘 → 重新读回来还是新值。"""
    new_dir = str(tmp_path / "我的下载")
    save_settings({"save_dir": new_dir})

    assert isolated_settings.exists()
    s = load_settings()
    assert s["save_dir"] == new_dir


def test_save_dir_survives_fresh_load(isolated_settings, tmp_path):
    """模拟"重启"：不传任何状态，直接重新 load。"""
    target = str(tmp_path / "另一个目录")
    save_settings({"save_dir": target})
    # 这里没有任何内存缓存，等价于进程重启后重新读磁盘
    assert load_settings()["save_dir"] == target


def test_partial_update_keeps_other_keys(isolated_settings, tmp_path):
    """只改一个键，不能把其他键冲掉。"""
    save_settings({"save_dir": str(tmp_path / "a"), "max_concurrent": 5,
                   "verify": False})
    save_settings({"save_dir": str(tmp_path / "b")})
    s = load_settings()
    assert s["save_dir"] == str(tmp_path / "b")
    assert s["max_concurrent"] == 5      # 未被冲掉
    assert s["verify"] is False


def test_bool_coercion_from_string(isolated_settings):
    """GUI/JSON 可能传字符串，要能正确转成布尔。"""
    save_settings({"verify": "false", "open_folder_when_done": "true"})
    s = load_settings()
    assert s["verify"] is False
    assert s["open_folder_when_done"] is True


def test_int_coercion_and_floor(isolated_settings):
    save_settings({"max_concurrent": "7"})
    assert load_settings()["max_concurrent"] == 7
    # 非法值回退
    save_settings({"max_concurrent": "abc"})
    assert load_settings()["max_concurrent"] == DEFAULTS["max_concurrent"]
    # 下界保护
    save_settings({"max_concurrent": 0})
    assert load_settings()["max_concurrent"] >= 1


def test_unknown_keys_ignored(isolated_settings):
    save_settings({"evil_key": "DROP TABLE", "save_dir": "x"})
    raw = json.loads(isolated_settings.read_text(encoding="utf-8"))
    inner = raw.get("settings", raw)
    assert "evil_key" not in inner


def test_corrupted_file_falls_back(isolated_settings):
    """配置文件坏了不能让程序起不来。"""
    isolated_settings.write_text("{ this is not json", encoding="utf-8")
    s = load_settings()
    assert s["save_dir"] == DEFAULTS["save_dir"]


def test_file_format_is_versioned(isolated_settings):
    save_settings({"save_dir": "x"})
    raw = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert "settings" in raw
