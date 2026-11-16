# 各平台实测汇总

> 全平台对比。数据均为本机 2026-11-15 实测。

## 一览

| 平台 | 直链获取 | 限速类型 | 实测速度 | 推荐 workers | 并行有效？ |
|---|---|---|---|---|---|
| **腾讯微云** | ✅ 需登录（`g_tk`+`FTN5K`） | **连接级** | 单连接 14MB 后掉到 240B/s；8 路聚合 **9.7 MB/s** | **8** | ✅ **有效** |
| **百度网盘** | ✅ 需登录（`BDUSS`+`STOKEN`+`BDCLND`） | **账号级配额** | 恒定 **80 KB/s**，聚合 100–130 KB/s 不随并发增 | **1** | ❌ 无效，8 路触发 403 |
| **夸克网盘** | ✅ 需登录（`__puus`），未登录 `31001` | **二阶段**（窗口期/限流期） | 窗口期 15–20MB/s；限流期约 1.8MB/s | **6**（需降级） | ⚠️ 窗口期有效 |
| **UC 网盘** | 同夸克 + `ut` 参数 | 同上（推测） | **未测**（无账号） | **6** | ⚠️ 未验证 |

---

## 1. 腾讯微云

### 直链链路
```
分享页 → store 读 file 树（免登录）
      → POST weiyunShare/WeiyunShareBatchDownload (cmd 12024，需登录)
      → https_download_url + FTN5K cookie
```

### 关键点
- `g_tk` 必须与**当前域** cookie 的 `skey` 匹配（`share.weiyun.com` 与 `www.weiyun.com` 的 g_tk 不同）
- 缺 `Accept` 头会得到 **403 空 body**（容易误判成登录问题）
- `FTN5K` cookie **必带**，缺则 401
- 信封是**双重 JSON 编码**

### 限速特征
- 单连接前 ~14MB 约 10MB/s，之后掉到 ~240B/s 并断流
- **新连接冷启动仍 ~9.7MB/s** → 限的是「连接年龄」不是账号
- **对策：32MB × 8 路分段并行，实测 1.53GB 约 2 分钟**

---

## 2. 百度网盘

### 直链链路（已实现并实测通过）
```
分享 URL + 提取码
  → BDCLND cookie 跳过提取码验证（浏览器通过提取码后种下）
  → /api/shorturlinfo 拿 shareid + uk（免登录）
  → /share/list 列目录（免登录）
  → /share/transfer 转存（需登录）
  → /api/filemetas?dlink=1 取直链（需登录）
```

### 关键点
- **`STOKEN` 必需**（`.passport.baidu.com` 域），只给 `BDUSS` 一律 `errno:-6`
- `bdstoken` 从 `/api/gettemplatevariable` 取
- `/api/sharedownload`（分享直下）**需 `jsToken` + `sekey`**，它们是**页面 JS 运行时变量**，静态 HTML 里没有 → Python 拿不到，只能走转存
- 直链 `expires=8h`，与 cookie 强绑定（缺 `BDUSS` 出 403 `error_code:31045`）
- 百度偶发返回损坏 gzip（`zlib: incorrect header check`）→ 请求头加 `Accept-Encoding: identity`
- 转存会污染网盘（同名文件变成 `PC(1).7z`）

### 限速特征（详见 `docs/BAIDU-BYPASS-FINAL.md`）
- 单连接**精确 80.0 KB/s**，40 秒零波动（服务端硬编码）
- 聚合 1/2/3/5/8 路 = 75/108/130/105/9 KB/s
- **换直链、换 CDN、换 UA、换 clienttype、改 Range、老接口 → 全部无效**
- 权威佐证：BaiduPCS-Go README「本软件不支持普通用户提速」

---

## 3. 夸克网盘 / UC 网盘

### 直链链路（源码确认 + 部分实测）
```
POST /1/clouddrive/share/sharepage/token      → stoken（免登录 ✅ 实测）
GET  /1/clouddrive/share/sharepage/detail     → 文件树（免登录 ✅ 实测）
POST /1/clouddrive/file/download  {fids:[..]} → download_url（需登录）
```

### 关键点（都是踩坑点）
1. **`/share/sharepage/download` 不存在（404）**——网上资料普遍写错，正确是 `/file/download`
2. **分享文件直取需登录**：未登录返回 `31001 require login [share missing]`
3. **UA 必须伪装 PC 客户端**，否则判游客触发 `23018` 大小限制
4. **UC 需要 `ut` 查询参数**：AES-128-CBC 加密 UTDID，固定 key `2106f55fd41e800c`，魔数 `0x35E9`。**已用 AList 官方测试向量逐字节验证 MATCH**
5. **cookie 快照必须与取链时一致**——下载 URL 签名基于取链时的 cookie 生成，中途刷新 `__puus` 会导致 403

### 限速特征（来自 fnOS-quark issue #6 的实测记录）
```
13:10  19.72 MB/s   ← 峰值
13:19   2.08 MB/s   ← 断崖
13:20~ 稳定 1.7~2.2 MB/s（不复原）
```
- 官方客户端同样被限 → **账号级**，不是第三方通道差异
- WiFi/蜂窝一致 → 不是 IP 级
- 触发时段：**18:00–23:00**
- 限流期约 100KB/s/连接，16 路 ≈ 1.6MB/s 与天花板持平 → **推断账号级**

### 待验证
「限流期增加并发是否有效」——需在 18:00–23:00 做 A/B/C 对照（连接级 vs 账号级的判定实验）。

---

## 4. 实测证据

| 项目 | 状态 |
|---|---|
| 百度完整链路 | ✅ 端到端跑通，拿到直链，206 + 7z 魔数验证 |
| 夸克免登录解析 | ✅ token + 文件树，递归列出 42.49 GB |
| 夸克取直链 | ⏳ 需登录 |
| UC 全部 | ⏳ 无账号 |
| UC `ut` 算法 | ✅ 官方测试向量 MATCH |
| 微云 | ✅ 交接文档记录，本机出口 IP 被网关拦 403 待换网络复测 |

---

## 5. 对下载器的启示

1. **workers 必须按平台给**：微云 8、百度 1、夸克 6
2. **要能降级**：夸克窗口期→限流期，检测"聚合不再增长"时降到 1 路
3. **直链要快用**：百度 8h、微云签名更短，取链后立即下载
4. **cookie 要快照**：夸克的直链与取链时 cookie 强绑定

---

## 6. 重要补充：百度「分享直链」匿名可用（2026-11-15 实测）

通过浏览器扩展在**页面上下文**解析时，拿到了 `window.jsToken` + `window.currentSekey`
（Python 抓静态 HTML 拿不到，这是扩展不可替代的原因）。

有了这两个，`/api/sharedownload` 直接返回 **`rt=sh` 分享直链**，且：

```
GET <rt=sh 直链> + Referer: https://pan.baidu.com/   （不带任何 cookie）
→ 206 Partial Content, content-range: bytes 0-1023/29620737
→ magic: 377abcaf271c（7z）✅
```

**结论**：
- ✅ 分享直链**匿名可下载**，连 `BDUSS` 都不需要
- ✅ **不需要转存**，不污染自己网盘，不受转存配额限制
- ⚠️ 但**速度仍是 ~74 KB/s**，与转存后一样 → 限速按 IP/会话，不是按账号归属
- ⚠️ `expires=8h`

这对工具是净收益：路径更短、更干净，但**并不能提速**（与四路调研结论一致）。

### 扩展相对 Python 的独有能力

| 能力 | Python | 浏览器扩展 |
|---|---|---|
| 读 `window.yunData` | ❌（JS 注入） | ✅ |
| 读 `window.jsToken` | ❌（同上） | ✅ |
| 读 `window.currentSekey` | ❌（同上） | ✅ |
| 拿到 HttpOnly cookie | ❌ | ✅（`chrome.cookies`） |
| 平台会话通过风控 | ❌（TLS/指纹校验） | ✅（原生会话） |
| 流式下载 | ✅ | ❌（CORS） |

**这就是「扩展取直链 + 下载器下载」架构的根本依据。**

---

## 7. 普通 HTTP 链接：并发数必须按主机自适应（2026-11-15 实测）

「普通链接」不是一类，而是千差万别。同一份代码、同样的并发设置，在不同主机上结果完全相反：

| 主机 | 1 路 | 4 路 | 8 路 | 特性 |
|---|---|---|---|---|
| proof.ovh.net | 4.91 MB/s | **8.19 MB/s** | 3.07 MB/s | **4 路最优**，8 路反而崩（服务器限并发） |
| registry.npmmirror.com | 2.72 MB/s | 8.59 MB/s | **11.55 MB/s** | **越多越快**（单连接跑不满） |

（单位 MB/s，同一文件、同样的 Range 分段，各跑 4–5 秒）

### 结论

**固定并发数是错的。** 一个写死在设置里的值，在 A 站是最优，在 B 站可能是灾难。

### 应对设计

三层结构：

1. **平台默认**（已有）：微云 8 / 百度 1 / 夸克 6 / 普通链接 8
2. **按 host 学习并记忆**（建议新增）：同一个域名第二次下载时，记住上次实测的最优并发
3. **任务级临时覆盖**：单次下载可手动指定，不影响记忆

第 2 层的可行性已验证：同一主机重复测两轮，趋势一致（npmmirror 第 1 轮 8 路 11.55、第 2 轮 8.76，都明显高于单连接）。

### 最小可行实现（不做过度的自动探测）

不必为每个下载都先跑一遍测速（那太浪费）。建议：

- **首次**下载某主机：用默认 8 路，**同时记录**实际聚合速度
- **第二次**：如果上次 8 路时出现过大量 403/0 速度（判定为「限并发」），降到 4 路
- 把结果记在 `%LOCALAPPDATA%/dlink/hosts.json`：

```json
{
  "proof.ovh.net": {"workers": 4, "reason": "8路时出现连接被拒", "updated": 1794727000},
  "registry.npmmirror.com": {"workers": 8, "reason": "8路聚合11.5MB/s", "updated": 1794727000}
}
```

- 用户在 GUI 里可以查看/修改/清空这个记忆

---

## 8. 平台路线图（**均未接入，未验证**）

> 下面的是候选，不是承诺。可行性基于公开信息推断，接入前必须实测 ——
> 网盘接口变化很快，任何没有实测记录的平台都不要写进 README 的支持表。

### 第一梯队（API 相对清晰，值得先做）

| 平台 | 域名 | 预判难点 |
|---|---|---|
| **阿里云盘** | `alipan.com` / `aliyundrive.com` | 前端 API 较完整；分享需 `share_id` + `share_token`；直链由 `getDownloadUrl` 返回，带 `x-oss-` 签名 |
| **123 云盘** | `123pan.com` / `123684.com` | 小站但 API 简单；分享直链需 `passCode`；有频率限制 |
| **天翼云盘** | `cloud.189.cn` | 需登录态；分享走 `shareId`；有 `familyId` 概念 |
| **蓝奏云** | `lanzou*.com`（域名常换） | 直链藏在页面 JS，**UA 极敏感**，域名轮换频繁，维护成本高 |

### 第二梯队（需要较多逆向）

| 平台 | 域名 | 预判难点 |
|---|---|---|
| 移动云盘 | `yun.139.com` | 接口字段复杂，需抓包 |
| 迅雷云盘 | `pan.xunlei.com` | 强风控，可能需要设备指纹 |
| 115 网盘 | `115.com` | 有开放平台但需申请；网页端接口加密较重 |
| 坚果云 | `jianguoyun.com` | 支持 WebDAV（最省事，但需用户填账号） |

### 不建议做

| 平台 | 原因 |
|---|---|
| 城通网盘 `545c.com` | 广告跳转严重，直链极不稳定 |
| 奶牛快传 / 文叔叔 | 临时分享型，时效短、接口已多次变更 |
| 各类「云盘聚合站」 | 本身是二次封装，接它们不如接源头 |

### 接入一个新平台的完整清单

1. `src/dlink/platforms/<name>.py` —— 实现平台接口
2. `src/dlink/profile.py` 的 `BUILTIN` 加默认策略 + 说明文案
3. `extension/injected.js` 加 `resolveXxx()` + 注册表一行
4. `extension/platforms.js` 加分享页域名识别 + **CDN 域名映射**
   （漏了这一步，接管下载时会按 `direct` 跑 8 路，百度那类账号级限速会直接塌）
5. `extension/manifest.json` 加 `host_permissions` 与 `content_scripts.matches`
6. `docs/PLATFORMS.md` 补实测数据（本文件）
7. `README.md` 平台表加一行
8. `tests/test_platform_<name>.py`

---

## 9. 百度直链的两套签名（2026-11-16 实测，重要）

百度 CDN 直链有两种签名，**能不能脱离浏览器使用**完全取决于用哪套：

| 来源 | sign 前缀 | 关键参数 | 是否需要 cookie | 能否外部使用 |
|---|---|---|---|---|
| 网页 `api/filemetas` | `FDtAER-` | `clienttype=0` | **需要 BDUSS** | ❌ 一律 `31362 sign error` |
| xpan `rest/2.0/xpan/multimedia?method=filemetas` | `FDtAERK-` | `access_token=123.xxx`、`vuk`、`origin_appid`、`file_type` | **不需要** | ✅ 302 → `*.baidupcs.com` → 206 |

### 网页通道那套为什么用不了

实测把一个 `FDtAER-` 直链（连 29MB 的无关文件也一样）在**同一个浏览器、同一秒**打开：

```
{"error_code":31362,"error_msg":"sign error"}
```

换成 `clienttype=12`、补 Referer、从网盘页点击过去 —— 全部 31362。
不带 cookie 则是 `31045 user not exists`。结论：网页通道的直链**只能在网页自身的下载流程里消费**。

### xpan 那套怎么才算「可用」

```
GET https://d.pcs.baidu.com/file/<md5>?fid=...&sign=FDtAERK-...&access_token=123.xxx
  → 302 https://<node>.baidupcs.com/file/...
  → 206 Content-Range: bytes 0-1023/1675346578
```

**不需要任何 cookie**，UA 不敏感（`pan.baidu.com` / `netdisk` 都行）。

### 代价与限制

- `access_token` 只能通过**百度开放平台 OAuth** 换取（设备码授权 → refresh_token 续期）。
  网页会话里拿不到：`gettemplatevariable`、`window.locals`、`/api/oauth/gettoken` 都没有。
- 即使有 token，**速度仍是账号级限额**（免费账号约 80–130 KB/s）。token 解决的是「能不能下」，不是「下得快」。
- `api/filemetas` 的参数名是 **`fsids`**，不是 `target`（用 `target` 会对任何文件返回
  `errno=12 / info:[{errno:31021}]`，极易误判成「文件被屏蔽」）。
