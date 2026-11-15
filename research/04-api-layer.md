# 04 · API 层是否存在绕过限速的机制

> 调研日期：2026-11-15
> 测试账号：普通用户（非会员，`vip_level:4` / `is_vip:0` / `vip_type:1` / `current_mvip:[]`）
> 测试对象：`/Game/VRQ1/PC.7z`，29,620,737 字节，MD5 `a1b2c3d4e5o6f7a8b9c0d1e2f3a4b5c6`
> 标注体系：✅ 本机实测 / ⚠️ 源码或资料佐证 / ❓ 未验证

---

## 1. 一句话结论

**API 层没有可用的免费加速漏。**

所有「换通道 / 换 app_id / 换 clienttype / 伪装 vip / 伪装 dlna 投屏」的尝试，最终都收敛到**同一条 `d.pcs.baidu.com` 直链 + 同一个账号级限速**。社区历史上真实存在过的两个免费技巧（dlna 媒体伪装、第三方 SDK 低限速渠道）**在 2026-11 均已失效或被服务端显式封禁**（✅ 实测 `error_code:31329 hit black userlist, hit illeage dlna`）。

限速的判定点在**服务端按账号计算的下发速率**，不依赖任何客户端可伪造的参数。这是本次调研最硬的结论：`vip=2` 能被服务端接受并回显进直链 URL，但**不改变实际速率**（✅ 实测）。

---

## 2. A-E 逐条评估

### A. 会员 / 加速包相关的 API — ❌ 无免费通道

| 测点 | 结果 | 状态 |
|---|---|---|
| `GET /rest/2.0/membership/isp?method=query` | `{"isp_name":"","request_id":...}` — 只返回空的 ISP 名称，**不返回任何限速/特权字段** | ✅ 实测 |
| `POST /rest/2.0/membership/isp?method=query`（前端真实调用形态，带 `user_id=1`） | 同上，空对象 | ✅ 实测（浏览器抓包） |
| `GET /rest/2.0/membership/user?method=query` | 返回完整会员画像：`current_mvip:[]`、`current_privilege_card:[]`、`current_product:[]`、`level_info.current_level:1` | ✅ 实测 |
| `GET /api/gettemplatevariable` | `{"errno":0,"result":{"bdstoken":"...","is_vip":0,"vip_type":1}}` | ✅ 实测 |
| 伪造直链 `vip=2` + `type=nolimit` | 服务端**接受**：直链里出现 `vip=2`。实测速度 **39.0 / 32.4 KB/s**，与 `vip=0` 的 **26.7～71.8 KB/s** 在同一噪声带内，**无提升** | ✅ 实测 |
| `type=nolimit` vs `type=dlink` | 两者返回的直链形态完全相同（同为 `rt=sh`、同 host） | ✅ 实测 |

**关键判断**：`vip` 是**回显参数**，不是**鉴权参数**。服务端的带宽配额在签发直链时按账号真实身份算好，写进了会话状态；URL 上的 `vip=2` 只是被原样抄进查询串，CDN/下载网关不据此提额。

> ⚠️ **法律 / 封号风险提示**：伪造 `vip` / `type=nolimit` 请求属于对服务端身份声明的欺骗。即便当前无实际收益，也**不建议**投入工程。若将来某次改动导致它真生效，那就构成**规避付费产品**，触及《百度网盘服务协议》中的禁止条款，可导致限速加严、封号，情节严重可能涉《刑法》285/286 条相关罪名（非法侵入/破坏计算机信息系统）。本报告只做可行性判定，不提供任何伪造付费状态的方法。

**其他付费产品**（⚠️ 资料）：会员中心兜底的 `/rest/2.0/membership/...` 体系外，「下载加速包」在网页端是**前端定价页 + 支付回调**，购买后由服务端在账号维度置位 `current_product`。「加速包」并没有独立于会员的另一套传输通道——它买的仍是同一个提速额度池。

---

### B. 不同 app_id / 渠道是否限速不同 — ⚠️ 历史上真实存在，现已失效

| 测点 | 结果 | 状态 |
|---|---|---|
| `app_id=250528`（网页） vs `app_id=498065` | 两者 `filemetas` 均 `errno:0`，直链均为 `d.pcs.baidu.com` + `rt=sh`，`vip=0` | ✅ 实测 |
| `clienttype=0 / 1 / 3 / 8 / 17` | 0/1 正常；**8 → `errno:8001`**；**17 → `errno:113 验证码签名错误`**（签名算法不同，需配套 sign） | ✅ 实测 |
| `channel=chunlei` vs `channel=0` vs `channel=android_15_25010PN30C_bd-netdisk_1523a` | 仅影响签名校验是否通过，**不影响直链 host / 速率** | ✅ 实测 |

**这是「真实存在过」的证据**（⚠️ 源码佐证，来自 OpenList / alist 的 baidu 驱动）：

```go
// research/src-openlist/meta.go:14
DownloadAPI string `json:"download_api" type:"select"
    options:"official,crack,crack_video" default:"official"`

// research/src-openlist/util.go:251-263  —— crack_video 通道
param := map[string]string{
    "type":       "VideoURL",
    "path":       file.GetPath(),
    "fs_id":      file.GetID(),
    "devuid":     "0%1",
    "clienttype": "1",
    "channel":    "android_15_25010PN30C_bd-netdisk_1523a",  // ← 安卓客户端渠道号
    "nom3u8":     "1", "dlink": "1", "media": "1", "origin": "dlna",
}
resp, _ := d.request("https://pan.baidu.com/api/mediainfo", ...)
```

**结论**：
1. **确实存在过**「安卓客户端渠道号 + `media=1` + `origin=dlna`」能拿到**更宽带宽**的请求形态——这是社区所称的「crack」。
2. **2026-11 实测已失效**：按上述源码原样重放，`/api/mediainfo` 与 `/api/filemetas(origin=dlna)` 都能返回**直链**，但拿该直链去下载立刻被拒：
   ```
   HTTP 403  {"error_code":31329,"error_msg":"hit black userlist , hit illeage dlna"}
   ```
   → **服务端已识别 `origin=dlna` 为违规来源并加入黑名单**，不是参数错，是明确封禁。
3. `app_id` 差异**从未体现在限速上**，只体现为 `xpan` 系列接口的权限门槛（`errno:9019 need verify`，需开发者应用授权）。

---

### C. 分享链接的特殊通道 — ✅ 已打通，但**不加速**（本次最重要的正面发现）

**之前「抓包只看到 `/share/tplconfig`」是不完整的。** 完整链路如下（✅ 浏览器实测抓包）：

分享页点击「下载」→ 弹窗「普通下载 / 高速下载」→ 点「**普通下载**」时，前端按下述顺序发请求：

```
1. GET  /disk/cmsdata?do=manual&ch=download_limit&...        ← 判断是否被限速拦截
2. GET  /share/tplconfig?surl=...&fields=sign,timestamp,jsToken
3. GET  /disk/cmsdata?do=client&...
4. POST /rest/2.0/membership/isp?method=query  (user_id=1)   ← 查会员状态决定是否弹付费墙
5. POST /api/sharedownload?sign=&timestamp=&jsToken=&...     ← ★ 真正取链
```

第 5 步的**真实请求体**（这是关键，之前几次实验都缺参数导致 118）：

```http
POST /api/sharedownload?sign=<sign>&timestamp=<ts>&channel=chunlei&web=1
     &app_id=250528&clienttype=0&jsToken=<96位>&bdstoken=<...>&logid=<...>

encrypt=0&type=dlink&product=share
&uk=1100709536112&primaryid=5317000099
&fid_list=["73179373657953"]
&extra={"sekey":"KwgAa+GOx7yxAx/eVc3zBtZPcJxfjXknqnnYb7+0e2c="}
&vip=0
```

**三个曾经缺失的必需条件**（这就是之前恒得 `errno:118` 的原因）：

| 参数 | 来源 | 缺失后果 |
|---|---|---|
| `jsToken`（96 位） | `/share/tplconfig` 响应 **或** 页面全局 `window.jsToken` | `errno:118 违规操作` |
| `extra={"sekey":...}` | 页面全局 **`window.currentSekey`**（44 字符） | `errno:118` |
| `encrypt=0` + `type=dlink` + `product=share` | 固定值 | `errno:2` |

**✅ 实测结果**：加上全部三项后，`/api/sharedownload` 返回 **`errno:0`** 并直接给出直链（**无需转存**）：

```
https://d.pcs.baidu.com/file/a1b2c3d4e5o6f7a8b9c0d1e2f3a4b5c6
  ?fid=1100709536112-250528-73179373657953&rt=sh&vip=0&...
  &shareid=5317000099&vuk=317526312&expires=8h
```

**但限速没变**（✅ 交替长时对照，每档 15-20s）：

| 通道 | 5s 窗口采样 | 均值 |
|---|---|---|
| `rt=pr`（转存后自盘） | 40, 37, 40, 38 | **39.0 KB/s** |
| `rt=sh`（分享直链） | 30, 25, 25, 40 | **31.3 KB/s** |
| `rt=pr`（第二轮） | 31, 25, 25 | **27.7 KB/s** |
| `rt=sh`（第二轮） | 30, 25, 25 | **27.5 KB/s** |

→ **`rt=sh` 与 `rt=pr` 无显著差异**，两者都被同一个账号级限速压制，且随时间衰减（账号级累积风控，与 03 文档一致）。

**打包下载文件夹**：✅ 实测 `/share/download`、`/share/taskquery`、`/share/downloadtask`、`/api/sharedownloadtask`、`/api/sharedownloadpack`、`/api/sharetaskquery` 全部返回 **`errno:2 请求失败`**；`/share/packdownload`、`/share/tasklist`、`/api/zipdownload`、`/api/mkzip`、`/api/pack` 返回 **404**（端点不存在）。**没有可用的「文件夹打包异步任务」通道。**

---

### D. 异步任务通道 — ❌ 有接口，但普通账号**配额为 0**

这是任务书里最看好的方向，实测结果最明确：**接口真实存在、能调通，但被配额硬卡死。**

**离线下载（cloud_dl）接口全清单**（⚠️ 源码：`BaiduPCS-Go/baidupcs/cloud_dl.go` + 本机 curl 验证）：

| 方法 | URL |
|---|---|
| `list_task` | `POST /rest/2.0/services/cloud_dl?method=list_task&app_id=250528&channel=chunlei&clienttype=0&need_task_info=1&status=255&start=0&limit=10` |
| `add_task` | `POST /rest/2.0/services/cloud_dl?method=add_task&app_id=250528&channel=chunlei&clienttype=0` |
| `query_task` | `GET /rest/2.0/services/cloud_dl?method=query_task&app_id=250528&task_ids=1,2,3` |
| `clear_task` | `POST /rest/2.0/services/cloud_dl?method=clear_task` |

**✅ 实测序列（这是 D 方向的决定性证据）**：

```
① list_task（app_id=250528, channel=0/chunlei）→ 200 {"task_info":[],"total":0}     ← 接口通
② list_task（app_id=498065）                  → 200 {"task_info":[],"total":0}     ← 接口通
③ add_task（非法 URL）                        → 400 {"error_code":36001,"param error"}  ← 参数校验在配额之前
④ add_task（合法 URL: https://pan.baidu.com/img/PCtm_...png）→ 200
     {"error_code":36040,"error_msg":"no times for download",
      "show_msg":"下载次数获取失败或已用完"}                                        ← ★ 配额拒绝
```

**`36040 no times for download` 的性质判定**：

- 这**不是**权限不足（权限不足会返回 `-6` / `-9` / `9019`），**也不是**参数错（参数错已由 ③ 的 `36001` 演示）。
- 它是**明确的业务配额耗尽**：普通账号的离线下载次数 = **0**。
- 因此**离线下载通道对普通账号完全不可用**，不存在「异步产物不受实时限速」的利用空间——**根本下发不了任务**。

**为什么这个方向即便有配额也不划算**（推理，❓ 未验证，因无会员账号）：
离线下载只负责「从外链拉到网盘」，产物落盘后仍是**普通文件**，取回时依旧走 `d.pcs.baidu.com` 并受同一账号限速。此外它只能接受**公网可访问的 URL**，而对「分享链接里的文件」用不上（那需要分享者自己的资源）。

**其他异步端点**：`/api/taskquery` → `{"errno":4,"errmsg":"pcslinkerr"}`；`/api/task/create`、`/api/download/task`、`/share/taskquery` → `errno:2`；`/api/batchtask`、`/api/zipdownload`、`/api/mkzip`、`/api/pack` → **404**。

---

### E. PC 客户端的 API 差异 — ❌ 无可用加速值

| 测点 | 结果 | 状态 |
|---|---|---|
| 客户端 UA `netdisk;P2SP;3.0.0.8;netdisk;11.12.3;ANG-AN00;android-android;10.0;JSbridge4.4.0;...` | `filemetas` 返回 `errno:0`，直链形态与浏览器 UA **完全相同** | ✅ 实测 |
| UA `pan.baidu.com`（alist 官方通道用的 UA） | 同上，无差异 | ✅ 实测 |
| `clienttype=8`（PC 客户端） | `errno:8001` | ✅ 实测 |
| `clienttype=17`（BaiduPCS-Go `locatedownload` 用的值） | `errno:113 验证码签名错误` —— 是独立签名算法（`netdisksign/locatedownloadsign.go`），不是无权限 | ✅ 实测 |
| `/rest/2.0/pcs/file?method=locatedownload&clienttype=17&channel=0&...` | 需 `ant=1&check_blue=1&es=1&esl=1&apn_id=1_0&freeisp=0&queryfree=0&use=0` + 专用 BDUSS 签名 | ⚠️ 源码 |

**`clienttype=17` 这条路线的价值判断**：它是 BaiduPCS-Go 的**上传/下载定位**接口，签名算法在源码中完整可复现（`netdisksign.NewLocateDownloadSign(uid, bduss)`）。但它拿到的仍是**同一条 `d.pcs.baidu.com` 直链**，不改变账号配额。❓ 未完整验证（需实现 `sign2` 算法），但从架构上看**不构成独立带宽池**。

**PC 客户端真正快的原因不是 API**（⚠️ 推理 + 资料）：客户端使用 **P2SP**（点对点 + 服务端加速混合）。UA 里的 `P2SP;3.0.0.8` 就是这一机制。P2SP 的速度来自**其他客户端上传的同一文件分片**，属客户端专有协议（非 HTTP），不是靠某个 `clienttype` 解锁的 HTTP 通道。要用它就得复刻客户端协议栈（需要客户端协商握手、分片索引），这已超出「API 层」范畴。

---

## 3. 可执行实验（按当前 cookie 就能跑）

以下**已全部在本次调研中跑通**（脚本在 `.tmp/api_research/`），可作为回归测试固化。

### 3.1 取分享直链（不转存，✅ 已验证 errno:0）

最简可行做法是**在浏览器页面上下文里执行**（能直接读到 `window.jsToken` / `window.currentSekey`）：

```js
// 1) 打开分享页并输提取码
// 2) 取三件套
const surl = location.pathname.split('/s/')[1];
const tc = await (await fetch(`/share/tplconfig?surl=${surl}&fields=sign,timestamp,jsToken`
        + `&view_mode=1&channel=chunlei&web=1&app_id=250528&clienttype=0`,
        {credentials:'include'})).json();
// 3) 组装（注意：jsToken 与 sekey 都来自 window 全局）
const q = new URLSearchParams({sign: tc.data.sign, timestamp: String(tc.data.timestamp),
    channel:'chunlei', web:'1', app_id:'250528', clienttype:'0', jsToken: window.jsToken});
const body = new URLSearchParams({encrypt:'0', type:'dlink', product:'share',
    uk: String(yunData.share_uk), primaryid: String(yunData.shareid),
    fid_list: JSON.stringify([73179373657953]),
    extra: JSON.stringify({sekey: window.currentSekey}), vip:'0'});
const j = await (await fetch('/api/sharedownload?' + q, {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/x-www-form-urlencoded'}, body})).json();
// j.list[0].dlink  ← errno:0 时有效，rt=sh，expires=8h
```

> 纯 Python 侧注意：`/share/init` 和分享页在无浏览器上下文时返回 **403「页面不存在」**（反爬）。必须复用浏览器会话的 cookie **且**从页面里取 `jsToken`/`currentSekey`。这两个值**不在 cookie 里**，是页面 JS 变量。

### 3.2 取自己网盘直链（✅ 已验证）

```http
GET https://pan.baidu.com/api/filemetas?fsids=[<fs_id>]&dlink=1&web=5&clienttype=0
Cookie: BDUSS=...; STOKEN=...
Referer: https://pan.baidu.com/disk/main
→ {"errno":0,"info":[{"dlink":"https://d.pcs.baidu.com/file/...?rt=pr&..."}]}
```

`target` 形态（`target=["/path"]&origin=dlna`）也能返回直链，但**下载即 403/31329**，见 §4.2。

### 3.3 会员状态（✅ 已验证，只读安全）

```http
GET /rest/2.0/membership/user?method=query&clienttype=0&app_id=250528&web=1
→ current_mvip:[], current_privilege_card:[], current_product:[], level_info.current_level:1
GET /api/gettemplatevariable?clienttype=0&app_id=250528&web=1&fields=["bdstoken","is_vip","vip_type","privilege"]
→ {"result":{"is_vip":0,"vip_type":1,"bdstoken":"..."}}
```

### 3.4 离线下载配额探测（✅ 已验证，**唯一有副作用风险的操作**）

```http
POST /rest/2.0/services/cloud_dl?method=list_task&app_id=250528&channel=chunlei&clienttype=0&need_task_info=1&status=255&start=0&limit=10
```

只读，可用。**不要调 `add_task`**——即便被 `36040` 拒绝，重复请求也可能计入风控。

### 3.5 环境坑（本次踩到，必须记）

- 百度部分接口返回 **假的 `content-encoding: gzip`**，requests 自动解压会抛 `ContentDecodingError`。必须设 `Accept-Encoding: identity` 并 `raw.read(decode_content=False)`。
- `/share/list` 需要 **`sekey`** 参数（页面从 `window.currentSekey` 拿），否则 `errno:2`。
- `/share/init` 的 `surl` 是**去掉首字符**的形式（`1DSrRjs...` → `DSrRjs...`）。
- `web_search` 后端本机 401 失效；GitHub / raw.githubusercontent 被墙（`api.github.com` 可用但易限流）；Bing 中文结果被 SEO 垃圾污染。公开资料补充困难，故本报告以**实测为主、源码为辅**。

---

## 4. 重点推荐

### 4.1 结论先行：不要再投入「找漏」

**D 方向（异步任务）已判定死亡**：接口通但配额为 0（`36040`）。这不是「还没找到方法」，是服务端明确说了「你的下载次数用完了」。

**建议把工程力气转向这三件事**：

1. **分享直链直取**（§3.1，✅ 已打通）——省掉「转存」这一步，显著改善**用户体验**（不污染网盘、不受转存配额限制），但**不提速**。这是当前**唯一值得写进代码**的 API 成果。
2. **接受限速，优化工程指标**：既然带宽恒定 27-72 KB/s 且并发越高越差（03 文档），正确策略是 **单连接 + 断点续传 + 长时间稳定**，并做「检测到 403/降速就退回单连接」的降级。提高的是**成功率与可预期性**，不是速度。
3. **对用户如实标注**：界面上明确写「百度网盘非会员限速由服务端按账号强制，本工具无法突破」。这比给一个虚假的「竞速」开关诚实，也避免用户误以为是工具不行。

### 4.2 需要记住的两个「已死」路径（避免后人重踩）

| 路径 | 实测结果 | 判定 |
|---|---|---|
| `origin=dlna` 媒体伪装（alist `crack`） | 取链成功 → 下载 **403 `31329 hit black userlist, hit illeage dlna`** | **服务端已封禁** |
| `media=1 + android channel`（alist `crack_video`） | 取链成功 → 下载 **403 `31329`** 同上 | **服务端已封禁** |
| 离线下载 `add_task` | **`36040 no times for download`** | **配额为 0** |
| `vip=2` / `type=nolimit` 伪造 | 直链回显 `vip=2`，速度无变化 | **仅回显，且涉协议风险** |
| 文件夹打包异步下载 | 端点全 `errno:2` 或 404 | **不存在** |
| 换 `app_id`（498065 等） | 直链形态完全一致 | **无限速差异** |
| 换 `clienttype`（0/1/3/8/17） | 0/1 正常；8→8001；17→签名错 | **无加速值** |

### 4.3 如果仍想做「值 1 小时」的最后验证

只有**一个**测点尚未排除，且**需要会员账号**（用户已否决多账号，故大概率跳过）：

> **会员账号的直链是否解除账号级限速**——这是 03 文档的遗留待办，也是判定「限速=纯账号维度」的最后一环。

- 若会员账号单连接显著 > 1 MB/s → 证实「限速严格绑定 `vip` 状态」，则任何 API 手段都无解（因为状态在服务端）。
- 若会员账号也慢 → 说明还有 IP/地域维度，需要换网络环境再测。

**不建议**做「找人借会员账号测一下」之外的任何尝试。

---

## 5. 参考来源

### 5.1 源码（本项目内已下载，⚠️ 一手代码证据）

| 文件 | 提供的关键信息 |
|---|---|
| [research/src-openlist/meta.go](src-openlist/meta.go) | `download_api` 选项 `official/crack/crack_video`，`custom_crack_ua` 默认 `netdisk` |
| [research/src-openlist/util.go](src-openlist/util.go) | `linkCrack`（`origin=dlna`）、`linkCrackVideo`（`android_15_...` channel + `media=1`）、`linkOfficial`（`/xpan/multimedia`）三套取链实现 |
| [research/src-alist/util.go](src-alist/util.go) | 同上（alist 上游），确认两代实现一致 |
| `BaiduPCS-Go/baidupcs/cloud_dl.go` | `cloud_dl` 全部方法、状态码 0-7 含义、`task_info` 字段 |
| `BaiduPCS-Go/baidupcs/prepare.go` | `locatedownload` 完整参数表（`ant/check_blue/es/esl/clienttype=17/channel=0/apn_id/freeisp/queryfree/use`）、`/api/download`（`fidlist`）、`filemetas` 用法 |
| `BaiduPCS-Go/baidupcs/baidupcs.go` | `PanAppID = "250528"`、`NetdiskUA = "netdisk;P2SP;3.0.0.8;..."` |
| `BaiduPCS-Go/baidupcs/netdisksign/locatedownloadsign.go` | `clienttype=17` 的专用签名算法 |
| `BaiduPCS-Go/baidupcs/transfer.go` | `/share/transfer?app_id=250528&channel=chunlei&clienttype=0&web=1` |

来源仓库：`github.com/qjfoidnh/BaiduPCS-Go`（`main` 分支）、`github.com/OpenListTeam/OpenList`、`github.com/AlistGo/alist` 的 `drivers/baidu_netdisk`。

### 5.2 本机实测记录（✅ 一手数据）

| 脚本 | 覆盖 |
|---|---|
| `.tmp/api_research/t1.py` | 会员状态 4 个接口、`cloud_dl list_task` 3 个 app_id/channel 变体、`/api/download`、UA 对 `filemetas` 的影响 |
| `.tmp/api_research/t2.py` | 真实 fs_id、`cloud_dl` 完整字段、`add_task` 权限探测、打包端点、`/api/download` 真实 fid |
| `.tmp/api_research/t3.py` | `add_task` 合法 URL → **`36040`**、`query_sinfo`、会员特权端点 404 |
| `.tmp/api_research/probe_share.py` | 分享页 `share/init` 反爬表现（403） |
| 浏览器抓包（`dsh_browser_execute` 钩子 fetch/XHR） | 分享页「普通下载」5 步完整请求链、`/api/sharedownload` 真实 body |
| `.tmp/api_research/probe_crack.py` / `probe_crack2.py` | `crack` / `crack_video` 参数穷举，`errno:12` / `31023` 成因 |
| `.tmp/api_research/probe_crack3.py` | **crack 取链成功但下载 403/31329**、对照 `normal` 通道 71.8 KB/s |
| `.tmp/api_research/probe_pack.py` | 文件夹打包 8 个端点探测 |
| 直链测速（交替 4 轮） | `rt=sh` vs `rt=pr` 无差异：39.0 / 31.3 / 27.7 / 27.5 KB/s |

### 5.3 项目内相关文档

- [docs/BAIDU-SPEEDTEST.md](../docs/BAIDU-SPEEDTEST.md) — 限速基线（单连接 80 KB/s、聚合 100-130 KB/s、账号级封顶）
- [research/02-client-protocol.md](02-client-protocol.md)、[research/03-network-layer.md](03-network-layer.md) — 客户端协议与网络层分析

### 5.4 公开资料（❓ 受限，未能充分覆盖）

本次调研中 `web_search` 后端鉴权失效（401）、GitHub 与镜像站均不可达、Bing 中文结果被 SEO 污染，**未能取得 2025-2026 年的社区讨论/官方文档佐证**。报告的可靠性建立在**源码 + 本机实测**两条独立证据链上，且结论多为**否证**（证明不可行），不需要依赖外部资料背书。若需补强「百度官方对限速的说明」，建议换网络环境后检索：

- `pan.baidu.com/disk/download` 帮助页中的「非会员下载速度说明」
- 百度网盘开放平台 `pan.baidu.com/union/doc`（`xpan` 官方 API 文档，含 `errno:9019` 说明）

---

## 6. 核心判断

> **API 层没有漏。**
>
> 三条可能的漏在 2026-11 全部被堵死，且堵法各不相同，构成了完整的封堵面：
> 1. **渠道伪装**（dlna / 安卓 channel）→ 取链能过，**下载网关按来源封禁**（`31329`）。封在最后一跳。
> 2. **异步任务**（离线下载）→ 接口全通，**业务配额为 0**（`36040`）。封在业务层。
> 3. **身份伪造**（`vip=2` / `type=nolimit`）→ 参数被接受甚至回显，**速率不变**。封在计算层。
>
> 限速是在**签发直链时按账号真实身份绑定**的服务端配额，不属于任何客户端可影响的参数面。因此**API 层攻击面已经关闭**，剩下的唯一变量是**真实的付费状态**——那是产品层面的选择，不是技术问题。
>
> 本工具对百度网盘应**明确按「限速平台」适配**：单连接、断点续传、稳定优先、不承诺提速。
