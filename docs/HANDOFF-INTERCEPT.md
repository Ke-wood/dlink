# 交接文档：浏览器下载接管 + 网盘平台路线图

> 写给接手的新 agent。本文自包含，不依赖前序会话上下文。
> 日期：2026-11-16　版本基线：v0.1.0（已发布）

---

## 0. 项目现状（先读这一节）

### 0.1 这是什么

DLink = **浏览器扩展取直链 + 本地下载器下载**，不劫持浏览器原生下载（当前版本）。

```
┌──────────────┐  直链+鉴权   ┌───────────────┐
│ 浏览器扩展    │ ──────────> │  DLink.exe     │
│ (Edge MV3)   │             │ (PySide6 GUI)  │
│ 读登录态      │ <────────── │  内嵌 HTTP API │
│ 页面内解析    │  续链轮询    │  任务队列下载   │
└──────────────┘             └───────────────┘
                                     │
                         127.0.0.1:8765-8769
```

### 0.2 代码地图

```
DLink/
├── launch.py                      打包入口（后端线程 + GUI 主线程）
├── DLink.spec                     PyInstaller 配置（含 extension/ 打包）
├── extension/                     ← 本任务主要改动区
│   ├── manifest.json              MV3 清单
│   ├── background.js              service worker：编排/取链/续链轮询
│   ├── injected.js                ★ 页面上下文执行各平台解析
│   ├── content.js                 悬浮按钮
│   ├── popup.html / popup.js      面板（提取直链 → 发送）
│   └── platforms.js               平台识别（域名正则）
├── src/dlink/
│   ├── server.py                  本地 HTTP API + 任务创建
│   ├── task_queue.py              有界并发队列
│   ├── downloader.py              分段并行/续传/校验（与平台无关）
│   ├── profile.py                 ★ 分平台下载策略（workers/seg_size）
│   ├── settings_store.py          全局设置持久化
│   ├── host_learn.py              按 host 学习并发
│   ├── refresh.py                 直链续期管理
│   ├── resources.py               打包态/开发态资源路径
│   ├── hashing.py                 哈希清洗（百度 md5 是非标准格式）
│   ├── cleanup.py                 夸克转存清理
│   ├── platforms/                 平台适配器
│   │   ├── baidu.py  quark.py  weiyun.py  direct.py
│   └── gui/                       PySide6 界面
└── tests/                         146 个测试
```

### 0.3 当前扩展的权限（`extension/manifest.json`）

```json
"permissions": ["cookies","storage","activeTab","scripting",
                "declarativeNetRequest","declarativeNetRequestWithHostAccess",
                "tabs","alarms"]
"host_permissions": 12 条（baidu / quark / uc / weiyun / pcs / 本地端口）
"background": { "service_worker": "background.js", "type": "module" }
"action": { "default_popup": "popup.html" }
```

**没有** `downloads`、`notifications`；**没有** `options_ui`。

### 0.4 后端 API 契约（已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{app,version,ok,queue,extension}` |
| GET | `/api/tasks` | `{tasks:[...], stats:{...}}` |
| POST | `/api/task` | 派发下载任务（见下） |
| POST | `/api/task/action` | `{id, action: pause\|resume\|cancel\|remove}` |
| DELETE | `/api/tasks/clear` | 清除已完成 |
| GET/POST | `/api/settings` | 全局设置（**已持久化**） |
| GET/POST | `/api/profiles` | 分平台策略 |
| GET/POST | `/api/hosts` | 按站点学习值 |
| GET | `/api/pending-refresh` | 扩展轮询续链 |
| POST | `/api/refresh` | 扩展回填新直链 |

**POST /api/task 请求体**：

```json
{
  "version": 1,
  "platform": "baidu | quark | uc | weiyun | direct",
  "title": "可选",
  "items": [{
    "url": "https://...",
    "filename": "a.bin",
    "size": 12345,
    "md5": "",
    "sha1": "",
    "cookies": {"BDUSS": "..."},
    "headers": {"Referer": "https://..."},
    "expires_at": 1794800000,
    "refresh": {"platform":"baidu","shareUrl":"...","fid":"..."},
    "transfer": null,
    "workers": 8
  }]
}
```

> `platform` 决定用哪套 `DownloadProfile`（并发数、分段大小）。
> **这是拦截功能必须正确的关键字段** —— 见 §4.3。

### 0.5 各平台实测限速（决定 `workers`，不要改错）

| 平台 | 限速类型 | 实测 | profile 里 `workers` |
|---|---|---|---|
| 腾讯微云 | 连接级（单连接约 14MB 后掉到 240B/s） | 8 路聚合 9.7 MB/s | **8** |
| 百度网盘 | 账号级配额，恒定 80 KB/s | 1 路 75 / 3 路 130 / **8 路 9 KB/s** | **1（force_single）** |
| 夸克 | 二阶段（窗口期/限流期） | 窗口期 15–20 MB/s | **6** |
| UC | 推测同夸克，**未实测** | — | 6 |
| 普通链接 | 看服务器 | 4 路比单连接快 1.7x，8 路反而慢 | **8** |

---

## 1. 本任务目标

### 1.1 需求 A：国内网盘平台路线图

产出一份清单：常见国内网盘 → 可行性分级 → 优先级。**不是全部实现**。

### 1.2 需求 B：浏览器下载接管（核心开发任务）

扩展新增开关，**开启后**：

1. 监听浏览器发起的下载
2. 拦截（阻止浏览器自己下）
3. 转交给 DLink 下载
4. **首次触发**时弹提示框，告知三件事：
   - 询问是否默认「接管并转到 DLink」
   - 以后在哪里开关这项设置
   - 接管范围（重要：会接管所有下载）

---

## 2. 技术方案（推荐实现）

### 2.1 拦截点选择

| 方案 | 可行性 | 结论 |
|---|---|---|
| `chrome.webRequest.onBeforeRequest` + blocking | ❌ MV3 不允许普通扩展使用 blocking | 排除 |
| `declarativeNetRequest` redirect | ⚠️ 能改 URL，但无法"交给本地程序"，只能重定向到某个 URL | 不适用 |
| **`chrome.downloads.onCreated`** | ✅ 可靠，能拿到 url/finalUrl/referrer/filename/mime/totalBytes | **采用** |
| `chrome.downloads.onDeterminingFilename` | ⚠️ 可更早介入，但仍需 cancel | 备选 |
| content script 拦 `<a download>` 点击 | ⚠️ 只能覆盖一部分，抓不到 JS 触发的 | 辅助 |

**采用 `chrome.downloads.onCreated`**，理由：覆盖所有经过浏览器下载栈的请求（含 JS 触发、iframe 触发、导航到 attachment）。

### 2.2 必须新增的权限

```json
"permissions": [..., "downloads", "notifications"]
```

⚠️ **运维影响**：已安装扩展的用户会被浏览器**自动禁用**，必须手动重新启用，并会看到「管理你的下载」警告。README 和升级提示里必须写明。

### 2.3 首次触发的流程（关键设计）

```
downloads.onCreated(item)
  │
  ├─ 过滤：incognito? blob:/data:? 自己的扩展? 127.0.0.1? 白名单域名? → 放行，什么都不做
  │
  ├─ intercept.enabled === false 且 intercept.asked === true → 放行
  │
  ├─ DLink 不在线？ → 放行（绝不拦下来又发不出去）
  │
  ├─ 首次（intercept.asked === false）
  │    ├─ chrome.downloads.pause(item.id)     ← ★ 暂停而不是取消
  │    ├─ 打开提示窗口 prompt.html
  │    │    · 「接管并转到 DLink」 → 记住 enabled=true → cancel + 转交
  │    │    · 「保持浏览器下载」   → 记住 enabled=false → resume(item.id)
  │    └─ 无论选哪边，intercept.asked = true
  │
  └─ 已开启（enabled === true）
       ├─ chrome.downloads.cancel(item.id)
       ├─ chrome.downloads.erase({id}); removeFile(id)  // 清临时文件
       └─ 构造任务 POST /api/task
            ├─ 失败 → chrome.downloads.download({url, filename})  // 兜底还原
            └─ 成功 → 角标 +1，可选通知
```

**为什么用 `pause` 而不是 `cancel`**：
`cancel` 之后要还原只能 `chrome.downloads.download(url)`，那会丢失：
- POST 表单体
- 自定义请求头（除 Referer）
- 某些站点的一次性签名（重新请求会失效）

`pause` 保留整个下载上下文，`resume` 能原样继续。**这是本方案最关键的取舍。**

### 2.4 构造任务时的字段来源

| 字段 | 来源 |
|---|---|
| `url` | `item.finalUrl || item.url`（优先 finalUrl，已跟随重定向） |
| `filename` | `item.filename`（Chrome 解析的文件名，可能含完整路径） |
| `size` | `item.totalBytes`（可能为 -1，表示未知） |
| `headers.Referer` | `item.referrer`（可能为空） |
| `cookies` | `chrome.cookies.getAll({url})` → 转成 `{name: value}` |
| `platform` | **由 host 推断**，见 §4.3 |
| `expires_at` | 不设（普通下载没有明确时效） |

### 2.5 设置存储（`chrome.storage.sync`）

```js
{
  "intercept.enabled": false,        // 总开关
  "intercept.asked": false,          // 是否问过（首次提示只弹一次）
  "intercept.mode": "netdisk",       // "netdisk" = 只接管已知网盘 | "all" = 全部接管
  "intercept.domainsAllow": [],      // 永不接管的域名（放行名单）
  "intercept.minSize": 0,            // 小于此字节数的文件不接管（0=不限制）
  "intercept.silent": false          // 接管时不弹通知
}
```

### 2.6 设置界面落点

**新增扩展选项页** `extension/options.html` + `options.js`：

```json
"options_ui": { "page": "options.html", "open_in_tab": true }
```

页面内容：
- 总开关（是否接管浏览器下载）
- 接管模式：只接管网盘 / 全部接管
- 放行域名列表（可增删）
- 最小文件大小
- 是否弹通知
- 「恢复首次询问」按钮（重置 `intercept.asked`）
- 只读区：DLink 连接状态、当前保存目录

**同时**在 `popup.html` 顶部放一个快捷开关（同一个 `chrome.storage` 键），并在首次提示窗口里给出跳转入口。

### 2.7 首次提示窗口

用 `chrome.windows.create({url:'prompt.html', type:'popup', width:460, height:340})` 而不是 `chrome.notifications`，理由：

| | `chrome.notifications` | 独立小窗 |
|---|---|---|
| 文字长度 | 严格限制，会被截断 | 自由 |
| 按钮数 | 最多 2 个 | 任意 |
| 能否放「去哪里改」的说明 | 很难 | 可以 |
| 用户可能忽略 | 高（会滑走） | 低 |

**三块信息的文案（初稿）**：

```
标题：要接管浏览器下载吗？

① 说明
   DLink 可以接管浏览器发起的下载，转由本地下载器完成
   （多线程、断点续传、自动续链）。
   接管后，浏览器自己的下载栏不会再出现这些文件。

② 选择
   [ 接管并转到 DLink ]        [ 保持浏览器下载 ]
   ☐ 只接管网盘链接（推荐）      ☐ 接管所有下载

③ 以后在哪改
   扩展图标 → 右键 → 选项 → 「接管浏览器下载」
   也可以随时点扩展面板顶部的开关关闭。
```

### 2.8 冲突处理

| 场景 | 处理 |
|---|---|
| DLink 未运行 | **不拦截**，直接放行 |
| DLink 返回错误 / 拒绝 | `chrome.downloads.download(url, filename)` 还原，并提示「已回退到浏览器下载」 |
| 3 秒内重复的同 URL | 去重，只处理第一次 |
| 用户想临时放行一次 | 扩展右键菜单「本次放行」→ 设 `intercept.skipOnce` 标记 |
| 下载被 Chrome 自身触发（扩展更新等） | `item.byExtensionId` 非空则放行 |

---

## 3. 必须解决的已知缺陷（否则功能是半残的）

### 3.1 `blob:` / `data:` URL —— 无法转交

现代站点常用 `URL.createObjectURL(blob)` + `<a download>`。这个 URL 只存在于页面内存，DLink 拿不到。

**处理**：识别 `url.startsWith('blob:')` → 放行浏览器下载，并（可选）提示「此类下载无法接管」。
**不要**尝试用 service worker 中转字节流（大文件会爆内存、且 MV3 worker 会随时被杀）。

### 3.2 HLS / DASH 视频流 —— 不在本期范围

`.m3u8` / `.mpd` 是分片清单，不是文件。要支持需另做项目（解析清单 + 下载分片 + 合并 + 解密）。
**处理**：检测到 m3u8/mpd 时放行，文档里注明不支持。

### 3.3 平台推断（★ 必须做，否则百度会被风控）

拦截到的是 CDN 直链，必须反推平台，否则一律按 `direct`（8 路）跑，**百度会掉到 9 KB/s**。

在 `extension/platforms.js` 增加：

```js
// CDN 域名 → 平台
const DOWNLOAD_HOST_MAP = [
  { re: /(^|\.)pcs\.baidu\.com$/i,        platform: 'baidu'  },
  { re: /(^|\.)baidupcs\.com$/i,          platform: 'baidu'  },
  { re: /(^|\.)drive\.quark\.cn$/i,       platform: 'quark'  },
  { re: /(^|\.)drive\.uc\.cn$/i,          platform: 'uc'     },
  { re: /(^|\.)weiyun\.com$/i,            platform: 'weiyun' },
];
export function platformForDownloadUrl(url) {
  try {
    const host = new URL(url).hostname;
    for (const m of DOWNLOAD_HOST_MAP) if (m.re.test(host)) return m.platform;
  } catch {}
  return 'direct';
}
```

⚠️ **CDN 域名会变**（实测夸克是 `dl-pc-zb-j.drive.quark.cn`，前缀随机）。
所以：
- 正则要写成 `*.drive.quark.cn` 这种宽松形式
- 兜底 `direct` 是对的，但要在任务里带上**原始 referrer 域名**，方便人工判断
- 长期方案：DLink 侧按 `Referer` 头再兜底推断一次

### 3.4 权限升级后的引导

`downloads` 权限会让已装用户扩展被禁用。必须：
- README 升级说明
- 首次提示窗口如果检测到 `chrome.downloads === undefined` → 显示「请先重新启用扩展并授权」

---

## 4. 国内网盘平台路线图（需求 A）

> ⚠️ 下面除已实现的 4 个外，**其余均为候选、未验证**。可行性基于公开信息推断，接入前必须实测。

### 4.1 已实现

| 平台 | 状态 | 备注 |
|---|---|---|
| 百度网盘 `pan.baidu.com` | ✅ 实测跑通 | 需 `BDUSS`+`STOKEN`+`BDCLND`；可走分享直链免转存；**80KB/s 账号级限速** |
| 夸克网盘 `pan.quark.cn` | ✅ 实测跑通 | 分享文件需转存；直链**必须显式带 Cookie**（跨域不自动带）；UA 要求客户端 |
| UC 网盘 `drive.uc.cn` | ⚠️ 代码就绪未实测 | 与夸克同架构，多一个 AES 加密的 `ut` 参数（算法已用官方测试向量验证） |
| 腾讯微云 `share.weiyun.com` | ✅ 实测跑通 | 连接级限速，8 路并行有效 |

### 4.2 候选（按可行性排序，均需实测）

**第一梯队 —— API 相对清晰，值得优先做**

| 平台 | 域名 | 预判难点 |
|---|---|---|
| **阿里云盘** | `alipan.com` / `aliyundrive.com` | 有较完整的前端 API；分享需 `share_id` + `share_token`；直链由 `getDownloadUrl` 返回，带 `x-oss-` 签名 |
| **123 云盘** | `123pan.com` / `123684.com` | 小站但 API 简单；分享直链需 `passCode`；有频率限制 |
| **天翼云盘** | `cloud.189.cn` | 需登录态；分享走 `shareId`；有 `familyId` 概念 |
| **蓝奏云** | `lanzou*.com`（域名经常换） | 直链藏在页面 JS，**UA 极敏感**，且域名轮换频繁，维护成本高 |

**第二梯队 —— 需要较多逆向**

| 平台 | 域名 | 预判难点 |
|---|---|---|
| **移动云盘** | `yun.139.com` | 接口字段复杂，需抓包 |
| **迅雷云盘** | `pan.xunlei.com` | 强风控，可能需要设备指纹 |
| **115 网盘** | `115.com` | 有开放平台但需申请；网页端接口加密较重 |
| **坚果云** | `jianguoyun.com` | 支持 WebDAV（最省事，但需用户填账号） |

**不建议做**

| 平台 | 原因 |
|---|---|
| 城通网盘 `545c.com` | 广告跳转严重，直链极不稳定 |
| 奶牛快传 / 文叔叔 | 临时分享型，时效短、已多次变更 |
| 各类"云盘"聚合站 | 本身是二次封装，接它们不如接源头 |

### 4.3 接入一个新平台的完整清单（已有约定）

1. `src/dlink/platforms/<name>.py` —— 实现平台接口
2. `src/dlink/profile.py` 的 `BUILTIN` 加默认策略 + 说明文案
3. `extension/injected.js` 加 `resolveXxx()` + 注册表一行
4. `extension/platforms.js` 加域名识别 + **CDN 域名映射**
5. `extension/manifest.json` 加 `host_permissions` 与 `content_scripts.matches`
6. `docs/PLATFORMS.md` 补实测数据
7. `README.md` 平台表加一行
8. `tests/test_platform_<name>.py`

---

## 5. 实施步骤（分阶段，每阶段可验证）

### 阶段 1：基础接管（无提示框）

- [ ] manifest 加 `downloads` 权限 + `options_ui`
- [ ] `extension/intercept.js`：`onCreated` 监听 + 过滤 + cancel + 转交
- [ ] `platforms.js` 加 `platformForDownloadUrl()`
- [ ] 设置读写（`chrome.storage.sync`）
- [ ] 验证：用一个真实直链站点测试，确认文件由 DLink 下载

**验收**：开启后浏览器下载被 DLink 接管；关闭后浏览器正常下载。

### 阶段 2：首次提示 + 选项页

- [ ] `prompt.html/js`：暂停 → 询问 → 接受/拒绝 → cancel/resume
- [ ] `options.html/js`：完整设置界面
- [ ] popup 顶部快捷开关
- [ ] `intercept.asked` 只弹一次

**验收**：全新安装 → 首次下载被暂停并弹窗 → 选「保持浏览器下载」能正常续传；选「接管」则转交 DLink 且不再弹。

### 阶段 3：健壮性

- [ ] DLink 离线时放行
- [ ] 转交失败回退到浏览器下载
- [ ] 重复 URL 去重（3 秒窗口）
- [ ] blob:/m3u8 放行 + 说明
- [ ] 放行域名/最小大小/接管模式
- [ ] 角标与通知

**验收**：断网/关 DLink/点两次/下 blob 文件，四种场景都不丢文件。

### 阶段 4：文档与发布

- [ ] README 补「接管下载」章节 + 权限变更说明
- [ ] `docs/PLATFORMS.md` 加平台路线图
- [ ] 重新打包 exe（extension 已内嵌，需要重新导出）
- [ ] 发 v0.2.0

---

## 6. 验收标准

| # | 场景 | 期望 |
|---|---|---|
| 1 | 全新安装，首次下载 | 下载被暂停，弹提示框，含①询问②选择③在哪改 |
| 2 | 选「保持浏览器下载」 | 原下载**继续完成**，不丢文件 |
| 3 | 选「接管并转到 DLink」 | 原下载取消，DLink 出现任务并开始下载 |
| 4 | 再次下载 | 不再弹窗，直接接管 |
| 5 | 关闭开关后下载 | 浏览器原生下载，DLink 无任务 |
| 6 | 下载百度 CDN 文件 | DLink 里该任务**1 路**（不是 8 路） |
| 7 | DLink 未运行 | 浏览器正常下载，不拦截 |
| 8 | blob: 下载 | 放行，不接管，不报错 |
| 9 | 同一 URL 连点两次 | 只产生 1 个 DLink 任务 |
| 10 | 选项页改设置 | 立即生效，重启浏览器后保持 |

---

## 7. 待决策（需要人拍板）

1. **默认接管范围**：只接管已知网盘域名（推荐，隐私友好）还是全部接管？
2. **首次默认值**：提示框里「只接管网盘」是否默认勾选？
3. **是否要「本次放行」的具体入口**：右键菜单 / 通知按钮 / 面板复选框？
4. **平台路线图优先级**：先做哪个？（建议阿里云盘 → 123 云盘 → 天翼）
5. **是否上架浏览器商店**：`downloads` 权限在商店审核里比较敏感，若上架需准备说明材料。

---

## 8. 已知风险汇总

| 风险 | 等级 | 缓解 |
|---|---|---|
| `blob:` 下载无法接管，用户预期落差 | 高 | UI 明说；检测到就放行并提示 |
| 平台推断错误导致百度被 8 路跑 | 高 | 域名映射 + DLink 侧按 Referer 兜底 |
| `downloads` 权限导致扩展被禁用 | 中 | README + 首次检测提示重新授权 |
| 拒绝接管后恢复失败（POST 下载） | 中 | 用 pause 而非 cancel；仍失败则提示手动重下 |
| 隐私顾虑（看到所有下载 URL） | 中 | 默认只接管网盘域；放行名单 |
| 每次都被接管导致烦躁 | 中 | 快捷开关 + 本次放行 + 最小大小阈值 |
| CDN 域名变更导致推断失效 | 中 | 宽松正则 + fallback + 可手动指定 |
| MV3 service worker 被回收导致漏事件 | 低 | `onCreated` 是持久监听器，事件会唤醒 worker |

---

## 9. 开发环境速查

```bash
# 测试（必须全绿才能提交）
PYTHONPATH="src;.vendor" python -m pytest tests/ -q -p no:cacheprovider

# 开发态启动
PYTHONPATH=src python launch.py

# 打包
python -m PyInstaller DLink.spec --noconfirm --clean
# 产物 dist/DLink.exe

# 发布
python scripts/release.py --tag v0.2.0 --notes RELEASE_NOTES.md --file dist/DLink.exe
```

**扩展调试**：
- `edge://extensions/` → 打开开发者模式 → 加载 `extension/` 目录
- 点「服务工作进程」看 service worker 的 console
- 改代码后必须点扩展卡片上的「重新加载」

**注意**：
- 本机 `curl` 的 TLS 是坏的（Schannel `SEC_E_NO_CREDENTIALS`），一律用 Python `requests`
- 测试用 pytest 在 `.vendor/`（这是手工解包的，`.gitignore` 已排除）
- GitHub 凭据在 `~/.git-credentials`，脚本会自己读

---

## 10. 参考：现有代码里的可复用部分

| 需要什么 | 去哪里看 |
|---|---|
| 怎么找 DLink 端口 | `background.js` 的 `findDownloader()` / `ping()` |
| 怎么 POST 任务 | `background.js` 的 `sendToDownloader()` |
| 怎么取 HttpOnly cookie | `background.js` 的 `collectHttpOnlyCookies()` |
| 平台的并发策略 | `src/dlink/profile.py` 的 `BUILTIN` |
| 任务状态机 | `src/dlink/task_queue.py` 的 `Task` / `Queue` |
| 设置持久化范式 | `src/dlink/settings_store.py`（原子写 + 坏文件回退） |
| 平台识别正则 | `extension/platforms.js` |

---

— 完 —
