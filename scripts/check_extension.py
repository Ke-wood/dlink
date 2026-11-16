#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扩展静态自检：manifest 完整性 / 版本一致 / JS 语法 / 隐私扫描。

为什么需要：
  · manifest 里写错一个文件名，浏览器只会给一句含糊的加载错误；
  · 版本号散落在 manifest / pyproject / __init__ 三处，不同步时
    「用户装的是不是新版」就变成猜谜，发布前也容易打错 tag；
  · 仓库里不允许出现个人路径/账号（历史清理过一次），必须有机器检查兜底。

用法：
    python scripts/check_extension.py          # 全部检查
    python scripts/check_extension.py -v       # 打印每个 JS 文件
    python scripts/check_extension.py --no-node  # 跳过 node 语法检查

退出码 0 = 全过；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"

# 仓库里不允许出现的个人标识（历史上清理过，防止再漏进来）
# 注意：这里用字符串拼接而不是写成完整字面量 —— 否则黑名单本身就成了泄漏。
PRIVACY_BLACKLIST = (
    "klk" + "lk",
    "zz" + "av",
    "vr" + "q1",
    "d:" + "/plan",
    "d:" + "\\plan",
)
SCAN_EXTS = {".py", ".js", ".json", ".md", ".html", ".css", ".toml", ".txt", ".spec", ".yml"}
SKIP_DIRS = {".git", "build", "dist", ".vendor", "node_modules", "__pycache__",
             ".pytest_cache", "tmp_tests", ".tmp", "probe"}

errors: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def load_manifest() -> dict:
    path = EXT / "manifest.json"
    if not path.exists():
        fail("extension/manifest.json 不存在")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"manifest.json 不是合法 JSON：{e}")
        return {}


def check_permissions(man: dict) -> None:
    perms = set(man.get("permissions") or [])
    # 接管功能依赖 downloads/notifications；少了不会报错，只会静默失效
    if (EXT / "intercept.js").exists():
        for need in ("downloads", "storage"):
            if need not in perms:
                fail(f"有 intercept.js 但 manifest 缺少 \"{need}\" 权限")
        if not man.get("optional_host_permissions"):
            notes.append("未声明 optional_host_permissions（读取任意站点 Cookie 的能力不可用）")
    if (EXT / "options.html").exists():
        ui = man.get("options_ui") or {}
        if not ui.get("page"):
            fail("有 options.html 但 manifest 没有 options_ui")
    if "alarms" not in perms:
        fail("缺少 \"alarms\"（MV3 service worker 会被回收，续链轮询必须用 alarms）")


def check_files(man: dict) -> None:
    refs: list[str] = []
    bg = man.get("background") or {}
    if bg.get("service_worker"):
        refs.append(bg["service_worker"])
    action = man.get("action") or {}
    if action.get("default_popup"):
        refs.append(action["default_popup"])
    ui = man.get("options_ui") or {}
    if ui.get("page"):
        refs.append(ui["page"])
    for cs in man.get("content_scripts") or []:
        refs.extend(cs.get("js") or [])
        refs.extend(cs.get("css") or [])
    for war in man.get("web_accessible_resources") or []:
        refs.extend(war.get("resources") or [])
    for size, name in (man.get("icons") or {}).items():
        refs.append(name)
    for rr in (man.get("declarative_net_request") or {}).get("rule_resources") or []:
        if rr.get("path"):
            refs.append(rr["path"])

    for rel in sorted(set(refs)):
        if "*" in rel:  # 通配资源（如 icons/*）不做存在性检查
            continue
        if not (EXT / rel).exists():
            fail(f"manifest 引用了不存在的文件：extension/{rel}")

    # 页面里 module 引用的脚本（import 语句的静态检查）
    for page in ("popup.html", "prompt.html", "options.html"):
        p = EXT / page
        if not p.exists():
            fail(f"缺少 {page}")
            continue
        html = p.read_text(encoding="utf-8")
        for m in re.finditer(r'<script[^>]+src="([^"]+)"', html):
            ref = m.group(1)
            if not (EXT / ref).exists():
                fail(f"{page} 引用了不存在的脚本：{ref}")


def check_imports() -> None:
    """ES module 的相对 import 目标必须存在（写错路径只有运行时才炸）。"""
    for js in sorted(EXT.glob("*.js")):
        text = js.read_text(encoding="utf-8")
        for m in re.finditer(r"from\s+['\"](\.[^'\"]+)['\"]", text):
            target = (js.parent / m.group(1)).resolve()
            if not target.exists():
                fail(f"{js.name} import 的 {m.group(1)} 不存在")


def check_versions(man: dict) -> None:
    v_manifest = man.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", v_manifest):
        fail(f"manifest version 不是 x.y.z：{v_manifest!r}")

    pyproj = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproj, re.M)
    v_pyproject = m.group(1) if m else ""
    if v_pyproject != v_manifest:
        fail(f"版本不一致：manifest={v_manifest} pyproject={v_pyproject}")

    init = (ROOT / "src" / "dlink" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    v_pkg = m.group(1) if m else ""
    if v_pkg != v_manifest:
        fail(f"版本不一致：manifest={v_manifest} dlink/__init__={v_pkg}")

    # server.py 的 VERSION 必须取自包版本。硬编码过一次，
    # 结果 /api/health 永远报 0.1.0，发布后无法确认用户装的是哪一版。
    srv = (ROOT / "src" / "dlink" / "server.py").read_text(encoding="utf-8")
    if re.search(r'^VERSION\s*=\s*"', srv, re.M):
        fail("server.py 的 VERSION 是硬编码字符串，应改为 VERSION = __version__")

    # injected.js 的 BUILD 决定「页面里的旧对象要不要被新注入替换」，
    # 与 manifest 不同步会出现「更新了扩展但功能还是旧的」。
    inj = (EXT / "injected.js").read_text(encoding="utf-8")
    m = re.search(r"const BUILD = '([^']+)'", inj)
    v_build = m.group(1) if m else ""
    if v_build != v_manifest:
        fail(f"injected.js 的 BUILD={v_build} 与 manifest={v_manifest} 不一致")


def check_js_syntax(verbose: bool) -> None:
    node = shutil.which("node")
    if not node:
        notes.append("未找到 node，跳过 JS 语法检查")
        return
    files = sorted(EXT.glob("*.js"))
    with tempfile.TemporaryDirectory() as td:
        for js in files:
            # node --check 默认按 CommonJS 解析；扩展脚本是 ESM，
            # 所以复制成 .mjs 再检查（严格模式，正好能 catch 更多问题）
            tmp = Path(td) / (js.stem + ".mjs")
            shutil.copyfile(js, tmp)
            r = subprocess.run([node, "--check", str(tmp)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                fail(f"{js.name} 语法错误：{(r.stderr or '').strip().splitlines()[-1:]}")
            elif verbose:
                print(f"    ok  {js.name}")


def check_privacy() -> None:
    hits: list[str] = []
    self_path = Path(__file__).resolve()  # 黑名单本身就在这个文件里
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCAN_EXTS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() == self_path:
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        for bad in PRIVACY_BLACKLIST:
            if bad in text:
                hits.append(f"{path.relative_to(ROOT)} 含 {bad!r}")
    for h in sorted(set(hits)):
        fail("隐私： " + h)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--no-node", action="store_true")
    args = ap.parse_args()

    print("== DLink 扩展自检 ==")
    man = load_manifest()
    if man:
        print(f"  manifest: {man.get('name')} v{man.get('version')}")
        check_permissions(man)
        check_files(man)
        check_imports()
        check_versions(man)
    if not args.no_node:
        check_js_syntax(args.verbose)
    check_privacy()

    for n in notes:
        print(f"  ! {n}")
    if errors:
        print("\n失败项：")
        for e in errors:
            print(f"  ✗ {e}")
        return 1
    print(f"\n全部通过（{len(list(EXT.glob('*.js')))} 个 JS 文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
