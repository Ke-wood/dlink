"""发布到 GitHub Release（用 API，不需要 gh CLI）。

用法:
    python scripts/release.py --tag v0.1.0 --notes RELEASE_NOTES.md

会做三件事：
    1. 创建/复用 tag 对应的 release
    2. 上传 dist/DLink.exe
    3. 打印 release 地址

token 从 ~/.git-credentials 读（不打印）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "Ke-wood/dlink"


def get_token() -> str:
    p = Path(os.path.expanduser("~/.git-credentials"))
    s = p.read_text(encoding="utf-8")
    m = re.search(r"https://[^:]+:([^@]+)@github\.com", s)
    if not m:
        raise SystemExit("找不到 GitHub token（~/.git-credentials）")
    return m.group(1)


def api(token: str, method: str, path: str, payload=None) -> dict:
    data = None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "dlink-release",
    }
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://api.github.com{path}",
                                 data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        return {"_error": e.code, "_body": body}


def upload_asset(token: str, upload_url: str, file: Path) -> dict:
    """上传附件（走 uploads.github.com）。"""
    # upload_url 形如 https://uploads.github.com/repos/x/y/releases/1/assets{?name,label}
    base = upload_url.split("{")[0]
    url = f"{base}?name={urllib.parse.quote(file.name)}"
    data = file.read_bytes()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"token {token}",
        "Content-Type": "application/octet-stream",
        "User-Agent": "dlink-release",
        "Content-Length": str(len(data)),
    })
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:400]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--notes", default="RELEASE_NOTES.md")
    ap.add_argument("--file", nargs="+", default=["dist/DLink.exe"],
                    help="要上传的附件（可多个）")
    ap.add_argument("--draft", action="store_true")
    a = ap.parse_args()

    token = get_token()
    files = [Path(f) for f in a.file]
    for f in files:
        if not f.exists():
            raise SystemExit(f"找不到 {f}，先跑 PyInstaller / pack_extension.py")

    notes = Path(a.notes).read_text(encoding="utf-8") if Path(a.notes).exists() else ""

    # 1. 查是否已有 release
    existing = api(token, "GET", f"/repos/{REPO}/releases/tags/{a.tag}")
    if existing.get("_error") == 404:
        rel = api(token, "POST", f"/repos/{REPO}/releases", {
            "tag_name": a.tag,
            "name": a.tag,
            "body": notes,
            "draft": a.draft,
            "prerelease": False,
        })
        if rel.get("_error"):
            print("创建 release 失败:", rel["_error"], rel["_body"][:300])
            return 1
        print("已创建 release:", rel["html_url"])
    else:
        rel = existing
        print("release 已存在:", rel.get("html_url"))
        if notes:
            upd = api(token, "PATCH", f"/repos/{REPO}/releases/{rel['id']}",
                      {"body": notes})
            print("  说明已更新" if not upd.get("_error")
                  else f"  说明更新失败: {upd.get('_error')}")

    # 2. 上传（先删同名旧附件）
    assets = api(token, "GET", f"/repos/{REPO}/releases/{rel['id']}/assets")
    existing_assets = assets if isinstance(assets, list) else []
    for f in files:
        for asset in existing_assets:
            if asset.get("name") == f.name:
                api(token, "DELETE", f"/repos/{REPO}/releases/assets/{asset['id']}")
                print("  已删除旧附件:", f.name)
        print(f"上传 {f.name}（{f.stat().st_size / 1e6:.1f} MB）…")
        up = upload_asset(token, rel["upload_url"], f)
        if up.get("_error"):
            print("上传失败:", up["_error"], up.get("_body", "")[:300])
            return 1
        print("上传成功:", up.get("browser_download_url"))
    print("\nRelease 地址:", rel["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
