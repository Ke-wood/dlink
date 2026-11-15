# 可拓展性设计（核心需求）

> 用户的明确要求：**工具必须可拓展**。
> 新增一个网盘平台，应该只写一个文件、不改核心代码。

---

## 1. 分层：核心与适配器彻底分离

```
┌─────────────────────────────────────────────────────┐
│  表现层  GUI (下载任务/进度/设置)  +  扩展 (取直链)    │
└────────────────────┬────────────────────────────────┘
                     │ 只依赖下面两个接口
┌────────────────────▼────────────────────────────────┐
│  契约层  (抽象接口 + 数据模型)                        │
│  · PlatformAdapter   —— 网盘适配器接口               │
│  · DirectLink        —— 下载凭证（URL+鉴权+TTL）     │
│  · RemoteFile        —— 远端文件元数据               │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│  核心层  (完全不知道"网盘"是什么)                     │
│  · downloader.py  分段并行/续传/校验                 │
│  · server.py      本地 API                          │
│  · queue.py       任务队列                          │
│  · profile.py     按平台的下载策略                    │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│  适配器层  (每平台一个文件，可插拔)                   │
│  · platforms/baidu.py                               │
│  · platforms/quark.py                               │
│  · platforms/weiyun.py                              │
│  · platforms/direct.py  ← 常规 HTTP 直链（新需求）    │
│  · 未来：platforms/aliyun.py / 123pan.py / ...       │
└─────────────────────────────────────────────────────┘
```

**核心层对平台一无所知**，只知道 `DirectLink`。这是可拓展性的根本。

---

## 2. 平台适配器契约

新增平台只需实现下面这一个类：

```python
class PlatformAdapter(Protocol):
    name: str                              # 唯一标识，如 "baidu"
    display_name: str                      # 展示名，如 "百度网盘"

    @staticmethod
    def match(url: str) -> bool:           # 能否处理这个 URL

    @staticmethod
    def parse_id(url: str) -> str:         # 从 URL 抽 ID

    def resolve(self, url: str, **opts) -> list[ResolvedFile]:
        """分享 URL → 文件列表 + 直链"""

    def default_profile(self) -> DownloadProfile:
        """该平台的推荐下载参数（并发/分段/超时）"""
```

**注册方式**：`platforms/__init__.py` 里自动发现，新增文件即生效：
```python
# 自动 import 本目录下所有 *.py，收集 PlatformAdapter 子类
```

**新增一个平台的工作量**：写 1 个文件（约 200 行）+ 加 1 条 profile 默认值 + 写 README 里的一行说明。**不改任何核心代码。**

---

## 3. 新增需求：常规 HTTP 直链下载

用户要能**粘贴任意 HTTP/HTTPS 链接**下载，并**用并行加速**（这是 IDM 的核心能力）。

### 设计

新增 `platforms/direct.py`，`match()` 对所有 http(s) URL 返回 True（**兜底**）：

```python
class DirectAdapter(PlatformAdapter):
    name = "direct"
    display_name = "普通链接"

    @staticmethod
    def match(url: str) -> bool:
        return url.startswith(("http://", "https://"))

    def resolve(self, url, **opts) -> list[ResolvedFile]:
        # 1. HEAD 请求探测：大小、是否支持 Range、文件名、Content-Type
        # 2. 不支持 Range → 标记 no_range，下载器自动降级单连接
        # 3. 支持 Range → 交给下载器分段
```

**关键探测逻辑**（决定能否并行）：
| 服务端行为 | 判定 | 策略 |
|---|---|---|
| `Accept-Ranges: bytes` + 206 | 支持并行 | 分段 |
| 无 Accept-Ranges 但 206 正常 | 支持并行 | 分段 |
| 返回 200 忽略 Range | **不支持** | 强制单连接 |
| HEAD 被拒（405） | 未知 | 先用 Range GET 试探 |

**这是通用下载器最有价值的部分**：很多 HTTP 服务器不限速，但单连接跑不满带宽，多段并行能显著提速（这点和网盘完全相反）。

### 与网盘的统一

两者都产出 `DirectLink`，下载器一模一样对待。**平台差异只体现在 `DownloadProfile`**。

---

## 4. 分平台配置（DownloadProfile）

用户要求**每种网盘和常规链接的设置分开**：

```
设置 → 下载策略
├─ 普通链接（默认）
│   并发数: 8      分段大小: 32MB
│   [说明] 大多数 HTTP 服务器不限速，多段并行通常能跑满带宽
├─ 百度网盘
│   并发数: 1      分段大小: —（单连接）
│   [说明] 账号级限速约 80KB/s，并发只会触发风控。见 README
├─ 腾讯微云
│   并发数: 8      分段大小: 32MB
│   [说明] 连接级限速（单连接 14MB 后降速），并行实测有效
├─ 夸克网盘
│   并发数: 6      分段大小: 16MB
│   [说明] 二阶段限速，18:00-23:00 可能限流，建议降为 2
└─ UC 网盘
    并发数: 6      分段大小: 16MB
```

### 数据结构

```python
@dataclass
class DownloadProfile:
    workers: int = 8
    seg_size: int = 32 * 1024 * 1024
    max_retries: int = 6
    timeout: float = 180.0
    # 高级
    force_single: bool = False      # 强制单连接
    respect_ranges: bool = True     # 是否尝试分段
    user_note: str = ""             # 展示给用户的说明
```

### 存储

`%LOCALAPPDATA%/dlink/profiles.json`：
```json
{
  "version": 1,
  "profiles": {
    "direct": {"workers": 8, "seg_size": 33554432},
    "baidu":  {"workers": 1, "force_single": true},
    "weiyun": {"workers": 8, "seg_size": 33554432},
    "quark":  {"workers": 6, "seg_size": 16777216}
  }
}
```

**拓展性**：新平台只需在 `default_profile()` 里定默认值，用户可在 GUI 覆盖。

---

## 5. 扩展端同样要可拓展

`extension/injected.js` 里的平台解析也做成**注册表**：

```js
const ADAPTERS = {
  baidu: { resolve: baiduResolve },
  quark: { resolve: quarkResolve },
  weiyun: { resolve: weiyunResolve },
  // 新增平台：加一行
};
window.__DLINK__.resolve = async (platformId, url, opts) => {
  const a = ADAPTERS[platformId];
  if (!a) return { ok: false, code: 'NO_PLATFORM' };
  return a.resolve(url, opts);
};
```

---

## 6. 拓展一个平台的完整清单（Checklist）

放 README 里，让别人能贡献：

- [ ] `src/dlink/platforms/<name>.py` —— 实现 PlatformAdapter
- [ ] 在 `default_profile()` 里定推荐并发数
- [ ] `extension/injected.js` —— 加一个 resolve 函数 + 注册表一行
- [ ] `extension/manifest.json` —— 加 `host_permissions` 和 content_scripts matches
- [ ] `extension/platforms.js` —— 加平台识别规则
- [ ] `docs/PLATFORMS.md` —— 补一节实测数据
- [ ] `README.md` —— 平台表格加一行
- [ ] `tests/test_platform_<name>.py` —— 至少测 URL 解析和信封构造

---

## 7. 不为了拓展性过度设计

明确**不做**的：
- ❌ 插件热加载（Python 重启够用）
- ❌ 平台能力协商/微内核（过度工程）
- ❌ 抽象的抽象（三层以上的继承）

**只用「协议 + 注册表」这一层抽象**，够用且清晰。
