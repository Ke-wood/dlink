"""百度网盘 OAuth 授权（设备码模式）。

为什么要授权：
    百度 CDN 的直链有两套签名（详见 docs/PLATFORMS.md §9）：
      · 网页 `api/filemetas` → sign 前缀 `FDtAER-`，**必须**带 BDUSS cookie，
        且只能在网页自己那一次下载流程里用，外部请求一律 31362 sign error；
      · xpan `rest/2.0/xpan/multimedia?method=filemetas` → sign 前缀 `FDtAERK-`，
        带 access_token，**不需要 cookie**，302 → *.baidupcs.com → 206，可以离线用。
    所以想「像微云那样全自动」，就必须走第二套，也就必须先拿到 access_token。

授权流程（设备码，用户只需操作一次）：
    1. POST /oauth/2.0/device/code        → 拿到 user_code 与授权页
    2. 用户在浏览器打开授权页、输入 user_code 并同意
    3. 轮询 POST /oauth/2.0/token (grant_type=device_token) → 拿到 access_token + refresh_token
    4. 之后 access_token 过期就用 refresh_token 续，用户不再需要操作

凭据存放（**都不进仓库**）：
    %LOCALAPPDATA%/dlink/baidu_app.json    ← AppKey / SecretKey（用户自己在开放平台申请）
    %LOCALAPPDATA%/dlink/baidu_token.json  ← access_token / refresh_token
环境变量可覆盖（测试用）：DLINK_BAIDU_APP / DLINK_BAIDU_TOKEN
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

OAUTH = "https://openapi.baidu.com/oauth/2.0"
SCOPE = "basic,netdisk"
TIMEOUT = 20


class BaiduAuthError(RuntimeError):
    """授权失败（凭据缺失、被拒、超时等）。"""


# ---------------- 配置与 token 文件 ----------------

def _base_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "dlink"


def app_path() -> Path:
    override = os.environ.get("DLINK_BAIDU_APP")
    return Path(override).expanduser() if override else _base_dir() / "baidu_app.json"


def token_path() -> Path:
    override = os.environ.get("DLINK_BAIDU_TOKEN")
    return Path(override).expanduser() if override else _base_dir() / "baidu_token.json"


def load_app() -> dict:
    """读开放平台凭据；没有就抛错并说清该填什么。"""
    p = app_path()
    if not p.exists():
        raise BaiduAuthError(
            f"没有找到百度应用凭据：{p}\n"
            "请在 https://pan.baidu.com/union/console/applist 创建应用后，"
            '把 {"app_id": "...", "app_key": "...", "secret_key": "..."} 写进该文件'
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BaiduAuthError(f"凭据文件不是合法 JSON：{p}（{e}）") from None
    for k in ("app_key", "secret_key"):
        if not data.get(k):
            raise BaiduAuthError(f"凭据文件缺少 {k}：{p}")
    return data


def load_token() -> Optional[dict]:
    p = token_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_token(tok: dict) -> Path:
    """原子写，并补一个 expires_at（便于判断要不要续期）。"""
    data = dict(tok)
    if data.get("expires_in") and not data.get("expires_at"):
        data["expires_at"] = int(time.time()) + int(data["expires_in"])
    p = token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


# ---------------- 设备码授权 ----------------


def request_device_code(app: Optional[dict] = None) -> dict:
    """第 1 步：申请设备码。返回含 user_code / verification_url 的字典。"""
    app = app or load_app()
    r = requests.post(f"{OAUTH}/device/code", data={
        "client_id": app["app_key"],
        "response_type": "device_code",
        "scope": SCOPE,
    }, timeout=TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        raise BaiduAuthError(f"设备码接口返回非 JSON：{r.status_code} {r.text[:200]}") from None
    if "device_code" not in j:
        raise BaiduAuthError(f"设备码接口返回异常：{j}")
    j.setdefault("verification_url", "https://openapi.baidu.com/device")
    return j


def poll_once(app: dict, device_code: str) -> Optional[dict]:
    """第 3 步的单次轮询：还没授权返回 None，成功返回 token。"""
    r = requests.post(f"{OAUTH}/token", data={
        "grant_type": "device_token",
        "code": device_code,
        "client_id": app["app_key"],
        "client_secret": app["secret_key"],
    }, timeout=TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        raise BaiduAuthError(f"token 接口返回非 JSON：{r.status_code} {r.text[:200]}") from None
    if j.get("access_token"):
        return j
    err = str(j.get("error") or "")
    if err in ("authorization_pending", "authorization_waiting"):
        return None          # 用户还没点同意，继续等
    if err in ("expired_token", "invalid_grant"):
        raise BaiduAuthError("设备码已失效，请重新发起授权")
    if err:
        raise BaiduAuthError(f"授权失败：{err} {j.get('error_description') or ''}".strip())
    raise BaiduAuthError(f"授权返回异常：{j}")


def poll_for_token(app: dict, device_code: str,
                   interval: int = 5, timeout: int = 300) -> dict:
    """第 3 步：轮询到用户授权为止（默认最多 5 分钟）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tok = poll_once(app, device_code)
        if tok:
            return tok
        time.sleep(max(2, int(interval or 5)))
    raise BaiduAuthError(f"等待授权超时（{timeout}s）。请重来一次并用浏览器完成授权")


def refresh(app: dict, refresh_token: str) -> dict:
    """用 refresh_token 换新的 access_token。"""
    r = requests.post(f"{OAUTH}/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app["app_key"],
        "client_secret": app["secret_key"],
    }, timeout=TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        raise BaiduAuthError(f"续期返回非 JSON：{r.status_code}") from None
    if not j.get("access_token"):
        raise BaiduAuthError(
            f"续期失败：{j.get('error')} {j.get('error_description') or ''}".strip())
    return j


# ---------------- 对外：拿一个可用的 access_token ----------------


def is_expired(tok: dict, leeway: int = 600) -> bool:
    """提前 10 分钟算过期，避免边界上刚好失效。"""
    exp = tok.get("expires_at")
    if not exp:
        return False
    return time.time() > float(exp) - leeway


def get_access_token(force_refresh: bool = False) -> str:
    """返回可用 access_token；过期则自动续期。

    没有 token 时抛 BaiduAuthError（调用方应提示用户去设置页授权）。
    """
    app = load_app()
    tok = load_token()
    if not tok or not tok.get("refresh_token"):
        raise BaiduAuthError("还没有授权过百度网盘，请先完成一次设备码授权")
    if force_refresh or is_expired(tok):
        new = refresh(app, tok["refresh_token"])
        # 续期响应里可能不带 refresh_token，保留旧的
        new.setdefault("refresh_token", tok["refresh_token"])
        save_token(new)
        return new["access_token"]
    return tok["access_token"]


def authorized() -> bool:
    tok = load_token()
    return bool(tok and tok.get("refresh_token"))


# ---------------- 命令行：跑一次授权 ----------------

def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="百度网盘设备码授权")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--status", action="store_true", help="只看当前是否已授权")
    a = ap.parse_args()

    if a.status:
        tok = load_token()
        if not tok:
            print("未授权")
            return 1
        left = int(float(tok.get("expires_at") or 0) - time.time())
        print(f"已授权｜access_token 剩余 {left}s｜refresh_token "
              f"{'有' if tok.get('refresh_token') else '无'}")
        return 0

    app = load_app()
    dc = request_device_code(app)
    print("\n请在浏览器打开下面地址，输入授权码并同意：")
    print(f"  地址  : {dc.get('verification_url')}")
    print(f"  授权码: {dc.get('user_code')}")
    print(f"\n有效期 {dc.get('expires_in', '?')} 秒，正在等待授权…（Ctrl+C 取消）")
    tok = poll_for_token(app, dc["device_code"],
                         interval=int(dc.get("interval") or 5), timeout=a.timeout)
    p = save_token(tok)
    print(f"\n授权成功，token 已保存：{p}")
    print(f"  scope: {tok.get('scope')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
