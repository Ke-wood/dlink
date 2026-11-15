"""从 Chromium 系浏览器（Edge/Chrome）的 Cookies 库中解密指定域名的 cookie。

Windows 上 Chromium 的加密方案：
  - 传统：v10 前缀 + AES-256-GCM，key = DPAPI(Local State 里的 encrypted_key)
  - 新版（Edge 127+ / Chrome 127+）：App-Bound Encryption（ABE），key 绑定浏览器进程，
    普通进程解不开 —— 这是硬限制，遇到就直接报错说明，不要硬试。

只读：先复制到临时文件再打开，避免锁住原库（浏览器运行时库文件被独占）。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None

try:
    import win32crypt
except ImportError:  # pragma: no cover
    win32crypt = None


EDGE = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/User Data"
CHROME = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"


class CookieDecryptError(Exception):
    pass


def _find_profile(root: Path, prefer: str | None = None) -> Path | None:
    if not root.exists():
        return None
    if prefer:
        p = root / prefer
        if (p / "Network/Cookies").exists():
            return p
    # 优先 Default，其次第一个有 Cookies 的
    cand = root / "Default"
    if (cand / "Network/Cookies").exists():
        return cand
    for p in sorted(root.iterdir()):
        if (p / "Network/Cookies").exists():
            return p
    return None


def _get_master_key(profile_root: Path) -> bytes:
    """从 Local State 取并解出 AES key。"""
    ls = profile_root.entity if False else profile_root / "Local State"
    if not ls.exists():
        raise CookieDecryptError(f"找不到 Local State: {ls}")
    data = json.loads(ls.read_text(encoding="utf-8"))
    enc = data.get("os_crypt", {}).get("encrypted_key")
    if not enc:
        raise CookieDecryptError("Local State 里没有 os_crypt.encrypted_key")
    import base64
    raw = base64.b64decode(enc)
    if raw[:5] != b"DPAPI":
        raise CookieDecryptError(f"encrypted_key 前缀异常: {raw[:5]!r}")
    if win32crypt is None:
        raise CookieDecryptError("需要 pywin32 的 win32crypt 来解 DPAPI")
    try:
        return win32crypt.CryptUnprotectData(raw[5:], None, None, None, 0)[1]
    except Exception as e:
        raise CookieDecryptError(
            f"DPAPI 解密失败: {e}\n"
            "  通常意味着这是 App-Bound Encryption（Edge 127+），key 绑定浏览器进程，"
            "外部进程无法解密。"
        ) from e


def _decrypt_value(blob: bytes, key: bytes) -> str:
    if blob is None:
        return ""
    if blob[:3] == b"v20":
        raise CookieDecryptError("cookie 是 v20（App-Bound Encryption），外部进程无法解密")
    if blob[:3] == b"v10":
        nonce, payload = blob[3:15], blob[15:]
        if AESGCM is None:
            raise CookieDecryptError("需要 cryptography 解 AES-GCM")
        try:
            return AESGCM(key).decrypt(nonce, payload, None).decode("utf-8", "replace")
        except Exception:
            # 老版本是 AES-CBC，容错回退
            raise
    # 极老的明文
    try:
        return blob.decode("utf-8")
    except Exception:
        return ""


def _query_rows(db: Path):
    """读 cookie 表。浏览器运行时文件被独占，所以按顺序尝试：
    1) immutable 只读 URI（不复制，最省事）
    2) 复制到临时文件（含 -wal/-shm）再读
    注意：Windows 上正在运行的 Chromium 会锁文件，1 常失败。
    """
    sql = "SELECT host_key, name, value, encrypted_value FROM cookies"

    # 方式 1：只读 URI
    try:
        uri = f"file:{db.as_posix()}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        pass

    # 方式 2：复制（用二进制读写绕过 CopyFile2 的独占检查）
    import time
    tmpdir = Path(tempfile.mkdtemp(prefix="ck_"))
    tmp = tmpdir / "Cookies"
    try:
        with open(db, "rb") as src, open(tmp, "wb") as dst:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                dst.write(b)
        # WAL 里的新 cookie 也要带上
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                try:
                    with open(side, "rb") as s2, open(tmpdir / (db.name + suffix), "wb") as d2:
                        shutil.copyfileobj(s2, d2)
                except OSError:
                    pass
        con = sqlite3.connect(str(tmp), timeout=5)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error) as e:
        raise CookieDecryptError(
            f"无法读取 cookie 库（浏览器正在运行并锁定文件）: {e}\n"
            "  解决：完全退出 Edge 后重跑，或用 --root 指定别的浏览器 profile"
        ) from e
    finally:
        time.sleep(0)
        shutil.rmtree(tmpdir, ignore_errors=True)


def read_cookies(domain_substr: str, root: Path | None = None,
                 profile: str | None = None) -> dict:
    """返回 {name: value}，只保留 domain 包含 domain_substr 的 cookie。"""
    roots = [root] if root else [EDGE, CHROME]
    for r in roots:
        if not r.exists():
            continue
        prof = _find_profile(r, profile)
        if not prof:
            continue
        db = prof / "Network/Cookies"
        key = _get_master_key(r)

        rows = _query_rows(db)

        out, failed = {}, 0
        for host, name, value, enc in rows:
            if domain_substr.lower() not in (host or "").lower():
                continue
            if value:
                out[name] = value
                continue
            try:
                v = _decrypt_value(enc, key)
                if v:
                    out[name] = v
            except CookieDecryptError:
                raise
            except Exception:
                failed += 1
        if out:
            print(f"[{r.name}] profile={prof.name} 匹配 {len(out)} 个 cookie，"
                  f"{failed} 个解密失败", file=sys.stderr)
            return out
    raise CookieDecryptError(f"没有找到包含 '{domain_substr}' 的 cookie")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="baidu")
    ap.add_argument("--out", default=".private/baidu_cookies.json")
    ap.add_argument("--profile", default=None)
    a = ap.parse_args()

    try:
        ck = read_cookies(a.domain, profile=a.profile)
    except CookieDecryptError as e:
        print(f"失败: {e}", file=sys.stderr)
        sys.exit(1)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(ck, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"写入 {a.out}：{len(ck)} 个 cookie；BDUSS={'有' if 'BDUSS' in ck else '无'}"
          f"；STOKEN={'有' if 'STOKEN' in ck else '无'}")


if __name__ == "__main__":
    main()
