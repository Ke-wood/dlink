# 05 — 夸克网盘 / UC 网盘：直链获取与限速特征

> 研究日期：2026-11-15
> 方法：开源项目源码逐行确认 + 真实端点实打实探测 + 上游 issue 一手用户数据
> 结论置信度标注：**【源码确认】**= 有可复制的源码行；**【实测】**= 本机真发请求验证；**【推断】**= 由证据推导，未直测

---

## 1. 一句话结论

**夸克/UC 适合做并行下载，但要"限并发 + 自适应降级"**：直链走阿里 CDN，支持 Range，多连接确实能提速（非会员 aria2 多线程明显快于单线程下载器），**但存在一个"先给高速、十几分钟后掐到恒定低速"的账号/会话级动态限速闸门**——被掐之后每连接掉到约 100KB/s。所以它是**"连接级限速 + 账号级软配额"的混合体**，既不像微云那样纯靠并发解决，也不像百度那样并发完全无效。策略上应该：**8~16 路起手跑满，持续监测聚合速率，一旦检测到断崖式下跌（判据见 §4.4）就停止加并发、切换为多文件轮转 + 重取链接**，而不是无脑堆连接。

---

## 2. 完整 API 调用链（可直接照抄实现）

### 2.1 两个域名的分工（重要，别混用）【源码确认】

| 用途 | 域名 | 说明 |
|---|---|---|
| 分享/元数据/列目录 | `https://pan.quark.cn/1/clouddrive` | 网页端 API |
| **取直链** | `https://drive-pc.quark.cn/1/clouddrive` | **PC 客户端 API，必须用这个** |
| 取直链（备选写法） | `https://drive.quark.cn/1/clouddrive` | AList 用的就是它，同样可用 |

> 源码依据：
> - `userscript_quark_to_gopeed.js:21-23` —— token/detail 走 `pan.quark.cn`，download 走 `drive-pc.quark.cn`
> - `alist_meta.go:40` —— `api: "https://drive.quark.cn/1/clouddrive"`
> - `qdl_api.rs:6-7` —— `API_BASE_URL = pan.quark.cn`，`DRIVE_BASE_URL = drive-pc.quark.cn`

**我实测**：`drive.quark.cn`、`drive-pc.quark.cn` 解析到同一 IP `203.119.252.101`（阿里 zb-443 wagbridge）；`pan.quark.cn`、`download.quark.cn` 解析到 `203.119.238.190`。四个域名都是阿里系，没有被墙风险。

---

### 2.2 分享链接解析【源码确认 `qdl_parser.rs:17-55`】

支持三种格式：

```
https://pan.quark.cn/s/<pwd_id>
https://pan.quark.cn/s/<pwd_id>?pwd=1234
https://pan.quark.cn/s/<pwd_id>#/list/share/<pdir_fid>
```

- `pwd_id`：正则 `/s/([A-Za-z0-9]+)`；如果整串就是个 10 位以上的纯字母数字，直接当 pwd_id
- `passcode`：正则 `[?&](pwd|password|pw)=([A-Za-z0-9]{4})`
- `pdir_fid`：正则 `#/list/share/([A-Za-z0-9]+)`，缺省 `"0"`（根目录）

---

### 2.3 步骤① 换 stoken

```
POST https://pan.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc
Content-Type: application/json;charset=UTF-8

{"pwd_id":"<pwd_id>","passcode":"<4位提取码，无则空串>"}
```

响应 `data.stoken` 即为后续令牌。

**错误码**【源码确认 `userscript:268-275`】：
- `31001` 或 message 含"提取码" → 需要提取码
- `31002` → 分享已失效/取消
- `41006` → **我已实测**：分享不存在（用假 pwd_id 打到真实端点，HTTP 404 + code 41006）

> **【实测】该端点 GET 会返回 405**（`Request method 'GET' not supported`，Spring 框架报错），证明必须 POST。这是一个很好的"链路正确性"自检点。

---

### 2.4 步骤② 列目录

```
GET https://pan.quark.cn/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc
    &pwd_id=<pwd_id>
    &stoken=<urlencode(stoken)>
    &pdir_fid=<目录fid，根目录用 0>
    &_page=1
    &_size=100
    &_sort=file_type:asc,updated_at:desc
```

响应 `data.list[]`，每项关键字段【源码确认 `userscript:304-312`】：

| 字段 | 含义 |
|---|---|
| `fid` | 文件 ID |
| **`share_fid_token`** | **取直链必需的令牌**（注意字段名是 share_fid_token，不是 fid_token） |
| `file_name` | 文件名 |
| `size` | 字节数（字符串或数字） |
| `dir` | 是否目录 |

分页终止条件：`list.length < _size`（源码用 `_size=100`，最多翻 50 页）。

---

### 2.5 步骤③ 取直链（游客模式，**不需要 cookie**）

```
POST https://drive-pc.quark.cn/1/clouddrive/file/download?entry=ft&fr=pc&pr=ucpro
Content-Type: application/json;charset=UTF-8
User-Agent: <夸克PC客户端UA，见 §3>

{
  "fids":       ["<fid1>", "<fid2>"],
  "fids_token": ["<share_fid_token1>", "<share_fid_token2>"],
  "pwd_id":     "<pwd_id>",
  "stoken":     "<stoken>"
}
```

响应 `data[]`（或 `data.list[]`），每项 `fid` + **`download_url`**。【源码确认 `userscript:318-360`、`alist_types.go:131`】

**关键错误码**：
- `23018` → **游客大小限制**。源码注释直白：【源码确认 `userscript:25`】"取直链必须伪装成夸克 PC 客户端，否则会被判成「游客」并受大小限制（23018）"
- `14001` → **我已实测**：`Bad Parameter: [分享链接不存在]`（HTTP 400）
- `31001` → **我已实测**：`require login [share missing]`（HTTP 401，未带有效 cookie/stoken 时）
- 批量：`LINK_BATCH = 15`（一次最多 15 个 fid），`BULK_LIMIT = 500`

> ⚠️ **防坑（我实测确认）**：路径是 **`/file/download`**，**不存在 `/share/sharepage/download`**。
> 后者实测返回 `HTTP 404 {"status":404,"error":"Not Found","path":"/1/clouddrive/share/sharepage/download"}`。
> 网上不少二手资料把它写成 `sharepage/download`，照抄必然 404。分享文件的取链接走的是**同一个 `/file/download`**，只是 body 里多带 `fids_token`/`pwd_id`/`stoken`。

> **【实测】确认**：向真实端点 POST 假参数 → HTTP 400 + code 14001，说明参数结构正确、只差有效 fid。

---

### 2.6 步骤③' 取直链（已登录 / 已转存到自己的网盘）

```
POST https://drive-pc.quark.cn/1/clouddrive/file/download?pr=ucpro&fr=pc
Cookie: <登录 cookie>
User-Agent: <夸克PC客户端UA>

{"fids":["<fid>"]}
```

【源码确认 `alist_util.go:180-224`】

**转存路径**（分享文件超过免费空间时的必需流程）【源码确认 `qdl_api.rs:142-168`】：

```
POST https://drive-pc.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc&__dt=<ms时间戳>

{
  "fid_list":       ["<fid>"],
  "fid_token_list": ["<share_fid_token>"],
  "to_pdir_fid":    "0",
  "pwd_id":         "<pwd_id>",
  "stoken":         "<stoken>",
  "pdir_fid":       "0",
  "scene":          "link"
}
```
→ 返回 `task_id` → 轮询 `GET /1/clouddrive/task?pr=ucpro&fr=pc&task_id=<id>&__dt=<ms>` → `data.status==2` 成功（取 `data.save_as.save_as_top_fids`），`status==3` 失败（空间满/风控）。

---

### 2.7 直链本身怎么下载（这一步决定成败）

【源码确认 `alist_util.go:214-221`】——**直链不是裸链接，必须带三个 header**：

```http
GET <download_url>
Cookie:    <与请求 /file/download 时完全相同的 cookie>
Referer:   https://pan.quark.cn
User-Agent: <与取链接时完全相同的 UA>
Range:     bytes=0-0          ← 探活/续传用
```

> 源码里有一段极重要的注释【源码确认 `alist_util.go:194-196`】：
> "下载 URL 的签名基于请求 `/file/download` 时携带的 cookie 生成，下载请求头必须与之一致，否则会被上游判定签名无效返回 **403**。请求和下载头使用同一个快照，避免定时刷新并发修改 d.Cookie 导致两者不一致。"
>
> **对下载器的直接含义**：cookie / UA 要和取直链那一刻**严格一致**，否则 403；`__puus` 会被服务端轮换，所以并发下要"取链接 + 下载"绑成一对快照，不能中途热更新。

---

## 3. 鉴权明细

### 3.1 两条路线的鉴权需求

| 场景 | 需要什么 | 来源 |
|---|---|---|
| **游客分享直链**（免登录） | **只要 pwd_id + stoken + share_fid_token**，不需要任何 cookie | 【源码确认 userscript 就是纯前端脚本，无 cookie】 |
| 自己网盘 / 已转存文件 | `Cookie`（含 `__puus`）+ 夸克PC客户端 UA | 【源码确认 `alist_util.go`】 |

### 3.2 Cookie 细节【源码确认 `alist_util.go:56-78,270-297`】

- 必需 cookie 名：**`__puus`**（会话令牌，**有效期约 3 小时**）
- 可选：`__pus`（仅当开启"转码地址"时）
- **服务端只在请求缺失 `__puus` 时才下发新的** → AList 的刷新手法是：故意去掉 `__puus` 发一次 `GET /config`，让服务端重新下发
- 刷新节奏：100 分钟一次 + ±5 分钟随机抖动（避免多实例同刻集中刷新）
- 过期表现：下载 403（AList issue #830）

### 3.3 UA 是硬性要求（夸克对 UA 极敏感，题目里猜对了）

【源码确认】

**夸克**（`alist_meta.go:38` / `userscript:26-29`）：
```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 Channel/pckk_other_ch
```
> 注意：AList 用 `2.5.20` 版本号，userscript 用 `3.20.0`。**两者都能工作**；如果遇到 23018，优先往上调版本号。关键是含 **`quark-cloud-drive/<版本>` + `Channel/pckk_other_ch`** 这段特征。

**UC**（`alist_meta.go:54`）：
```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch
```

### 3.4 全量请求头模板

**【源码确认 `qdl_api.rs:12-20`】**

```http
Cookie: <cookie>
User-Agent: <对应平台的 cloud-drive UA>
Referer: https://pan.quark.cn
Origin:  https://pan.quark.cn
Content-Type: application/json;charset=UTF-8
Accept: application/json, text/plain, */*
```

查询串统一带：`pr=ucpro`（夸克）/ `pr=UCBrowser`（UC），`fr=pc`；动态加 `__dt=<毫秒时间戳>` 防缓存。

---

## 4. 限速特征

### 4.1 直链域名与 CDN【实测 + 一手用户数据】

- 直链主机：**`download.quark.cn`**（DNS 实测存在，`203.119.238.190`，Server: **Tengine**，阿里系）
- 实际下载节点（用户实测报告）：**`dl-pc-zb.drive.quark.cn`**，**阿里 alikunlun CDN，走 IPv6**
- 我实测 `https://download.quark.cn/` 返回 `{"status":404,"message":"Not Found"}`，说明域名活着、只是路径需要签名

### 4.2 Range 支持

**【源码确认】**：`qdl_api.rs:279-289` 的 `check_link_expire()` 就是用 `Range: bytes=0-0` 探活，判据是"非 2xx 即失效"——说明官方工具链就假设直链吃 Range。
**【源码确认】**：AList 对夸克直链显式设置 `Concurrency: 3, PartSize: 10MB`（`openlist_util.go:134-135`），新版可配 `down_concurrency`/`down_part_size`，默认 3 路 × 10MB 分片。
**【推断】**：支持 206，且服务端按分片给速；否则 AList 不会把分片下载做成默认开箱能力。

> ⚠️ 我**没有**用真实直链跑 206 实测——需要有效 cookie/分享，本机没有。这留作下一步（见 §6）。

### 4.3 ★核心证据：限速曲线（一手数据，88VIP 会员账号）

来源：[ElyDemiurge/fnOS-quark issue #6](https://github.com/ElyDemiurge/fnOS-quark/issues/6)（已存 `issue_fnos_quark6.json` + 回复 `issue_fnos_quark6_comments.json`）

测试条件：88VIP（`EXP_SVIP`，未过期）、并发 3、**单文件连接 6**、总限速 0、15 文件约 55GB、单文件约 4GB。

```
13:07   15.67 MB/s
13:10   19.72 MB/s   ← 峰值
13:18   18.54 MB/s
13:19    2.08 MB/s   ← 断崖式下跌
13:20~13:52  稳定 1.7 ~ 2.2 MB/s（不复原）
```

**报告者自己算出的关键比值**：
> "限速后每个文件单独都是 **0.6 MB/s ÷ 6 连接 ≈ 100 KB/s/连接**，数字非常齐整、几乎不抖（不像链路拥塞）"

**已排除的本地因素**（报告质量很高）：同机 curl 阿里云 21MB/s、清华 37MB/s、腾讯 74MB/s（带宽正常）；网卡 2500Mbps 零丢包；iostat 磁盘 0% 忙；无透明代理；应用总限速=0；日志 0 次重试、0 次 429 → **不是重试/风控报错，是稳定的低速**。

### 4.4 ★维护者回复：这是服务端限速，且有**时段性**

> 作者 ElyDemiurge：
> "同一账号在不同网络环境下（WiFi、蜂窝）如果被限速的话，那么**官方网盘客户端的下载速度也同样会发生限速**。这个限速应当是**夸克服务器的限速，限速的大致时间段为晚 6 点到晚 11 点左右**，这个问题应该无解。"

**这段是全篇最有价值的判断依据**：
1. **官方客户端同样被限** → 不是第三方工具的通道差异，是账号级
2. **跨网络环境一致**（WiFi / 蜂窝都限）→ 不是 IP 级、不是链路级
3. **有明确时段（18:00–23:00）** → 是**按账号 + 时段的动态配额（soft quota）**

### 4.5 支持"并发确实有用"的反向证据

同仓库 issue #7（非会员用户）：
> "我一直用 openlist 挂载夸克网盘 + aria2 作为下载器，之前和您这个项目对比过，相比之下 **aria2 下载速度更快（非会员情况下）**。"

aria2 的默认行为就是多连接分片——**这说明在未被掐的窗口期内，多连接确实能提速**。

### 4.6 其它速度数据点

| 来源 | 数据 |
|---|---|
| fnOS-quark #2 | "权游全集，刚开始稳定 30+，过一会变成 500K~2M" |
| fnOS-quark #2 (Elecboy5) | "88VIP 会员昨天正常，今天只有 400K" |
| fnOS-quark #6 (wanchayuxia-prog) | "千兆宽带，初期 18M/s，后来 1.8MB/s" |
| Alist issue #2097 | "夸克开了会员，webdav 下载，**仍旧限速 2M**"；维护者答"夸克云盘本身限速" |
| syhyz1990/baiduyun #493 | "都已开通年费会员，之前差不多 **70M/s**，现在只有 2M/s" |

### 4.7 结论：夸克/UC 属于哪一类

**不是纯连接级，也不是纯账号级，而是"二阶段"：**

| 阶段 | 表现 | 机制 | 并发是否有效 |
|---|---|---|---|
| **窗口期**（未被掐） | 15~20MB/s（会员），可达 70MB/s（好时段） | CDN 正常给量 | ✅ **有效**，多连接线性提升 |
| **限流期**（被掐后） | 恒定 ~1.8MB/s，每连接 ~100KB/s | 账号级时段配额 | ⚠️ **疑似仍然有效但天花板低**：100KB/s/连接 × 16 路 ≈ 1.6MB/s，与总量 1.8MB/s 持平 → 说明**总量被账号配额锁死，堆连接没用** |

**推断（明确标注为推断）**：被掐后"每文件 0.6MB/s、每连接 100KB/s"这种整齐数字，加上总量恒定 1.8MB/s 不随文件数增加，更像**账号级总配额 ≈2MB/s**，单连接 100KB/s 只是**并发分摊的结果而非独立限制**。若如此，**限流期内加并发无效**——这与百度的账号级配额在"并发无效"这一点上表现相同，只是流量地板更高（2MB/s vs 80KB/s）。

> ⚠️ 这条推断**还没有人做过对照实验验证**。原始 issue 里报告者就问了这个（"单文件连接数 6→12、并发 3→5 是否有效？"），维护者没有回帖给出数据，issue 已关闭。**这是我们能贡献的独特价值——§6 给了实验设计。**

### 4.8 判定判据（给下载器用）

```
聚合速率 < 3MB/s 且 持续 > 30s 且 抖动系数 < 0.15   → 判定进入限流期
  → 停止增加并发；改为多文件轮转（同时开 N 个小文件而非分片 1 个大文件）
  → 尝试重新取直链（新签名 = 新会话窗口）
  → 若仍 <2.5MB/s 且时段在 18:00-23:00 → 接受 2MB/s 地板，不再挣扎
```

---

## 5. 与 UC 的差异

### 5.1 结论：**API 同构，驱动共用**——AList 用一个 `QuarkOrUC` 结构体同时服务两者，只靠 `Conf` 四个字段切换

【源码确认 `alist_meta.go:29-60`】

| 项 | 夸克 | UC |
|---|---|---|
| 驱动名 | `Quark` | `UC` |
| API 基址 | `https://drive.quark.cn/1/clouddrive`（取直链也可 `drive-pc.quark.cn`） | **`https://pc-api.uc.cn/1/clouddrive`** |
| Referer | `https://pan.quark.cn` | **`https://drive.uc.cn`** |
| `pr` 参数 | `ucpro` | **`UCBrowser`** |
| UA | `quark-cloud-drive/.../Channel/pckk_other_ch` | `uc-cloud-drive/.../Channel/pckk_other_ch` |
| 额外鉴权 | 无 | **必须带 `ut` 查询参数**（见 5.2） |
| 是否强制走代理 | 否 | **`OnlyProxy: true`** ← 注意 |

### 5.2 ★UC 独有：`ut` 设备令牌（这是最大的差异点）

【源码确认 `alist_uc_token.go:13-39`，且已用官方向量验证】

算法：
```
UTDID  = base64(18 个随机字节)              → 24 字符，需持久化，每个 storage 独立
key    = b"2106f55fd41e800c"                ← 固定常量 = md5("C88EAC2B-F07C-4FE2-ABDF-C8F049C46C7B")[:16]
padding = 16 - len(UTDID) % 16  (PKCS7, 补 pad 个值为 pad 的字节)
ct     = AES-128-CBC(key, iv=key, plaintext=UTDID+padding)
ut     = base64( uint16_be(13801) || ct )    ← 13801 = 0x35E9 魔数前缀
```

**我做的独立验证（不只是照抄）**：

```python
import base64, struct, hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
KEY = b"2106f55fd41e800c"
print(hashlib.md5(b"C88EAC2B-F07C-4FE2-ABDF-C8F049C46C7B").hexdigest())
# → 2106f55fd41e800cc5d2a1b1ce7efdcd   ← 前 16 位正好是 KEY，注释属实

def uc_token(u):
    pad = 16 - len(u) % 16
    pt = u.encode() + bytes([pad])*pad
    e = Cipher(algorithms.AES(KEY), modes.CBC(KEY)).encryptor()
    return base64.b64encode(struct.pack(">H", 13801) + e.update(pt) + e.finalize()).decode()

uc_token("0123456789abcdefghijklmn")
# → NelUvDsfgQ5vcbK/BaZ1HoSnF9RePiGAuxFxhYc/dPslAA==
```

对照 AList 官方测试向量 `alist_uc_token_test.go:17`：
```go
const want = "NelUvDsfgQ5vcbK/BaZ1HoSnF9RePiGAuxFxhYc/dPslAA=="
```
**逐字节 MATCH ✅** —— UC 的 `ut` 算法已完全攻克，可直接用（Python 实现已写入 `probe/probe_quark.py:uc_download_token()`）。

### 5.3 UC 使用要点

- `ut` **只加在 `/file/download` 上**，且只在 UC 分支加。【源码确认 `alist_uc_token_test.go:91-93`】明确断言 "UC token leaked to Quark" 是错误
- UTDID 要**持久化 + 每个 storage 独立**（`alist_uc_token_test.go:71-73` 断言不同 storage 不能共用设备 ID）
- `pr` 必须是 `UCBrowser`（`alist_uc_token_test.go:88`）
- UC 驱动 `OnlyProxy: true` → 上游认为 UC 直链**不适合 302 直跳**，建议由服务端中转

### 5.4 UC 侧待补（诚实说明缺口）

- **我没有 UC 会员账号**，§4 的限速曲线全部来自夸克。UC 是否同样存在 18:00–23:00 时段闸门：**【未知】**
- 推论：同属阿里、同一套 `clouddrive` 后端、共用一份驱动代码，**限速策略极可能一致或同源**，但**这仍是推断，不是结论**
- UC 分享链接格式：`https://drive.uc.cn/s/<pwd_id>`（与夸克 `/s/<pwd_id>` 同构）；`sharepage/token`、`sharepage/detail` 路径相同【源码确认：驱动里这些路径是共用的】

**【实测】UC 端点确实活着且同构**：
```
POST https://pc-api.uc.cn/1/clouddrive/share/sharepage/token?pr=UCBrowser&fr=pc
  {"pwd_id":"abcd1234","passcode":""}
→ HTTP 404, {"status":404,"code":41006,"message":"分享不存在"}   ← 与夸克完全相同的错误码！

POST https://pc-api.uc.cn/1/clouddrive/file/download?pr=UCBrowser&fr=pc
  {"fids":["x"]}    （不带 ut）
→ HTTP 401, {"status":401,"code":31001,"message":"require login [share missing]"}  ← 未登录被拦
```

---

## 6. 下一步实验设计（补掉唯一的推断缺口）

**目标**：验证限流期"账号级总量锁死"vs"连接级独立限速"。

**设计**（需要一个夸克会员账号 + 一个 ≥2GB 文件）：

| 组 | 并发 | 单文件连接 | 预期（若账号级） | 预期（若连接级） |
|---|---|---|---|---|
| A | 1 | 1 | ~100KB/s | ~100KB/s |
| B | 1 | 4 | ~400KB/s | ~100KB/s（不变） |
| C | 1 | 16 | ~1.6MB/s | ~100KB/s（不变） |
| D | 4 个文件 | 各 4 | **总量仍 ~2MB/s** | 4×400KB/s=1.6MB/s↑ |

**判据**：C 组若显著 > A 组 → **连接级**（堆并发有效，像微云）；C 组与 A 组持平 → **账号级**（堆并发无效，像百度）。

**时机**：必须在 18:00–23:00（已知限流时段）做，否则会跑在窗口期、测不出限速。

现成工具：`probe/probe_quark.py speed "<直链>"` 已实现 `probe_range()`（单连接测速 + 206 判定）和 `probe_multi()`（N 连接并行 + 聚合速率），直接喂直链即可。

---

## 7. 参考源码位置

全部已落盘到 `research/src-quark/`（`gh-proxy.com` 可通；`raw.githubusercontent.com` 和 `cdn.jsdelivr.net` 在本机被 RST）。另：`web_search` 因 API key 失效不可用，改用 GitHub API + issue 原文取证。

### AList `drivers/quark_uc/`（**最新最全，UC 支持完整**，推荐以此为准）

| 本地文件 | 上游路径 | 价值 |
|---|---|---|
| `alist_driver.go` | `AlistGo/alist@main/drivers/quark_uc/driver.go` | 驱动骨架、Init、Link 入口 |
| `alist_util.go` | `.../util.go` | ★**取直链 `getDownloadLink()` 全文**、`__puus` 刷新、cookie 快照 403 注释 |
| `alist_meta.go` | `.../meta.go` | ★**夸克/UC 四字段差异表**（api/referer/pr/ua） |
| `alist_types.go` | `.../types.go` | `DownResp` / `SortResp` 响应结构、`download_url` 字段 |
| `alist_uc_token.go` | `.../uc_token.go` | ★**UC `ut` 算法全文**（AES 常量 + 0x35E9 前缀） |
| `alist_uc_token_test.go` | `.../uc_token_test.go` | ★**官方测试向量**（我已用它验证 Python 实现） |
| `alist_util_test.go` | `.../util_test.go` | 直链 header 断言、`__puus` 轮换场景 |
| `alist_driver_test.go` | `.../driver_test.go` | — |
| `alist_delete_reliability.go` | `.../delete_reliability.go` | 删除重试（次要） |

### OpenList `drivers/quark_uc/`（**旧版，无 UC `ut`**，可作对比）

`openlist_driver.go` / `openlist_util.go` / `openlist_meta.go` / `openlist_types.go`
> 差异：`getDownloadLink()` 无 `ut`、无 `__puus` 刷新、无 DownConcurrency 配置（`openlist_util.go:113-137`）。**说明 UC 的 `ut` 是较新加入的硬要求，老实现已不可用。**

### OpenList `drivers/quark_open/`（官方 OpenAPI，非分享直链）

`quark_open_{driver,meta,util,types}.go` — 走 `https://open-api-drive.quark.cn`，需 `app_id`/`sign_key`/`refresh_token`。**不适用于分享链接场景**，仅备查。

### markgeen/quark-to-gopeed（**游客分享直链的最佳范例**）

`userscript_quark_to_gopeed.js`（27KB 全量）
> ★ 最有价值：完整免 cookie 的 `token → detail → file/download` 三步 + 23018 游客限制处理 + `LINK_BATCH=15` + stoken 从页面 fiber 树抓取的技巧。

### zmj159809/quark-downloader（Tauri，**转存 + 直链完整链路**）

`qdl_api.rs`（★转存/轮询/取直链）、`qdl_parser.rs`（★分享 URL 解析）、`qdl_cookie.rs`（`__puus` 从 Set-Cookie 更新）、`qdl_token.rs`、`qdl_files.rs`、`qdl_transfer.rs`

### 一手限速证据（JSON 原文）

| 文件 | 内容 |
|---|---|
| `issue_fnos_quark6.json` | ★88VIP 断崖限速完整报告 + 速度曲线 |
| `issue_fnos_quark6_comments.json` | ★**维护者定性：服务端限速、18-23 点、官方客户端同样受限** |
| `issue_alist2097.json` | 会员仍限速 2M |

### 仓库文件清单快照

`_flat.json`（AList main 全文件，662 项）、`_flat_openlist.json`（OpenList，761 项）——用 `data.jsdelivr.com/v1/package/gh/<repo>@main/flat` 拿到的，**这个域名在本机可用**，是 loc 源码树的好办法。

### 可执行探针

`probe/probe_quark.py` —— 已实现：`parse_share_url()` / `uc_download_token()`（含官方向量自检）/ `share_token()` / `share_detail()` / `share_download()` / `own_download()` / `probe_range()` / `probe_multi()`。CLI：`uctoken` / `url` / `speed`。

---

## 8. 给下载器的落地建议（一页速查）

1. **两条路都实现**：游客分享直链（免 cookie，最快上手）+ 登录转存直链（超空间时必需）
2. **取直链必须伪夸克 PC 客户端 UA**，否则 23018
3. **直链下载三 header 锁死**：Cookie / Referer / UA 必须与取链接时一致 → 403 否则
4. **`__puus` 约 3 小时过期**，用"去掉 `__puus` 打 `/config`"刷新；刷新与下载要 cookie 快照隔离
5. **起手 8~16 连接**（窗口期能跑满），**但必须带速率地板检测**：<3MB/s 且持续 30s 且抖动 <0.15 → 判定进限流期，停止加并发
6. **18:00–23:00 预期被掐到 ~2MB/s**，此时多文件轮转优于单文件多分片
7. **UC 必须加 `ut`**（算法见 §5.2，每 storage 独立持久化 UTDID），`pr=UCBrowser`，且考虑服务端中转（`OnlyProxy`）
8. **409/41006/31001/31002/23018/14001** 这几个码写进错误分类（分别=未登录/分享不存在/需登录/分享失效/游客超限/参数错）
