# 踩坑与经验（2026-11-16 一轮长调试）

> 这份文档只记**实测得到的、会反复踩的**事实，不写设计愿景。
> 每条都注明「症状 → 根因 → 对策」，方便下次 30 秒内定位。

---

## 一、百度网盘

### 1.1 两套签名，能不能离线用完全看这个

| 来源 | sign 前缀 | 关键参数 | 要 cookie | 外部可用 |
|---|---|---|---|---|
| 网页 `api/filemetas` | `FDtAER-` | `clienttype=0` | 要 BDUSS | ❌ 一律 `31362 sign error` |
| xpan `rest/2.0/xpan/multimedia?method=filemetas` | `FDtAERK-` | `access_token=123.xxx` | 不要 | ✅ 302 → `*.baidupcs.com` → 206 |

- **症状**：文件列表能拉，取直链却 403。
- **根因**：网页那套 dlink 绑定了网页自身的下载上下文，脱离就签名失效（连同一个浏览器、
  同一秒、带 Referer、换 `clienttype=12` 都试过，全部 31362）。
- **对策**：走 xpan + `access_token`。

### 1.2 `api/filemetas` 的参数名是 `fsids`，不是 `target`

- **症状**：不管文件存不存在，都返回 `errno=12` + `info:[{errno:31021}]`。
- **根因**：`target` 是**分享**接口的参数；用错名字时百度回个和文件无关的通用错误，
  极易误判成「文件被屏蔽」。
- **对策**：`?fsids=["<fs_id>"]&dlink=1`。

### 1.3 `access_token` 只能走开放平台 OAuth

- 网页会话换不出来：`gettemplatevariable`、`window.locals`、`/api/oauth/gettoken`
  都不给。
- 设备码模式可行（本项目 `src/dlink/baidu_oauth.py`）：`/oauth/2.0/device/code`
  → 用户输授权码 → 轮询 `/oauth/2.0/token` 拿 `access_token` + `refresh_token`。
- **注意**：拿到 token 后 `filemetas` 返回的直链**不含 access_token 参数**，
  要自己拼 `&access_token=<tok>`，否则 CDN 报 `31045 user not exists`。
- ⚠️ **待确认**：用开发者中心（带 SignKey）的凭据拿到的 token，元数据接口都通，
  但 CDN 只给响应头、不吐 body（`ChunkedEncodingError`）。怀疑应用能力/审核级别
  不够，需要在**网盘开放平台**（pan.baidu.com/union）创建并过审。

### 1.4 账号级限流会「突然全挂」

- **症状**：同一账号下**所有**文件的直链都变成「返回 206 + 正确 Content-Range，
  然后连接被掐，body 0 字节」。
- **根因**：短时间大量并发/失败请求触发账号或出口 IP 的临时限制。
- **对策**：停手等一段时间；不要反复重试（会延长恢复）。
  已下载的分段保留在 `<dest>.parts`，恢复后可续。

### 1.5 限速是账号级，别指望并发

免费账号 80–130 KB/s，8 路反而掉到 9 KB/s。工具不做绕过。

---

## 二、腾讯微云

### 2.1 403 空 body 的三种可能（按概率排序）

1. **缺 `Accept` 头**：必须带 `Accept: application/json, text/plain, */*`，缺了必 403。
2. **`g_tk` 与 cookie 的 `skey` 不匹配**：`g_tk` 按域绑定
   （`share.weiyun.com` 与 `www.weiyun.com` 不同）。
3. **游客身份下载超限文件**：见 2.2。

### 2.2 分享页对「大文件」直接禁下载

- **症状**：点「普通下载」弹窗：
  > **下载文件大小超过限制** — 无法在当前页面直接下载，请转存到微云后下载，
  > 或使用最新微云客户端直接下载。
- **本机实测**：417 MB 的分享文件即触发（不是几 GB）。
- **对策**：转存到自己的微云 → 从自己空间下载；或用客户端。
- 昨晚 1.53GB 能跑通（32MB × 8 路，约 2 分钟）说明**下载器本身没问题**，
  限制在「分享页的下载入口」。

### 2.3 `skey` 是 HttpOnly，这是扩展必须存在的原因之一

- 页面 JS `document.cookie` 里**没有** `skey`，所以 `g_tk` 算不出来（只能发 0）。
- 扩展里可以用 `chrome.cookies.getAll({url:'https://share.weiyun.com'})` 读到
  `skey`（含 HttpOnly），算对 `g_tk` 再传给页面脚本 —— 这是唯一正确路径。
- 备选：`performance.getEntriesByType('resource')` 能读到页面自己发过的请求 URL，
  里面带真实 `g_tk`（实测可取到 732187767）。属于兜底手段。

### 2.4 文件夹分享会让解析器「看不到文件」

- **症状**：分享根是一个文件夹，解析器返回 EMPTY。
- **根因**：页面 Vue store 里，子目录的 `_kid_files` **要等用户点进去才会加载**。
  直接遍历 store 只能看到根层的 dir 节点。
- **对策**：对未加载的目录调 `weiyunShareNoLogin/WeiyunShareDirList`（需正确
  `g_tk` 与 `cmd`），或在页面里先导航进目录。

---

## 三、通用（所有平台）

### 3.1 Referer 不能一把梭

- **症状**：百度直链请求返回 `31326 user is not authorized, hitcode:109`。
- **根因**：下载器给**所有**任务默认塞了 `Referer: https://www.weiyun.com/`。
- **对策**：按域名给 Referer（baidu→pan.baidu.com，quark→pan.quark.cn，
  uc→drive.uc.cn）。

### 3.2 UA 要看平台

- 百度带 `access_token` 的直链：UA `pan.baidu.com` 或 `netdisk` 都行。
- 网页那套 dlink：浏览器 UA 会被拒成 `31326 user is not authorized, hitcode:119`。
- **对策**：URL 含 `access_token` 且是百度域 → UA 用 `pan.baidu.com`。

### 3.3 分段布局一变，续传就白费

- 下载器按 `<dest>.parts/part_NNN` 续传，**布局由 `workers × seg_size` 决定**。
- 换 workers 或 seg_size → 分段区间变了 → 旧分段作废，等于重下。
- **对策**：续传时保持与上次相同的 workers/seg_size；改之前先想清楚。

### 3.4 失败要释放文件名预占

- **症状**：连续失败几次后，文件被存成 `PC(3).7z`。
- **根因**：`unique_dest()` 会预占文件名，失败路径没释放。
- **对策**：任务真失败时 `release_dest()`；续链（NEED_REFRESH）不能释放。

### 3.5 打包成 windowed exe 的两个坑

1. `sys.stderr` 是 `None` → `log_message` 里 `write` 抛异常，把请求线程带走，
   表现是「所有 POST 连接被重置，而 GET /api/health 正常」。必须判空。
2. 单文件版启动要解压到 `%TEMP%`，失败时 PyInstaller 只弹
   `could not create temporary directory` 就退出，Python 代码一行没跑。
   备一个 onedir 目录版。

### 3.6 浏览器自动化里点「下载」要用真实鼠标事件

- JS 的 `el.click()` 是**不可信**手势，浏览器会拦下载（无提示、无反应）。
- 必须用 CDP 的 `Input.dispatchMouseEvent` 坐标点击，才等价于用户点击。

---

## 四、平台申请（百度开放平台）

- `pan.baidu.com/union`（网盘开放平台）与 `developer.baidu.com`（开发者中心）
  **不是一个东西**：后者发的凭据带 `SignKey`，前者发 `API Key`/`Secret Key`。
- 网盘开放平台控制台如果显示「暂无应用」，说明应用建在别处。
- 申请文案见 `docs/BAIDU-APP-APPLICATION.md`（**别写成「分享链接解析工具」**，
  写「下载自己的文件」，通过率高得多）。

---

## 五、当前结论（给未来的自己）

| 平台 | 能做到什么 | 卡在哪 |
|---|---|---|
| 普通 HTTP | 全自动，多线程有效 | — |
| 腾讯微云 | 列表可读；<=限额的小文件可直下 | 大文件分享页禁下载；`skey` 需扩展读取 |
| 百度网盘 | 列表/转存可做；token 可授权 | CDN 只认 xpan token，且本机应用疑似未获下载权限 |
| 夸克/UC | 代码就绪 | 未实测 |

**工作方式**：需要下载时喊我，我按真实登录态现场取链、现场试，
不通就如实说明卡在哪一层（平台限制 vs 我们的 bug），不硬编「万能解析」。

---

## 六、微云「自己空间下载」的完整链路（2026-11-16 打通，最有用的一条）

> 分享页对超限文件关死了直链，但**自己空间**没这个限制。
> 下面这套实测能让 1.3GB 文件以 2MB/s、8 路稳定下完。

### 6.1 两个接口不是一回事

| 场景 | 接口 | 结果 |
|---|---|---|
| 分享页直下 | `weiyunShare/WeiyunShareBatchDownload`（cmd 12024） | 超限文件一律 `24203 服务器繁忙` |
| 自己空间直下 | `weiyunQdiskClient/DiskFileBatchDownload`（cmd **2402**） | `retcode 0`，返回可用下载信息 |

请求体要点（自己空间那套）：

```json
{"req_header":"<JSON字符串：seq/type=1/cmd=2402/appid=30151/version=3.3.3.3/wx_openid/user_flag=1/env_id>",
 "req_body":"<JSON字符串：ReqMsg_body.ext_req_head + '.weiyun.DiskFileBatchDownloadMsgReq_body'，
             内含 file_list:[{file_id, pdir_key, filename, file_size}]>"}
```

响应里每个文件给三样东西：`encode_url`、`server_name`、`cookie_name/cookie_value`。
**`cookie_value` 每个文件不同** —— 拿 A 文件的 FTN5K 去下 B 文件会 403。

### 6.2 下载 URL 的真实形状

```
https://<server_name>/ftn_handler/<encode_url>/<文件名>?fname=<文件名>&from=30151&version=3.3.3.3
Cookie: FTN5K=<cookie_value>
```

少任何一段都是 `500` 或 `403`：

| 少了什么 | 现象 |
|---|---|
| `/文件名?fname=…&from=…&version=…` | `500` 空 body |
| 正确的 `FTN5K` | `403` |
| Range 头 | 能下但拿不到断点续传 |

### 6.3 怎么从页面拿到 encode_url

页面把真实下载地址上报到了统计接口，**decode 那个 URL 就能读到**：

```js
performance.getEntriesByType('resource').map(e => e.name)
  .filter(n => n.includes('trace.qq.com') && n.includes('weiyun_web_report_download'))
  .map(u => decodeURIComponent(u.match(/[?&]content=([^&]+)/)[1]))
  .map(s => JSON.parse(s)?.log?.[0]?.content?.[0]?.dimensions || [])
```

经验：**用过的 encode_url 不会失效**（可在浏览器下载的同时给 Python 用）。

### 6.4 Python 直连 API 会 403，但 CDN 不会

| 目标 | Python requests | 说明 |
|---|---|---|
| `www.weiyun.com/webapp/json/...` | ❌ 403 空 body | requests 的 TLS/HTTP 指纹和 Chrome 不同，被网关拦 |
| `gzi-download.weiyun.com/ftn_handler/...` | ✅ 200/206 | CDN 只看 FTN5K，不看指纹 |

**所以正确姿势是「浏览器取链 → Python 多线程下载」**，
而不是「Python 全自动」。这也解释了为什么抓包工具能看、脚本却调不通。

### 6.5 微信登录没有 skey

`g_tk` 由 `skey` 算，但微信登录的微云账号**根本没有 skey**（只有
`weiyun_wx_openid` / `weiyun_wx_access_token` / `wyctoken`），
hash33 那九个值都试过，全都对不上页面真实 `g_tk`（它是服务端会话态）。

**结论**：微云的 `g_tk` 只能从页面请求里借，不要试图算。

### 6.6 实测速度

| 文件 | 大小 | 8 路速度 | 耗时 |
|---|---|---|---|
| LustFriend/PC.7z | 417 MB | 2.0 MB/s | 约 3.5 分钟 |
| 不可思议之国的淫魔/PC.7z | 619 MB | 2.0 MB/s（后半 0.7） | 约 5 分钟 |
| 别被诱惑所迷/PC.7z | 1.3 GB | 2.0 MB/s | 约 13 分钟 |

工具：`scripts/dl_url.py`（按 URL 分段并行，支持断点续传）。
