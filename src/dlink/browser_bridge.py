"""浏览器桥：从真实浏览器补齐工具拿不到的东西。

两个职责（对应交接文档 §2 第 1、6 步）：
1. 导出 cookie：Cookie 只能由真实浏览器产生，工具不破解登录。
2. 页面内重放：某些接口只在页面上下文里通（CORS / 首屏鉴权 / g_tk 域绑定），
   此时把请求「借」给浏览器发，比在 Python 里硬拼更可靠。

本模块**不包含浏览器驱动依赖**：它只产出/消费纯文本协议，
由调用方（CLI 或 MCP 工具）负责把 JS 送进浏览器、把结果取回来。
这样工具在无浏览器环境仍能工作（已导入 cookie 的情况下）。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .models import DLinkError


class BrowserBridgeError(DLinkError):
    code = "E_BROWSER"


# 在浏览器控制台里跑的取 cookie 脚本。
# 注意：微云 cookie 的 skey / TOK 等在 www.weiyun.com 与 share.weiyun.com 上可能不同，
# 需要按域分别取，所以脚本接收 origin 参数。
COOKIE_JS = r"""
(() => {
  const m = document.cookie.split(';').filter(Boolean);
  const out = {};
  for (const kv of m) {
    const i = kv.indexOf('=');
    if (i < 0) continue;
    out[kv.slice(0, i).trim()] = kv.slice(i + 1).trim();
  }
  return JSON.stringify({origin: location.origin, cookies: out});
})()
"""

DAT_JS = r"""
(() => {
  try {
    const e = performance.getEntriesByType('resource')
      .filter(x => /webapp\/json/.test(x.name))
      .map(x => new URL(x.name));
    const g = e.map(u => u.searchParams.get('g_tk')).filter(Boolean);
    return JSON.stringify({origin: location.origin, g_tk: g[0] || null, count: e.length});
  } catch (err) { return JSON.stringify({error: String(err)}); }
})()
"""


def parse_cookie_export(text: str) -> dict:
    """解析浏览器回传的 JSON（COOKIE_JS 的输出），返回 {origin, cookies}。

    容错：允许外面裹一层引号、允许直接是 cookie 字符串（`a=1; b=2`）。
    """
    text = (text or "").strip()
    if not text:
        raise BrowserBridgeError("浏览器没有返回任何内容")

    # 情况 1：标准 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, str):
            obj = json.loads(obj)
        if isinstance(obj, dict) and "cookies" in obj:
            cookies = obj.get("cookies") or {}
            if not isinstance(cookies, dict):
                raise ValueError
            return {"origin": obj.get("origin", ""), "cookies": {str(k): str(v) for k, v in cookies.items()}}
        if isinstance(obj, dict) and obj and all(isinstance(v, str) for v in obj.values()):
            # 直接就是 cookie 字典
            return {"origin": "", "cookies": obj}
    except (json.JSONDecodeError, ValueError):
        pass

    # 情况 2：裸 cookie 字符串
    if "=" in text and "{" not in text:
        cookies = {}
        for kv in re.split(r";\s*", text):
            if "=" in kv:
                k, _, v = kv.partition("=")
                cookies[k.strip()] = v.strip()
        if cookies:
            return {"origin": "", "cookies": cookies}

    raise BrowserBridgeError(
        "看不懂浏览器返回的内容，期望形如 {\"origin\":\"...\",\"cookies\":{...}} 的 JSON",
        hint="把 COOKIE_JS 的完整输出原样粘贴进来",
    )


def gtk_from_skey(skey: str) -> int:
    """QQ 系标准 g_tk 算法。空 skey 返回 0。"""
    if not skey:
        return 0
    h = 5381
    for ch in skey:
        h = (h * 33 + ord(ch)) & 0x7FFFFFFF
    return h


def replaysnippet(protocol: str, name: str, g_tk: int, body: dict) -> str:
    """生成一段可在 share.weiyun.com 控制台直接粘贴的 fetch 重放片段。

    用途：当 Python 侧因 CORS/风控拿不到结果时，让用户粘这段，把返回贴回来。
    """
    url = f"/webapp/json/{protocol}/{name}?refer=chrome_windows&g_tk={g_tk}&r="
    payload = json.dumps(body, ensure_ascii=False)
    return (
        f"fetch({url!r} + Math.random(), {{method:'POST', credentials:'include', "
        f"headers:{{'Accept':'application/json, text/plain, */*',"
        f"'Content-Type':'application/json;charset=utf-8'}}, "
        f"body:{json.dumps(payload)}}})"
        f".then(r=>r.text()).then(t=>{{console.log(t); copy(t); return t;}})"
    )


def check_login(cookies: dict) -> tuple[bool, Optional[str]]:
    """判断 cookie 里有没有能代表登录态的 skey。返回 (是否登录, 说明)。"""
    if not cookies:
        return False, "没有任何 cookie"
    if not cookies.get("skey"):
        return False, "缺少 skey —— 该会话是访客态，取直链需要登录"
    return True, None
