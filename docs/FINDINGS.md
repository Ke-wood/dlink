# 实测发现（2026-11-15 复现，本机）

> 本文只记**当场跑过**的事实，用来校正交接文档 §3 里未经验证/有误的条目。
> 标记：✅ 本次实测通过 ｜ ⚠️ 与交接文档冲突 ｜ ❓ 未验证

## 1. 传输层

| 事实 | 结论 |
| --- | --- |
| 宿主 `curl` (Schannel 8.22.0) | ✅ 复现 `SEC_E_NO_CREDENTIALS`，https 全废。**不得把 curl 作为默认引擎** |
| Python `urllib` / `requests` 2.34 / `httpx` / `aiohttp` | ✅ 全部正常 TLS，`https://share.weiyun.com/` 200 |

→ 工具以 **Python** 为唯一宿主，绕开 curl。交接文档 §4.1「用 http:// 直链绕过」不再需要，用 https 即可。

## 2. 鉴权规律（本次最大收获）

微云 API 是 `POST /webapp/json/{protocol}/{name}?refer=chrome_windows&g_tk={g_tk}&r={rand}`，三重门：

| 失败形态 | 真实原因 | 验证 |
| --- | --- | --- |
| **HTTP 403，空 body** | **缺 `Accept` 头**（不是登录问题） | ✅ 同一 body，加 `Accept: application/json, text/plain, */*` 后 403 → 200 |
| **HTTP 200 + `retcode:190011`（未登录）** | `g_tk` 与当前 cookie 的 `skey` 不匹配 | ✅ 用 A 页的 g_tk 打 B 域必 403/190011，用同域 g_tk 则通 |
| `retcode:24203`（服务器繁忙） | 信封字段不合该接口 schema，服务端未进入业务逻辑 | ✅ 手写 DiskDirBatchList 复现 |

**`g_tk` 是按域绑定的**：`share.weiyun.com` 的 g_tk 与 `www.weiyun.com` 的 g_tk 不同（实测 1437925728 vs 1440339844），必须用**目标域**页面自己发请求里的那个值。

交接文档 §3.3「g_tk 直接从页面自身请求 URL 参数里抄」——✅ 正确，但要强调**抄同域页面**的。

## 3. 免登录路径（⚠️ 与交接文档 §3.2 结论冲突）

交接文档称：`weiyunShareNoLogin/WeiyunShareBatchDownload` 路径**不存在（404）**。

实测：

| 接口 | 结果 |
| --- | --- |
| `weiyunShareNoLogin/WeiyunShareDirList` (cmd 12031) | ✅ **免登录可用**，`retcode:0`，返回完整 `file_id` / `filename` / `file_size` / `file_sha` |
| `weiyunShareNoLogin/WeiyunShareBatchDownload` (cmd 12024) | ✅ 路径**存在**（不再 404），但本会话用无 skey 态未拿到 0；页面首屏曾发出同名请求 → 该端点确实被页面使用 |
| `weiyunShare/WeiyunShareBatchDownload` | ✅ 路径存在；**需登录态**，未登录得 190011 |

→ 修正：**404 的结论已过期**。`NoLogin` 命名空间对 DirList 确实可用；BatchDownload 的免登录形态需要登录态补齐，工具里**两条路都要实现**，运行时自动降级：
1. 有登录态 → `weiyunShare/*`（权威）
2. 无登录态 → 先试 `weiyunShareNoLogin/WeiyunShareBatchDownload`，失败再提示导入登录态

## 4. 分享元数据（免登录即可）✅

- 分享页：`https://share.weiyun.com/{share_key}`，Vue2 SPA。
- store：`document.getElementById('app').__vue__.$store.state`，模块 `userInfo/shareInfo/sharedir/sharefile`。
- `state.shareInfo` 直接给出：`share_key`、`pdir_key`、`share_name`、`share_uin`、`need_pwd`、`share_type`、`total_dir_count`、`total_file_count`、`dir_list[]`（每项含 `dir_key` / `dir_name` / `pdir_key` / `dir_file_size`）、`share_uin_security`。
- 文件树：`state.sharefile.shareFile.shareFile.pathStack[末位]` 是 `FileNode`，子项在 `_kid_files` / `_kid_dirs`；`FileNode` 含 `_id` / `_pdir_key` / `_name` / `_file_size` / `_file_sha` / `_ext`。
- ✅ 实例自检：根节点 `_kid_dirs` 拿到 1 个目录（`abbab47b2335ea4d271c5ac5aed36efc`，1,638,865,525 字节），与交接文档一致。
- ⚠️ 交接文档说「`_kid_files[]._parent` 循环引用」——本次根节点无 `_parent` 自有属性（`_ppdir_key` 存在）。仍按**手动遍历**实现，不依赖 `JSON.stringify`。

## 5. 请求形态（照抄即可，勿手搓）

抓到的真实 XHR（✅ 200）：

```
POST /webapp/json/weiyunShareNoLogin/WeiyunShareDirList?refer=chrome_windows&g_tk={同域g_tk}&r={rand}
Accept: application/json, text/plain, */*
Content-Type: application/json;charset=utf-8
{"req_header":"<string>","req_body":"<string>"}   ← 双重 JSON 编码（交接文档 §3.3 正确）
```

`req_header.cmd` 必须与接口对应（12031=DirList，12024=BatchDownload），`req_body` 内键名 `.weiyun.{接口名}MsgReq_body`。

## 6. 本会话登录态现状 ❓

当前浏览器 profile 访问 `www.weiyun.com/disk` 时界面显示「已用 0 B/10 GB」「暂无文件」，但**没有 `skey` cookie**，只有 `weiyun_wx_*` / `TOK` / `wyctoken` / `uin` 等。

→ 说明：该 profile 处于**半登录/访客**态。真正取直链（`weiyunShare/*`）需要用户在 GUI 里登录一次；工具必须把「登录态探测 + 明确提示」做成一等公民，而不是静默失败。

## 7. 下载行为（沿用交接文档 §3.5，本次未重跑）

- 无 `FTN5K` cookie → 401；单连接 ~14MB 后衰减至 ~240B/s。
- 新连接冷启动 ~9.7MB/s → 限的是连接年龄，用 32MB × 8 路分段并行。
- 终验：`file_sha`(SHA1) + `file_size` + 魔数三位一体。
