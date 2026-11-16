# 软件架构设计

> 目标：**浏览器扩展只负责取直链，独立下载器只负责下载**（不劫持浏览器原生下载）。
> 2026-11-15 定稿。

---

## 1. 为什么这么分工

| 能力 | 浏览器扩展 | 下载器 |
|---|---|---|
| 拿到登录态（HttpOnly cookie） | ✅ **只有它能做** | ❌ Python 拿不到 BDUSS/STOKEN |
| 页面内调用平台 API | ✅ 同源，无 CORS | ⚠️ 能，但需手工搬 cookie |
| 流式下载 + 写盘 | ❌ CORS、内存限制、无文件系统 | ✅ 强项 |
| 断点续传、多任务队列 | ❌ | ✅ 强项 |
| 资源占用 | 随浏览器 | 独立进程，可后台 |

**关键约束**：`BDUSS`、`STOKEN` 是 HttpOnly，JS 读不到，Python 也拿不到（Edge 用 App-Bound Encryption 加密）。**只有扩展的 background（`chrome.cookies` API）能读**。这是扩展不可替代的根本原因。

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│ 浏览器扩展 (Manifest V3)                                  │
│                                                          │
│  content script        background (service worker)       │
│  ┌──────────────┐      ┌─────────────────────────┐      │
│  │ 识别分享页    │      │ chrome.cookies.getAll()  │      │
│  │ 注入「发送到  │ ───> │ 平台解析器 (per-site)     │      │
│  │ 下载器」按钮  │      │ 取直链 + 鉴权 + TTL       │      │
│  └──────────────┘      └───────────┬─────────────┘      │
│                                    │ POST               │
└────────────────────────────────────┼────────────────────┘
                                     │ 127.0.0.1:PORT
                                     ▼
┌──────────────────────────────────────────────────────────┐
│ 下载器 (Python + GUI)                                     │
│                                                          │
│  ┌────────────────────────────────────────────┐          │
│  │ 本地 HTTP 服务 127.0.0.1:随机端口            │          │
│  │ POST /api/task   接收 {url, cookies, ...}   │          │
│  │ GET  /api/refresh?task=xx  扩展回填新直链    │          │
│  │ GET  /api/health  扩展探测下载器是否在线      │          │
│  └────────────────────────────────────────────┘          │
│                     │                                     │
│  ┌──────────────────▼──────────────────────┐             │
│  │ 任务队列 (sqlite)                        │             │
│  │  pending → running → done/failed         │             │
│  │  断点信息、ETA、速度                      │             │
│  └──────────────────┬──────────────────────┘             │
│                     │                                     │
│  ┌──────────────────▼──────────────────────┐             │
│  │ 下载引擎 (downloader.py)                 │             │
│  │  分段并行 / 单连接，按平台策略            │             │
│  │  Range 续传、完整性校验                   │             │
│  └─────────────────────────────────────────┘             │
└──────────────────────────────────────────────────────────┘
```

---

## 3. 数据契约（扩展 ↔ 下载器）

### 3.1 POST /api/task

```json
{
  "version": 1,
  "platform": "baidu",
  "title": "sample.bin",
  "items": [
    {
      "url": "https://d.pcs.baidu.com/file/...",
      "filename": "sample.bin",
      "size": 29620737,
      "sha1": "",
      "md5": "a1b2c3d4e5o6f7a8b9c0d1e2f3a4b5c6",
      "cookies": { "BDUSS": "...", "STOKEN": "..." },
      "headers": { "Referer": "https://pan.baidu.com/" },
      "expires_at": 1794748800,
      "refresh": {
        "platform": "baidu",
        "share_url": "https://pan.baidu.com/s/<分享ID>?pwd=<提取码>",
        "file_id": "73179373657953"
      }
    }
  ],
  "options": { "workers": 1, "save_dir": "D:/Downloads" }
}
```

**设计要点**：
- `cookies` 是**必需**的，不是可选。直链离开鉴权就是 403。
- `expires_at` 用 Unix 时间戳。百度是不给绝对时间的（只给 `expires=8h`），扩展按收到时刻 + 8h 算。
- `refresh` 块让下载器在链接过期时能**主动要新链接**——这是长下载能续下去的关键。
- `workers` 由扩展**按平台给出建议值**，下载器可覆盖。

### 3.2 POST /api/refresh-request（下载器 → 扩展，反向）

下载器不主动调扩展（扩展没有固定监听地址），改为：
1. 下载器把"需要刷新"写进自己的状态；
2. 扩展在 service worker 里**每 30 秒轮询** `GET /api/pending-refresh`；
3. 拿到请求后重新解析，`POST /api/task` 回填。

这样扩展始终是发起方，避免端口/权限问题。

---

## 4. 平台策略层（核心差异化）

各平台限速机制不同，**不能一刀切**：

| 平台 | 限速类型 | 实测 | workers | 说明 |
|---|---|---|---|---|
| **腾讯微云** | **连接级**（单连接 14MB 后掉到 240B/s） | 8 路聚合 9.7MB/s | **8** | 并行有效，是工具主要卖点 |
| **百度网盘** | **账号级配额**（80KB/s 恒定） | 1/3/8 路分别 75/130/9 KB/s | **1** | 并发反而触发风控（403） |
| 夸克 | 待测 | — | 待定 | |
| UC | 待测 | — | 待定 | |

**设计**：`Platform.recommended_workers()`，扩展在 `POST /api/task` 里带上建议值。

**百度的权威佐证**（BaiduPCS-Go README:952）：
> 普通用户请将 max_parallel 设为 1，调大线程数只会在短时间内提升下载速度，且极易很快触发限速，导致几小时至几天内账号在各客户端都接近 0 速。

---

## 5. 平台解密逻辑放哪？

**放在扩展里**，理由：

1. 登录态在浏览器，扩展读取零成本（`chrome.cookies.getAll({domain})`）
2. 平台页面就在眼前，可以直接 `chrome.scripting.executeScript` 读 `window.yunData` / `window.jsToken` —— **这些是 Python 拿不到的**（JS 注入的，静态 HTML 里没有）
3. 各平台 API 都是 HTTP 调用，扩展的 `fetch` 带 `credentials: 'include'` 天然同源

下载器只做**通用下载**，完全不懂各平台差异。

---

## 6. 关键技术难点与对策

| 难点 | 对策 |
|---|---|
| `jsToken`/`sekey`/`yunData` 只在 JS 运行时 | 扩展用 `executeScript` 注入读取 |
| 百度提取码需要 `BDCLND` cookie | 扩展在分享页 `chrome.cookies.get` 拿，随任务传递 |
| 直链过期（8h / 微云签名） | `refresh` 块 + 轮询回填 |
| 微云 `FTN5K` 下载 cookie | 随 `DirectLink.cookies` 一起传 |
| gzip 损坏（百度偶发） | 请求头 `Accept-Encoding: identity` |
| 本地端口被占用 | 启动时随机端口，写进 `%APPDATA%/dlink/port.txt`，扩展读取 |

---

## 7. 组件清单

```
dlink/
├── extension/                    # Chrome/Edge MV3 扩展
│   ├── manifest.json
│   ├── background.js             # 平台解析器 + 与下载器通信
│   ├── content.js                # 注入「发送到下载器」按钮
│   ├── popup.html / popup.js     # 手动粘贴链接
│   └── platforms/
│       ├── baidu.js
│       ├── weiyun.js
│       └── quark.js
└── app/                          # Python 下载器
    ├── server.py                 # 本地 HTTP API
    ├── queue.py                  # 任务队列 (sqlite)
    ├── gui.py                    # GUI 主窗口
    └── engine/                   # 复用现有 downloader.py
```

---

## 8. 分阶段交付

| 阶段 | 内容 | 验收 |
|---|---|---|
| **P0** | Python 引擎 + CLI | 能用直链下载并校验（已有） |
| **P1** | 百度/微云平台模块 | 取直链可用（百度已完成） |
| **P2** | 扩展：百度 + 微云 | 点按钮 → 下载器开始下载 |
| **P3** | GUI | 任务列表、进度、暂停续传 |
| **P4** | 夸克/UC | 按实测结果接入 |

---

## 9. 明确不做的

- ❌ 劫持浏览器原生下载（用户明确要求）
- ❌ 破解限速（已论证不可行，见 `docs/BAIDU-BYPASS-FINAL.md`）
- ❌ 多账号池 / 账号共享（封号风险）
- ❌ 上传/同步功能（只做下载）

---

## 10. 实测踩到的关键约束（2026-11-15）

### 10.1 平台页面会劫持 `fetch`

夸克页面加载了阿里的风控 SDK **baxia**，它包装了 `window.fetch`，会拦截发往
`127.0.0.1` 等非白名单地址的请求（报 `TypeError: Failed to fetch`）。
`XMLHttpRequest` 未被劫持（或者拦截更弱）。

**影响**：
- ❌ 不能在页面上下文里直接给下载器发任务
- ✅ **必须由 background（service worker）发**，它不在页面的 JS 环境里
- ✅ 这是当前设计，无需改动

### 10.2 页面 CSP 禁止连本地端口

夸克分享页的 CSP 阻止了从页面上下文访问 `http://127.0.0.1:*`。
同样不影响扩展：`chrome.scripting.executeScript({world:'MAIN'})` 是特权注入，
**不受页面 CSP 限制**。

### 10.3 夸克直链必须显式带 Cookie 头

夸克直链域名是 `dl-pc-*.drive.quark.cn`，与 `pan.quark.cn` **不同域**，
浏览器不会自动带上 cookie。AList 源码里明确写了：

```go
Header: http.Header{
    "Cookie":     []string{reqCookie},   // ← 必须显式带
    "Referer":    []string{d.conf.referer},
    "User-Agent": []string{ua},          // ← 必须是客户端 UA
}
```

**实测验证**：
- 不带 Cookie → `412 Precondition Failed`（590 字节 nginx 错误页）
- 带 Cookie + 客户端 UA + Referer → `206` + 正确内容 ✅

### 10.4 百度 `yunData` 是唯一可靠来源

`/api/shorturlinfo` 在有提取码的分享上会返回 `errno:0` 但 `file_list` 为空
（假成功）。而页面已经把完整数据注入 `window.yunData`。

**所以：优先读 `yunData`，接口只作兜底。**

### 10.5 百度 `jsToken` / `sekey` 只在页面运行时存在

静态 HTML 里搜不到（`hasJsToken: false`），但页面上下文里 `window.jsToken` /
`window.currentSekey` 有值。有了它们才能走「分享直下」，跳过转存。
**这是扩展相对 Python 的核心优势。**

---

## 11. 自动续链（已实现）

### 问题

大文件下载时间远超直链有效期：
- 百度 `expires=8h`、夸克约 6h、微云更短
- 1.5GB 在百度 80KB/s 下要 **5.4 小时**

一次下载中途必然遇到过期。

### 流程

```
下载器                              扩展 (service worker)
  │ 检测到 400/401/403/412
  │ → 任务状态 = need_refresh
  │ → 登记到 RefreshManager
  │                                    │ chrome.alarms 每 20 秒
  │ ◀──── GET /api/pending-refresh ────┤
  │ ─────── {task_id, platform, ──────▶│
  │          share_url, fid}           │ 复用/后台打开分享页
  │                                    │ 执行 injected.js 重新取链
  │ ◀── POST /api/refresh ─────────────┤
  │     {task_id, url, cookies, ...}   │
  │ → 更新任务凭证，重新入队             │
  │ → 保留 .parts，从断点续传            │
```

### 关键设计

| 决策 | 理由 |
|---|---|
| **扩展发起轮询**（而非下载器推） | 下载器不知道扩展在哪；扩展随时能 POST 到 127.0.0.1 |
| **用 `chrome.alarms` 而非 `setInterval`** | MV3 的 service worker 会被回收，定时器随之消失 |
| **错误判定放宽**（400/401/403/404/410/412/416 + 关键字） | 实测各平台过期状态码不统一：百度 400/403、夸克 412、微云 401 |
| **最多续链 5 次** | 防止无限循环；超出则明确失败 |
| **90 秒超时** | 扩展没响应（浏览器关了）时不能无限等，标 `timed_out` |
| **保留 `.parts`** | 续链后从断点继续，不重下已下载的部分 |

### 实测验证

```
提交直链必然 400 的任务
  → 1s 后状态 need_refresh
  → 扩展轮询拿到 {task_id, platform, share_url, fid}
  → 模拟回填新直链
  → 1s 后 running，12 秒下完 10.49 MB
  → done ✅
```

全程无需人工干预。
