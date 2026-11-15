# 实测报告：百度网盘直链与限速（2026-11-15）

> 全部数据为本机当场实测，非传闻。测试对象：`https://pan.baidu.com/s/<示例分享ID>（提取码略）`
> 文件：`<目录>/<文件>`，**29,620,737 字节**
> 账号：普通用户（非会员）

---

## 1. 直链获取链路（已验证可用）

### 1.1 完整步骤

```
分享 URL + 提取码
  ↓ ① 提取码校验（页面自动，或 POST /share/init）
  ↓ ② 读页面 window.yunData → shareid / share_uk / file_list[].fs_id
  ↓ ③ /share/list  列目录（★未登录即可，errno:0）
  ↓ ④ /share/tplconfig?fields=sign,timestamp  取签名（★关键，文档未记载）
  ↓ ⑤ /share/transfer  转存到自己网盘（需登录）
  ↓ ⑥ /api/filemetas?dlink=1&fsids=[to_fs_id]  取直链（需登录）
```

### 1.2 逐条实测

| 步骤 | 接口 | 未登录 | 已登录 |
|---|---|---|---|
| 列目录 | `GET /share/list?uk=&shareid=&dir=` | ✅ `errno:0` | ✅ |
| 取签名 | `GET /share/tplconfig?surl=&fields=sign,timestamp,jsToken` | ✅ `errno:0` | ✅ |
| 分享直下 | `POST /api/sharedownload?sign=&timestamp=` | ❌ `errno:118` 违规操作 | ❌ `errno:118` |
| 转存 | `POST /share/transfer?shareid=&from=` | — | ✅ `errno:0` |
| 取直链 | `GET /api/filemetas?dlink=1&fsids=[]` | ❌ | ✅ `errno:0` |

**关键发现**：
- `/api/sharedownload`（分享直下）**即使登录也返回 118**，网页端"普通下载"走的是调起客户端，不返回直链。
- **可行路径只有：转存 → 取自己网盘的直链。**
- `sign` 每次请求都变（`L5Bxwbum…`、`qOiYLL9P…`、`pw+Jq+xp…`），且**与具体请求绑定**，不能跨请求复用。

### 1.3 鉴权 cookie 必需项

| cookie | 域 | 必需 | 说明 |
|---|---|---|---|
| `BDUSS` | `.baidu.com` | ✅ | 身份主凭证 |
| `BDUSS_BFESS` | `.baidu.com` | 建议 | BFESS 变体 |
| **`STOKEN`** | `.passport.baidu.com` | ✅ **必需** | 缺它网页接口一律 `errno:-6` |
| `BAIDUID`/`BIDUPSID` | `.baidu.com` | 建议 | 设备标识 |
| `bdstoken` | —（非 cookie） | ✅ | 从 `/api/gettemplatevariable` 或页面 HTML 取 |

**验证**：只给 `BDUSS` → 网页接口 `errno:-6`；补上 `STOKEN` → `errno:0`，拿到 `bdstoken`。

### 1.4 直链形态

```
https://d.pcs.baidu.com/file/<md5>?fid=<uid>-250528-<fs_id>&rt=pr
  &sign=FDtAER-DCb740ccc5511e5e8fedcff06b081203-<签名>&expires=8h
  &chkbd=0&chkv=0&dp-logid=...&dp-callid=0&dstime=...&r=...&clienttype=0
```

- **时效 `expires=8h`**
- 必须带 `BDUSS` 系 cookie，否则 `403 + {"error_code":31045,"error_msg":"user not exists"}`
- **支持 Range（206）**，`content-range: bytes 0-N/29620737`
- `content-type: application/octet-stream`

---

## 2. 限速实测（核心结论）

### 2.1 并发对照

| 并发 | 聚合速度 | 单路速度 | 成功率 | 备注 |
|---|---|---|---|---|
| **1 路** | **0.076 MB/s** | 0.076 | 100% | 基线，约 **74 KB/s** |
| 4 路 | 0.150 MB/s | 0.04/0.033/0.075/0.022 | 100% | 略增但不稳定 |
| 8 路 | 0.020 MB/s | 多为 0 | 低 | **大幅劣化** |
| 16 路 | 0.113 MB/s | 0.014~0.07，12 路为 0 | **6/16** | **3 路直接 403** |

### 2.2 换新直链能否重置限速

每轮重新调 `/api/filemetas` 拿全新签名：

| 轮次 | 速度 |
|---|---|
| 第 1 次 | 0.074 MB/s |
| 第 2 次 | 0.043 MB/s |
| 第 3 次 | 0.043 MB/s |

→ **不能重置，且越测越慢（账号级累积风控）。**

### 2.3 结论

> **百度是「账号级总带宽封顶」，约 100 KB/s 量级。分段并行无效，且并发越高越差（触发风控返回 403）。**

对比微云（交接文档 §3.5 实测）：

| | 微云 | 百度 |
|---|---|---|
| 限速类型 | **连接级**（单连接先快后死） | **账号级**（总带宽封顶） |
| 分段并行 | ✅ 有效（8 路聚合 ~9.7MB/s） | ❌ 无效，反致 403 |
| 换新直链 | — | ❌ 不重置 |
| 结论 | 值得做并行下载器 | **只能靠会员/其他手段** |

**这是两个平台的根本差异，直接决定工具设计：并行下载器对微云有效，对百度无效。**

---

## 3. 不可行的路径（避免重复踩）

| 路径 | 结果 |
|---|---|
| Edge cookie 库直读 | ❌ `v20` = App-Bound Encryption，`InvalidTag`，外部进程无法解密 |
| Edge 运行中复制 cookie 文件 | ❌ 独占锁（`WinError 32`、`PermissionError`） |
| DSH 浏览器 cookie 库复制 | ❌ 同样被锁 |
| `xpan` 官方 API（`/rest/2.0/xpan/multimedia`） | ❌ `errno:9019 need verify`，需开发者 app 授权 |
| 页面内 fetch 直链 | ❌ CORS（`TypeError: Failed to fetch`） |
| `dsh_browser_download` | ❌ 同为页内 fetch，CORS 失败 |
| iframe 导航直链 | ❌ 未落盘（跨域响应被浏览器丢弃） |

**唯一可靠办法**：从浏览器 **F12 → Application → Cookies** 手工取 `BDUSS` + `STOKEN`，其余用 Python。

---

## 4. 对工具设计的含义

1. **平台适配层必须区分「限速类型」**，不能假定所有网盘都适合分段并行。
2. 百度平台的 resolver 必须实现「转存 → 取直链」两步，且必须先校验 `STOKEN` 存在，缺失时立刻报错而不是静默 `-6`。
3. 下载器应支持**降级策略**：检测到并发导致 403/速度劣化时，自动退回单连接。
4. `expires=8h` 需要在 `DirectLink.expires_at` 里体现，取链后立即下载。

---

## 5. 待办

- [ ] 微云同口径复测（本机出口 IP 被微云网关拦 403，需换网络环境）
- [ ] 蓝奏云 / 123 盘 / 天翼 同口径测试
- [ ] 验证「百度会员账号」是否解除账号级限速（需会员账号）
