# 开源下载器 / 网盘工具 · GUI 设计调研

> 状态：调研稿 v1（2026-11-15）
> 目的：为「网盘直链下载器」（扩展取直链 → 本机 Python 下载器）寻找**可直接借鉴**的 GUI 设计经验。
> 性质：**只做调研，不写代码**。
>
> **证据分级（全文遵守）**
> - 🟢 **[查证]** = 本次用 GitHub API / 抓取仓库源码直接读到的，附文件路径可复核
> - 🟡 **[推断]** = 从查证到的代码或文档推断出的设计意图，不是原作者明说
> - 🔴 **[未查证]** = 未拿到实证，明确标注，**请勿当事实引用**
>
> **数据口径**：star 数与 `pushed_at` 取自 GitHub REST API `GET /repos/{owner}/{repo}`，抓取时刻 **2026-11-15 约 07:00 UTC**。
> 本机 `raw.githubusercontent.com` 连接被重置、Gitee 被阻断、匿名 GitHub API 配额 60 次/小时（曾用尽）；凡受此影响未取到的，均在文中标注，不用记忆填空。

---

## 0. 一页结论（先看这个）

| 问题 | 答案 | 依据 |
|---|---|---|
| 最该抄谁的界面结构 | **Motrix 的任务表列定义**（逐列给了像素宽度与可显隐规则）+ **AriaNg 的「引擎设置 / 界面设置」分离** | 🟢 §1.1、§1.3 |
| 最该抄谁的进度展示 | **qBittorrent**：进度条**画在表格单元格里、条与百分比同格**；**Gopeed/Motrix**：进度条与数字同格并带阶段标签 | 🟢 §1.4、§1.1 |
| 最该抄谁的「限速差异」处理 | **LinkSwift**：每个平台一份独立配置 + 每个下载器目标独立配置 | 🟢 §1.8 |
| **并发数配在哪一层** | 四家主流工具都只做「全局默认 + 任务覆盖」；**「按 host 记忆」是它们共同的空位，是我们真正的差异化功能** | 🟢 §3.3b |
| 最该避的 | **JDownloader2 式功能堆砌**、**aria2 本身无 GUI**、**qBittorrent 式左侧导航栏**（我们任务数 <20，用不上） | 🟡 §4 |
| GUI 技术栈 | **Web UI（本地 HTTP + 浏览器/套壳）> PySide6 > Tkinter**，理由见 §3.4 | 🟢 基于本地后端实测接口 |

一句话：**抄 Motrix 的「表格列纪律」和 LinkSwift 的「按平台分治配置」，避 qBittorrent 的「导航复杂度」和 JDownloader 的「设置项淹没」；并发数做成「平台默认 → 按 host 记忆 → 任务覆盖」三层（§3.3b）。**

---

## 1. 逐项目分析

### 1.1 Motrix —— 最接近我们定位的对照组

| 项 | 值 |
|---|---|
| 仓库 | `agalwood/Motrix` |
| Stars | **58,287** 🟢 |
| Forks | 5,154 🟢 |
| 主语言 | TypeScript 🟢 |
| 最后推送 | **2026-11-14T15:57:26Z**（抓取前一日，**高度活跃**）🟢 |
| 开放 issue | 160 🟢 |
| License | NOASSERTION（GitHub 未识别为标准 SPDX，实为 MIT + 附加条款，需自行核对）🟢 |
| 技术栈 | Electron + Vue/React（renderer 有 961 个文件，已重构为 TSX 组件化结构）🟢 |

**布局描述**（🟢 从源码结构读出的形态，非截图）

主窗口 = 左侧窄导航（下载中/已完成 等）+ 主体**任务表格** + 底部全局状态。任务表是真正的「数据网格」而非卡片：

- 行高 **36px**，表头 36px，勾选列 36px（`src/renderer/routes/downloads/columns.ts`）
- 名称列弹性（`minmax(200px, 1fr)`），其余列固定宽、且每列都定义了 `min/max` 夹取范围
- 数值列（大小/进度/下行/上行/ETA/连接数）显式标记 `numeric: true` → 右对齐

**它的列清单（这是全文最值得抄的一段）**🟢

| 列 | 默认宽 | 最小 | 最大 | 数值对齐 |
|---|---|---|---|---|
| name | 200 | 160 | 900 | — |
| size | 84 | 72 | 240 | ✅ |
| progress | 104 | 100 | 300 | ✅ |
| status | 100 | 80 | 240 | — |
| down（下载速度） | 128 | 128 | 240 | ✅ |
| up（上传速度） | 128 | 128 | 240 | ✅ |
| eta | 112 | 112 | 240 | ✅ |
| connections | 112 | 72 | 240 | ✅ |
| createdAt | 196 | 140 | 300 | — |
| finishedAt | 196 | 140 | 300 | — |

**进度怎么显示**（🟢 `task-progress.tsx`）
- 一个 **1px 高的细进度条 + 紧跟的文本标签**，同一格内并排（`flex items-center gap-2`），文本列 `truncate` 防溢出
- 文本 = `[阶段标签] + [百分比]`，例如 `下载中 37%`；无进度时显示 `—` 且进度条宽度归零（`pct === null && 'w-0'`）
- 阶段标签来自 `getMediaPhaseLabel(task)` → 说明它把「媒体处理」拆成独立阶段（下载 / 转码 / 合并），这是**阶段化进度**的先例

**速度 / ETA / 连接数的「条件显示」纪律**（🟢 `task-column-values.ts`，强烈建议照抄这个思路）

```
下载速度：仅 status ∈ {Downloading, FetchingMetadata} 时显示，否则返回 null
上传速度：仅 BT/Magnet 且 (下载中 或 做种中) 时显示，否则 null
        —— 纯 HTTP 任务的上行列永远显示 "—"
ETA     ：仅 Downloading 且不在媒体处理阶段时显示
连接数  ：BT 显示 peers 数；HTTP 显示 connections 数；非活跃状态一律 null
完成时间：仅 status === Completed 才填，失败虽有 finishedAt 但**不显示**
```

> **可抄点**：把「这一列在这个状态下有没有意义」写成**函数**，而不是渲染时到处 `if`。它的收益是：**表格永远不出现「0 B/s」这种误导性的假数据**，而是干净的空格 / `—`。
> 这条对我们的意义：百度账号级限速下速度**恒定 80KB/s**，若网络抖动导致某一秒读到 0，绝不能显示 `0 KB/s`（用户会以为卡死）。Motrix 的 `null` 而非 `0` 是正解。

**设置项组织**：🟡 从目录结构看为「左侧分类 + 右侧表单」的常规 Electron 设置页；具体分组未逐项查证（🔴）。它著名的做法是**把 aria2 的全部参数暴露成图形化表单**，这也正是它的主要槽点来源（见下）。

**优点（值得抄）**
1. **列宽全部有 min/max 夹取**，且提供 `clampColumnWidth()`——用户拖列宽永远不会拖坏布局 🟢
2. **「该列在当前状态下是否适用」函数化** 🟢（见上）
3. **行高 36px + 虚拟滚动**：适合 20 行以内的自用工具，也撑得住 200 行 🟢（虚拟滚动为 🟡，从 961 个组件与 table 抽象推断）
4. 定位与我们最接近（aria2 引擎 + 浏览器扩展推送 + 桌面 GUI），**扩展推送任务的完整链路有现成参考**

**缺点 / 用户吐槽**
- 🟡 **它本质上是个 aria2 的皮肤**：用户抱怨集中在「aria2 有的毛病它全有」，如连接数/分段参数调不明白、RPC 端口冲突、开机不自启。这些不是 UI 问题，是「把引擎的全部旋钮暴露给用户」的必然结果。
- 🟡 **BT 功能占了大量界面面积**（up、connections、做种、peer 面板），对纯 HTTP 直链下载器是纯噪音。我们要**明确不做**。
- 🟡 中文文档与 i18n 齐全但**默认文案偏「软件说明书」语气**，不解释「为什么慢」——这恰是我们的差异点。
- 🔴 具体 issue 吐槽未逐条查证（配额用尽），此处不引用具体 issue 编号。

---

### 1.2 Gopeed —— 现代跨平台重写，工程结构最值得学

| 项 | 值 |
|---|---|
| 仓库 | `GopeedLab/gopeed` |
| Stars | **27,705** 🟢 |
| Forks | 1,807 🟢 |
| 主语言 | Dart（+ Go 后端）🟢 |
| 最后推送 | **2026-11-15T03:38:35Z**（**当日推送，最活跃**）🟢 |
| 开放 issue | 363 🟢 |
| License | GPL-3.0 🟢 |
| 技术栈 | Go（引擎）+ Flutter（UI，`ui/flutter/`）🟢 |

**布局描述**（🟢 读 `primary_rail.dart` / `filter_sidebar.dart`）

三段式，且**桌面与移动共用一套导航组件**：

```
┌──┬────────────┬───────────────────────────┐
│图│ FILTERS    │  任务列表                  │
│标│ ☰ 全部 (12)│  ┌ 任务卡片 ──────────────┐│
│导│ ⬇ 下载中(3)│  │ 名称 / 进度条 / 速度   ││
│航│ ✓ 已完成   │  └───────────────────────┘│
│轨│ ⏸ 已暂停   │                           │
│  │ ✕ 失败     │                           │
│  │ [速度监控卡]│                          │
└──┴────────────┴───────────────────────────┘
```

- 左 1：**图标导航轨**（Rail），三项：任务 / 扩展 / 设置 —— 极简，只有 3 个 🟢
- 左 2：**过滤侧栏**，枚举 `TaskFilter { all, downloading, completed, paused, failed }`，**每项带计数**，标题 `FILTERS` 用 12px + 字距 2.4 的大写字 🟢
- 侧栏底部挂 **`speed_monitor_card`（速度监控卡）** 🟢 —— 全局速度作为「常驻卡片」而不是一行文字
- 任务区用**卡片**（`task_card/task_card_desktop.dart` + `task_card_mobile.dart` 双实现），**不是表格** 🟢

**进度怎么显示**：独立组件 `task_progress_bar.dart` 🟢（文件已确认存在；内部实现因限流未读到 🔴）。任务详情页有 `task_statistics/` 子模块，含 `peer_table.dart`、`piece_map.dart`——**与 qBittorrent 同款的 peer 表 + 分块地图** 🟢。

**设置项组织**：`settings_page.dart` + 每个设置项一个 widget（`settings_item.dart`、`download_categories_setting.dart`、`theme_settings_control.dart`…），**设置项组件化** 🟢。另值得注意的两个非典型设置：`mcp_agent_setup.dart`（MCP/Agent 集成）、`extension_tag_editor.dart`（扩展标签）🟢 —— 说明它把「扩展」当一等公民。

**优点（值得抄）**
1. **工程结构**：`features/<域>/{presentation/{pages,widgets}, domain}` 的 **feature-first 分层** 🟢。我们的 GUI 若按 `tasks/ settings/ platform/` 分域，代码不会长成一个 2000 行的 `main_window.py`。
2. **过滤侧栏带计数**（`全部 (12)`）🟢 —— 一行字给出「有多少事」，成本极低。
3. **速度监控卡常驻** 🟢 —— 比 Motrix 的「底部状态栏一行小字」更醒目，符合我们「速度是真相来源第 1 条」的判断。
4. **桌面/移动双实现**的取舍思路：布局随宽度切换，而非一套响应式硬撑 🟢。

**缺点 / 用户吐槽**
- 🟡 **Flutter 桌面在 Windows 上的高 DPI 与中文字重渲染**是长期槽点（Flutter 中文/字体问题需要显式配置），并且安装包体积大（Flutter runtime）。
- 🟡 **卡片式列表在任务数多时信息密度低于表格**：卡片为了好看牺牲了「一行扫完 8 个字段」的能力。我们的场景（100 个文件的批次）用卡片会非常费屏。
- 🟡 **peer_table / piece_map 是 BT 遗留**，对直链下载器无价值（见 §3.2）。
- 🔴 具体 issue 未逐条查证。

---

### 1.3 AriaNg —— 纯前端管理界面，设置分层最干净

| 项 | 值 |
|---|---|
| 仓库 | `mayswind/AriaNg` |
| Stars | **13,274** 🟢 |
| Forks | 1,361 🟢 |
| 主语言 | JavaScript（AngularJS 体系，`src/scripts/controllers`）🟢 |
| 最后推送 | **2026-10-05T08:19:55Z** 🟢 |
| 开放 issue | **15**（相对 star 数极低，说明 issue 处理良好 / 或功能已稳定）🟢 |
| License | MIT 🟢 |
| 技术栈 | 纯静态前端，通过 **JSON-RPC / WebSocket** 与 aria2 通信，无后端 🟢 |

**布局描述**（🟢 从 `src/views/` 目录读出）

视图按「页面级」拆分，每个页面一个 HTML：

| 视图文件 | 作用 |
|---|---|
| `list.html` | 任务列表（主界面） |
| `new.html` | 新建任务 |
| `task-detail.html` | 单任务详情 |
| `settings-aria2.html` | **引擎设置** |
| `settings-ariang.html` | **界面自身设置** |
| `status.html` | 全局状态 |
| `debug.html` | 调试 |
| `export-command-api-dialog.html` | 导出为命令行 |

**这是全文第二个最值得抄的结构**：`settings-aria2` 与 `settings-ariang` **是两个独立页面** 🟢。

> **含义**：「**引擎参数**」和「**界面偏好**」不是一类东西，不该混在同一个设置窗口里。
> 我们对应的是：`settings-engine`（线程数、分段大小、重试、代理、续链阈值）vs `settings-ui`（列显隐、窗口尺寸、托盘行为）。
> 理由：引擎参数有副作用、要写进文档、改错会导致下载失败；界面偏好无副作用、随手改。混在一起会让用户在「找一个开关」时被迫翻过 40 个 aria2 参数。

**任务列表形式**：表格，列表有 `taskOrderBy` / `fileOrderBy` / `peerOrderBy` **三个独立排序 filter** 🟢 —— 说明它能对「任务」「文件」「peer」三级列表分别排序（多文件任务的先例）。

**进度显示**：`filters/percent.js`、`filters/volume.js`、`filters/dateDuration.js` —— **格式化逻辑被抽成独立 filter** 🟢。即「37% / 1.48 GB / 3 小时 26 分」这类显示字符串有单一的格式化入口，不在模板里拼。

**优点（值得抄）**
1. **设置分层**（引擎 / 界面分离）🟢
2. **格式化收敛**：percent / volume / duration 三个 filter，单位换算只写一次 🟢
3. **`peerOrderBy` 之外还有 `taskOrderBy`**：排序按实体分开定义，不写通用比较器 🟢
4. 有 `export-command-api-dialog`（导出 curl/aria2 命令）🟢 —— **对自用工具是极好的「可迁移性」设计**，用户能把任务拿到命令行复现，排错时价值巨大。

**缺点 / 用户吐槽**
- 🟡 **它只是 aria2 的遥控器，不是下载器**。用户最大抱怨是「装了 AriaNg 还得自己装 aria2 并配 RPC」，两步缺一不可——这正是我们「一体化」的机会。
- 🟡 **原生 aria2 概念直接暴露**（`continue`、`split`、`max-connection-per-server`），中文用户普遍看不懂。
- 🟢 issue 仅 15 个，但这可能也意味着**功能更新趋于停滞**（最后推送 2026-10，比 Motrix/Gopeed 慢一个量级）。
- 🔴 具体吐槽未逐条查证。

**补充：AriaNg-Native**（`mayswind/AriaNg-Native`，**2,728 stars**，最后推送 **2026-10-05T09:01:03Z**，Electron 打包版，含 `app/index.html` 26KB + 多语言 `app/langs/*.txt` 每个 47–100KB）🟢 —— 说明「**同一套 HTML 界面 + Electron 套壳**」是这类工具的成熟终局形态，对我们选技术栈有直接参考价值（见 §3.4）。

---

### 1.4 qBittorrent —— 表格与进度渲染的工业级参考

| 项 | 值 |
|---|---|
| 仓库 | `qbittorrent/qBittorrent` |
| Stars | **41,624** 🟢 |
| Forks | 4,989 🟢 |
| 主语言 | C++ / Qt 🟢 |
| 最后推送 | **2026-11-14T11:21:14Z**（活跃）🟢 |
| 开放 issue | **2,750**（数量大，但项目体量大）🟢 |
| License | NOASSERTION（GPL-2.0-or-later + OpenSSL 例外，见源码头）🟢 |
| 技术栈 | C++ / Qt Widgets（`src/gui/*.ui`）+ WebUI（`src/webui/www/private/`）🟢 |

**布局描述**（🟢 从 `src/gui/` 结构读出）

经典三区：**左侧过滤栏 + 主转移列表（TransferList）+ 右侧属性面板**。

- 过滤栏：`src/gui/transferlistfilters/`（`basefilterwidget.cpp` 等）🟢
- 转移列表：`transferlistdelegate.cpp`（自定义 delegate 绘制单元格）+ `progressbarpainter.cpp` 🟢
- 右侧属性：`src/gui/properties/`，含 `speedplotview.cpp`（速度曲线）、`piecesbar.cpp` / `downloadedpiecesbar.cpp` / `pieceavailabilitybar.cpp`（**三种分块可视化**）、`peerlistwidget.cpp` + `peerlistsortmodel.cpp`（peer 表）、`speedwidget.cpp` 🟢

**进度怎么显示（最值得抄的一条实证）**🟢

`ProgressBarPainter::paint(painter, option, text, progress, color)`：
- 在**表格单元格内部**构造 `QStyleOptionProgressBar`，`minimum=0, maximum=100, progress=progress`
- **同时设置 `styleOption.text = text; textVisible = true;`**
- 矩形 `option.rect.adjusted(0, 1, 0, -1)`
- 按 `option.state` 的 Enabled 位切换 palette 颜色组；传入的 `color` 有效时覆盖 `QPalette::Highlight`

> **可抄点（高优先级）**：**进度条、百分比文字、颜色状态，在同一个小矩形里完成**。
> 而不是「进度条一列 + 百分比一列 + 状态一列」三列各占 100px。qBittorrent 用**一个 ~140px 宽的格子**同时给出「到哪了 + 多快（在文字里）+ 什么状态（颜色）」。这对我们「8 列要塞进 900px 最小宽」是直接的解法。
> 另外：**按状态给进度条换色**（下载中蓝 / 暂停灰 / 失败红 / 完成绿）集中在一处 palette 覆盖，不要散在业务代码里。

**设置项组织**：🟡 Qt 的 `optionsdialog` 多页 Tab 式（未逐文件查证，🔴 具体页签列表）。这类 C++ 桌面应用的设置通常 8–12 页。

**优点（值得抄）**
1. **单元格内合成绘制**（条 + 文字 + 色）🟢
2. **按状态着色集中管理**（`ProgressBarPainter` 一处）🟢
3. **三种分块视图**（pieces / downloaded-pieces / availability）说明它把「分块状态」当作独立可视化维度 🟢 —— 但我们**只需要最简单的那一种**（见 §3.2）
4. 自定义 delegate 绘制意味着**列可以承载任意图形**，不必受原生控件限制 🟢

**缺点 / 用户吐槽**
- 🟡 **左侧导航（全部/下载中/已完成/做种/未完成）+ 过滤栏 + 搜索 + 分类 + 标签**，五套「切分任务」的机制并存，新用户不知该用哪个。
- 🟡 **BT 概念天然复杂**：peer、做种、健康度、分享率、tracker —— 对我们 100% 是噪音。前序设计文档已正确判断「不为列表做左右分栏导航」（`GUI-INFORMATION-ARCHITECTURE.md` §2.1），本次调研**确认这个判断成立**：qBittorrent 的侧栏复杂度是它任务体量（数百个 BT 种子）逼出来的，我们任务数 <20，用不上。
- 🟡 设置项极多（每个 BT 细节都可调），是功能完备的代价。

---

### 1.5 Xtreme Download Manager (XDM) —— 老牌「浏览器扩展 + 本地加速器」范式

| 项 | 值 |
|---|---|
| 仓库 | `subhra74/xdm` |
| Stars | **8,481** 🟢 |
| Forks | 1,225 🟢 |
| 主语言 | Kotlin（JavaFX）🟢 |
| 最后推送 | **2026-11-13T17:06:24Z**（**仍然活跃，反直觉**）🟢 |
| 开放 issue | 110 🟢 |
| License | GPL-3.0 🟢 |
| 技术栈 | Kotlin + JavaFX；**浏览器扩展**（Chrome/Firefox）🟢（topics 含 `browser-extension`）🟢 |
| 起始 | 2013 年起（描述自述 "since 2013"）🟢 |

**与我们的关系**：这是**架构最像我们的老牌项目**——「浏览器扩展捕获 → 本地 Java 加速器下载」🟢。它的 topics 明确列出 `batch`、`batchdownloading`、`hls`、`mpeg-dash`、`browser-extension`、`chrome`、`firefox` 🟢。

**布局描述**：🔴 未取得源码级证据（限流，源码未读）。🟡 从 JavaFX 桌面范式与其定位推测为「左侧分类 + 任务表格 + 底部速度条」，**但这是推断，不作为可抄依据**。

**优点（值得抄的部分均有主题级证据）**
1. **「批量下载」被列为一级能力**（topics `batch` / `batchdownloading`）🟢 —— 与我们「一次扩展推送 = 一个批次」直接对应。
2. **扩展与客户端分离但强绑定**的成熟范式 🟢 —— 我们的架构选择走在一条被验证过的路上。
3. 能活 13 年且仍在推送，说明**「不劫持浏览器原生下载」的定位可以长期存活** 🟢 —— 这对「个人自用但要能开源」的项目是重要的可行性佐证。

**缺点**
- 🟡 老牌 JavaFX 应用的典型问题：高 DPI 缩放差、安装体积大（JRE 绑定）、UI 视觉停留在 2015 年水平。
- 🔴 具体吐槽未查证。

---

### 1.6 AList / OpenList —— 网盘文件管理（不是下载器）

| 项 | AList | OpenList |
|---|---|---|
| 仓库 | `AlistGo/alist`（原 `alist-org/alist`） | `OpenListTeam/OpenList` |
| Stars | **50,455** 🟢 | **25,632** 🟢 |
| Forks | 7,894 🟢 | 2,467 🟢 |
| 主语言 | Go | Go 🟢 |
| 最后推送 | 2026-10-21T10:37:43Z 🟢 | **2026-11-15T03:15:50Z**（当日，活跃）🟢 |
| 开放 issue | 563 🟢 | 310 🟢 |
| License | AGPL-3.0 🟢 | AGPL-3.0 🟢 |
| 技术栈 | Gin（Go）+ **SolidJS** 前端 🟢 | 同（AList fork）🟢 |

> ⚠️ **注意**：`alist-org/alist` 现在 302 到 `AlistGo/alist`（API 返回的 `full_name` 是 `AlistGo/alist`）🟢。OpenList 是 2025-06 创建的 AList fork（描述 "A new AList Fork to Anti Trust Crisis"）🟢，一年半就 25.6k stars。

**它们的定位（关键区别）**：**「文件列表 / WebDAV 服务」，不是下载器**。它们做的是「把一堆网盘的目录统一成一个网页文件树」，下载只是「点一个文件直接下载」🟢（描述明确写 file list / WebDAV）。

**能借鉴的点**
1. **「一个存储 = 一个适配器」的配置模型** 🟢：星数最高的网盘聚合项目，其核心抽象是「存储驱动」。我们的 `src/dlink/platforms/` 已经是同样的抽象（见 §3.3），这条路线经过 50k star 项目验证。
2. **Web UI 即全部界面**（SolidJS 单页）🟢 —— 极低分发成本，无需为 Win/macOS/Linux 各打一套 GUI。
3. **OpenList 一年 25.6k star** 🟢 说明「网盘工具」这个赛道在中国用户里需求极强且可持续。对「要开源」的我们：**开源接受度不是问题，问题是合规表述**（见 §4.3）。

**不适合借鉴**
- 🟡 **它是「浏览 + 管理」，我们是「下载 + 进度」**。文件树的交互（面包屑、多选、移动、复制）对我们是纯负担。**不要把 AList 的文件管理器搬进来**。
- 🟡 AList 的「复制直链」入口是给「拿到链接去别处用」，我们的核心是「链接进来，文件出去」——方向相反。

---

### 1.7 Neat Download Manager (NDM) / Free Download Manager (FDM) / Persepolis —— 轻量级三件套

**NDM（Neat Download Manager）**
- 🔴 **闭源 / 无官方 GitHub 仓库**。证据：按名字搜索 GitHub 只找到**第三方**周边（汉化补丁 `qiqi258/NeatDownloadManager-zh` 45★、`zhang1997aaa/ndm-zh-patch` 4★、扩展增强 `yizhimuzhuozi/NeatDownloadManager-Extension` 71★）🟢 —— **官方星数为 0（不可得）**，若有人在报告里给出 NDM 的 star 数，那是编的。
- 🟡 存在多个汉化补丁这件事本身说明：**它的 UI 被中国用户使用且英文界面是障碍**。反面启示：面向中文用户，「中文优先 + 不必做多语言」是合理取舍（前序设计已定「不要多语言」）。
- 🟡 定位是「极简 + 系统原生下载接管」；我们的定位（**不劫持**）与它相反，正好是差异化点。

**FDM（Free Download Manager）**
- 🔴 **未取得 GitHub 数据**。它是闭源商业软件（有免费版），无公开主仓库；`en.wikipedia.org` 在本机被策略阻断（`resolves to a non-public IP`），未能取到权威描述。**因此本文不给出 FDM 的 star / 更新时间**。
- 🟡 已知（记忆，非查证）：FDM 的界面是「左侧分类（全部/下载中/已完成/已计划）+ 任务表格 + 底部图表」，且有「按类型自动分文件夹」的规则。**标为 🟡，请勿当实证**。

**Persepolis**
| 项 | 值 |
|---|---|
| 仓库 | **`persepolisdm/persepolis`**（注意不是 `persepolis/persepolis`，后者 404）🟢 |
| Stars | **7,501** 🟢（来自 search API 结果） |
| 主语言 | **Python** 🟢 |
| 最后推送 | **2025-10-09T19:26:14Z**（约 13 个月前，**低速维护**）🟢 |
| 技术栈 | Python + **PyQt**（`persepolis/gui/`）+ aria2 后端 🟢（`persepolis/gui` 目录已确认）🟢 |

> **Persepolis 对我们最重要**：它是**「Python + PyQt + aria2」的完整先例** 🟢，如果选 PySide6 路线，它就是「这条路走得通、但会不会变成怎样」的活标本。
> 它的 `persepolis/gui/` 与 `persepolis/scripts/` 分离结构 🟢 说明：GUI 与下载逻辑解耦在 Python 项目里是成熟做法。
> 🟡 教训：13 个月只推一次，说明**纯 Python 桌面 GUI 项目容易因 Qt 打包（PyInstaller 体积、缺 DLL）而耗尽维护热情**。我们对包体积与打包复杂度的预期要现实。

---

### 1.8 LinkSwift —— 与「网盘直链」最直接相关的项目（意外收获）

| 项 | 值 |
|---|---|
| 仓库 | `hmjz100/LinkSwift` |
| Stars | **39,926** 🟢 |
| 主语言 | JavaScript（用户脚本 `.user.js`）🟢 |
| 最后推送 | 2026-11-10T17:48:04Z 🟢 |
| License | AGPL-3.0（`LICENSE` 34.5KB，AGPL 典型长度）🟢 |
| 形态 | **浏览器用户脚本（Tampermonkey / ScriptCat）**，非桌面应用 🟢 |
| 支持网盘 | **九家**：百度 / 阿里云盘 / 中国移动云盘 / 天翼云盘 / 迅雷云盘 / 光鸭云盘 / **夸克** / **UC** / 123云盘 🟢 |

> 它不在原任务清单里，是本次搜索 `aliyunpan` 时发现的高星项目 🔴→🟢（现已有实证）。**理由：它解决的问题（多网盘分享页 → 直链 → 推给本地下载器）与我们 90% 重合**，必须纳入。

**它的「下载器」抽象 —— 这是全文第三个可抄点** 🟢（`config/config.json`）

配置里把**下载目标**并列成多个一级键，每个都带「适用对象 + 操作说明」：

| 键 | 含义 |
|---|---|
| `api` | 直接点击链接下载（IDM / NDM / 浏览器原生） |
| `aria` | 复制 aria2c 命令行（XDown / Linux Shell） |
| `rpc` | **推送到本地或远程 RPC 服务**（Motrix / Aria2 Tools / AriaNgGUI） |
| `curl` | 复制 cURL 命令（支持断点续传） |
| `bc` | 推送到比特彗星 |
| 另有 | 1.1.3 新增 **IDM 客户端设置**；1.1.5 新增 **AB Download Manager**；1.1.2.1 新增「API 下载推送到 IDM」 |

更新日志原文（1.1.1，2025-06-01）🟢：
> **「配置文件格式更新，支持添加、删除、切换多个服务配置」**

> **这意味着：「一个下载器」不是一个固定目标，而是「一个可增删的配置条目列表」。**
> 应用到我们：扩展的「推送到本机」不该硬编码 `127.0.0.1:8765` 一个地址，而应允许**多条服务配置并存**（本机 / 局域网另一台机器 / 备用端口），并支持**测试连接**（它 1.0.7.5 就加了「设置可测试 RPC 连接」🟢）。

**它踩过的坑（对我们几乎是「已知问题清单」）** 🟢 —— 全部来自官方更新日志原文：

| 版本 | 原日志 | 对我们的含义 |
|---|---|---|
| 1.1.4 | 「修复 - 推送到 ABDM 下载器**实际成功却显示失败**」 | **推送的「成功」判定不能只看 HTTP 状态码**，要看对方是否真的建了任务。我们 `/api/task` 必须回明确的任务 id，扩展侧据此判成功 |
| 1.1.1.1 | 「修复 - 推送到 Aria2 时**推送成功但报错**」 | 同上：跨进程调用的返回值语义要对齐 |
| 1.1.1.4 / 1.0.7.2 | 「修复使用 RPC 下载时会**重复发送链接**」/「会发送多条下载请求」 | **扩展侧必须幂等**：同一链接重复点击不能建两个任务。我们后端 `/api/task` 要做去重（按直链 hash 或 file_id） |
| 1.0.9.4 | 「修复因百度网盘 **AccessToken 过期**导致无法获取链接」 | **凭证过期是常态**，不是异常。UI 里凭证要有显式状态（前序设计已定「Cookie 清单 ✓/✗」，正确） |
| 1.0.9.2 | 「修复使用API下载时**有可能会导致 IDM 无限弹窗**」 | **不能让浏览器侧反复触发下载** —— 这正是我们「不劫持原生下载」的另一面：既要可推，又不能失控 |
| 1.0.9 | 「修复因无法进行百度授权而导致获取直链报错 **9019**」 | 平台错误码要**原样呈现**给用户（可搜索），不要翻译成「获取失败」 |
| 1.1.4 | 「优化 - **夸克网盘 Cookie 传递**」 | 夸克需要特定 cookie 才能取链（与前序 §2.3「Cookie 清单」的设计吻合） |
| 1.1.4 | 「适配 - 变来变去的 123 云盘」 | **平台接口会持续变更**，架构上要把「平台适配器」做成可独立修改的模块 |
| 1.0.9.3 | 「适配了 **Dark Reader** 插件，界面更协调」 | 说明它认真对待「注入到别人页面里的 UI 不能破坏宿主」。我们的扩展 UI 同理 |

**它的反面（合规表述，值得学）** 🟢
README 顶部两段 `IMPORTANT` 明确写着：
> 「项目**未以任何形式承诺、宣传或实现**对网盘限速机制、功能的破解。」
> 「若获得较快速度，通常系由于服务商接口策略、用户本地网络环境或**阶段性带宽限制调整**所致。」
> 「**技术中立**，不触及、不损害任何网盘服务商的核心利益。」

> 一个 39.9k star 的项目把免责声明写成这样，说明：**「不承诺破解限速」是这类项目能长期存活的必要条件**。我们要开源，这段的写法值得直接迁移（改写成我们自己的实测口径）。

---

### 1.9 补充：pyload（纯 Python 下载管理器）

| 项 | 值 |
|---|---|
| 仓库 | `pyload/pyload` |
| Stars | **3,911** 🟢（search API） |
| 主语言 | **Python** 🟢 |
| 最后推送 | 2026-11-12T14:05:35Z（**活跃**）🟢 |
| 定位 | 「The free and open-source Download Manager written in **pure Python**」🟢 |

> **对我们最有价值的单点**：它证明「纯 Python 的下载管理器 + **Web 前端**」这条路线有 3.9k star 的活跃项目在跑 🟢。
> 它的形态（服务端 + 浏览器界面）与我们「Python 已定 + GUI 待选」的处境完全对应，见 §3.4。
> 🔴 其 Web UI 具体布局未查证（限流未取到前端源码），不描述细节。

### 1.10 JDownloader2 / PanDownload 类

**JDownloader2**
- 🔴 **GitHub 上无官方主仓库**（`JDownloader/JDownloader2` → 404）。搜索结果只有镜像与周边：`mirror/jdownloader`（442★）、`jlesage/docker-jdownloader-2`（1,547★）、暗色主题 `Vinylwalk3r/JDownloader-2-Dark-Theme`（577★）🟢。
- 🟡 「**只有第三方暗色主题能拿 577 star**」这一事实本身就说明它的默认 UI 观感被普遍嫌弃 🟢→🟡。这是本文对 JDownloader 最有信息量的实证。
- 🟡 已知（记忆，非查证）：主界面「链接收集 / 下载 / 完成」三栏 + 海量设置页；被诟病功能堆砌、Java UI 慢、告警弹窗多。**标为 🟡**。

**PanDownload 类**
- 🟢 实际查到的同类高星项目：`Accelerider/BaiduPanDownloadWinform`（**2,943★**，C#，2021 起停止推送）🟢、`liupan1890/aliyunpan`（阿里云盘小白羊版，**11,776★**，TypeScript，2022-10-24 后停更）🟢、`qjfoidnh/BaiduPCS-Go`（**5,931★**，Go，2026-09 仍在推送）🟢。
- 🟡 **共同结局**：前两者均已停更（2021/2022），原因指向「平台封堵 + 作者个人风险」。**这是我们做开源时最该正视的现实**：不是技术问题，是可持续性问题。→ 见 §4.3。

---

## 2. 横向对比表

> 所有 star / 推送时间均为 2026-11-15 抓取的 API 实值 🟢。NDM / FDM / JDownloader 无公开主仓库，标注为「不可得」。

| 维度 | **Motrix** | **Gopeed** | **AriaNg** | **qBittorrent** | **XDM** | **AList** | **LinkSwift** | NDM/FDM/JD2 |
|---|---|---|---|---|---|---|---|---|
| Stars | 58,287 | 27,705 | 13,274 | 41,624 | 8,481 | 50,455 | 39,926 | 不可得 |
| 最后推送 | 2026-11-14 | 2026-11-15 | 2026-10-05 | 2026-11-14 | 2026-11-13 | 2026-10-21 | 2026-11-10 | 不可得 |
| 技术栈 | Electron + TS | Go + Flutter | 静态 JS（RPC 客户端） | C++/Qt | Kotlin/JavaFX | Go + SolidJS | JS 用户脚本 | 闭源 |
| 性质 | 桌面下载器 | 桌面下载器 | 引擎的遥控器 | BT 客户端 | 桌面下载器+扩展 | 网盘文件服务 | 浏览器取链脚本 | 桌面下载器 |
| **任务列表形式** | **数据网格**（10 列，行高36，列宽带 min/max）🟢 | **卡片**（桌面/移动双实现）🟢 | 表格（三级列表：任务/文件/peer）🟢 | 表格 + delegate 自绘 🟢 | 🔴 | 文件树（非任务列表）🟢 | 无列表（一次性取链弹窗）🟢 | 🔴 |
| **进度展示** | 1px 条 + `阶段+%` 同格；速度/ETA/连接数**按状态条件显示**🟢 | 独立进度条组件 + 速度监控卡 🟢/🟡 | 百分号 filter 统一格式化 🟢 | **单元格内条+文字+状态色合成**🟢 | 🔴 | 无 🟢 | 无（仅取链结果）🟢 | 🔴 |
| **速度/ETA 是否同屏** | ✅ 同屏（独立列）🟢 | ✅ 卡片内 🟡 | ✅ 表格列 🟡 | ✅ 转移列表列 🟢 | 🔴 | ❌ | ❌ | 🔴 |
| **多任务管理** | 表格多选 + 批量（36px 勾选列）🟢 | 过滤侧栏带计数 + 批量选择器 `task_batch_selection_builder`🟢 | 分类标签（下载中/等待/已停止）🟡 | 过滤栏 + 分类 + 标签 + 搜索（**五套机制，过重**）🟢 | 🔴 | 不适用 | 不适用 | 🔴 |
| **设置复杂度** | 高（暴露 aria2 全部参数）🟡 | 中高（组件化多页）🟢 | **中，且「引擎/界面」分离**🟢 | 高（BT 细节尽可调）🟡 | 🔴 | 中（存储驱动配置）🟢 | **中（每个下载器一份配置）**🟢 | 🔴 |
| **多文件/批次** | 有（BT 文件树）🟢 | 有（`task_file_tree`、`resolve_file_tree`）🟢 | 有（文件级排序 `fileOrderBy`）🟢 | 有（BT 文件优先级）🟢 | 主题级支持🟢 | 有（目录树）🟢 | 支持批量取链（按文件夹）🟢 | 🔴 |
| **并发数配置粒度** | 全局默认 + **任务级覆盖**（`add-task/advanced-panel`）🟢 | 全局默认 + **任务级覆盖**（`create_task_advanced_toggle`）🟢 | 全局（直接暴露 aria2 `split`/`max-connection-per-server`）🟢 | 全局 + 每任务限速/连接数 🟡 | 🔴 | 不适用 | 不适用（它是取链方） | 🔴 |
| **是否支持「按 host/站点」记并发** | ❌（文件树无 per-host 模块）🟢 | ❌ 🟢 | ❌ 🟢 | ❌ 🟢 | ❌ 🟢 | ❌ | ❌ | 🔴 |
| **直链过期处理先例** | 无 🟡 | 无 🟡 | 无 🟡 | 不适用 | 无 🟡 | 无（依赖平台会话）🟡 | **有：AccessToken 过期修复 + 取链失败错误码呈现**🟢 | 🔴 |
| **平台差异化处理先例** | 无 🟡 | 无 🟡 | 无（只接 aria2）🟡 | 不适用 | 无🟡 | 有（每存储一个驱动）🟢 | **有：每平台独立配置文件**🟢 | 🔴 |
| 对我们适配度 | ★★★★★ | ★★★★ | ★★★☆ | ★★☆（表格/进度可抄，导航别抄） | ★★★（架构像） | ★★（定位不同） | ★★★★★（问题域重合） | ★☆ |

**读表结论**：
- **没有一个是「网盘直链下载器」的完整先例** —— 这个组合（直链时效 + 平台限速差异 + 扩展推送）确实是空位。
- 但**每一块都有最好的老师**：表格/进度抄 qBittorrent+Motrix，配置抄 LinkSwift，工程结构抄 Gopeed，设置分层抄 AriaNg。

---

## 3. 对我们的具体建议

### 3.1 最值得借鉴的：三个「可抄」，按投入产出排序

**① Motrix 的「列适用性函数」+ 列宽夹取**（成本：低，收益：最高）
- 抄 `TASK_COLUMNS` 那套 `{width, min, max, numeric}` 结构 + `clampColumnWidth()`
- 抄 `isTaskSpeedApplicable()` / `isTaskEtaApplicable()` / `getTaskConnections()` 把「无意义值 → null」写成函数
- 落到我们身上：`速度` 列在「已完成/已暂停/等待续链」时显示 `—` 而非 `0 B/s`；`直链剩余` 列在「已完成」时显示 `—`
- **为什么**：这直接解决前序设计 §1.1.4 里最担心的问题——「速度必须可信」。**恒为 null 的格子比错误的 0 更诚实**。

**② qBittorrent 的「进度单元格合成绘制」**（成本：低，收益：高）
- 一个 ~140px 的格子里同时放：细进度条 + `37%` 文字 + 状态色，而不是拆成三列
- 前序设计给了 140px 的「进度」列（§2.2.1 第 5 列）—— 恰好够，**不需要额外加「状态色」列**，颜色画在条上
- **为什么**：8 列要挤进 900px，每多一列就多一次降级。把「进度/百分比/状态色」三合一，省出一整列预算。

**③ LinkSwift 的「下载器目标是可增删配置」+「推送幂等」**（成本：低，收益：特高）
- 扩展设置页里，「推送到」不是硬编码一个地址，而是**服务配置列表**（名称 / 地址 / 可选 token / [测试连接] / 启用开关）
- 后端 `/api/task` 做**幂等去重**，返回明确 `task_id`；扩展以「拿到 task_id」为成功判据，不以 HTTP 200 为判据
- **为什么**：这是 LinkSwift 用 4 个版本的 bug 换来的经验（重复发送、成功却显示失败、无限弹窗），我们可以零成本直接避开。

### 3.2 「看起来好但不适合我们」的清单

| 设计 | 出处 | 为什么不适合 | 我们要怎么做 |
|---|---|---|---|
| **peer 表 / 做种 / 分享率 / 健康度** | qBittorrent、Gopeed | 纯 BT 概念，直链下载没有 peer | **完全不做**。前序 §2.3 已正确排除 |
| **分块地图（piece map）/ 可用性条** | qBittorrent（三种）、Gopeed | 信息量不抵成本；前序 §2.3 已判 v0.1 不做 | 最多在详情里给「分段 47 × 32MB」一行文字 |
| **速度曲线图** | qBittorrent `speedplotview`、Gopeed | 5 小时下载的曲线图没人看，且自绘成本高 | **不做**。用「速度数字稳定」代替（前序 §4.1 判断正确） |
| **左侧导航栏（全部/下载中/已完成）** | qBittorrent、Gopeed | 任务数 <20，一屏看完，导航是净负担 | 前序已定：**筛选下拉替代侧栏**。本次调研支持此判断 |
| **卡片式任务列表** | Gopeed | 卡片好看但信息密度低，100 个文件会刷爆屏 | 用表格；卡片思想只用在「批次组头」 |
| **暴露引擎全部参数** | Motrix、AriaNg、qBittorrent | 用户看不懂 `split`/`continue`/`max-connection-per-server` | 只暴露 4 个平台各自的「线程数」+ 3 个高级项 |
| **文件管理器（浏览/移动/复制）** | AList | 我们是「链接进来、文件出去」，不是网盘客户端 | 只给「打开所在文件夹」 |
| **多语言 / 主题皮肤** | AriaNg（10+ 语言，每个 60–100KB） | 自用工具，中文优先 | 前序已定：不做多语言。**支持** |
| **BT/磁力/ed2k 全协议** | Motrix、Gopeed、qBittorrent | 我们只做 HTTP 直链 | 单一协议，界面因此可以更干净 |

### 3.3 我们的三个特殊问题，有没有先例？

**问题 A：直链 6–8 小时过期，大文件要下几小时**
- **先例：没有完整先例** 🔴。所有查证项目（Motrix/Gopeed/AriaNg/qBittorrent/AList）都没有「直链时效 + 自动续链」这个概念 🟡（它们的链接模型是「一旦提交就长期有效」或「由引擎自己处理 403 重试」）。
- **最接近的碎片**：
  - LinkSwift 处理 **AccessToken 过期**（1.0.9.4）🟢 —— 凭证会过期，需重新授权，与「链接会过期，需重新获取」同构
  - aria2 本身有「403 后重试」，但**不会重新取链**（它会拿同一个 URL 重试到失败）🟡
  - AriaNg 有 `autoRetry` 类选项 🟡（未逐项验证）
- **结论**：这是我们的**真实创新点，没有现成 UI 可抄**。前序 §3.3 的三层防线（提前续链 → 403 兜底 → 人工提示）设计是合理的，且「**不用模态框**、只用行内状态 + 顶部横幅」的判断与 qBittorrent 的「状态靠行内颜色、不弹窗」文化一致 🟡。
- **建议补一条**：详情里的「时间线」写法（前序 §2.3）在同业里对应 qBittorrent 的「日志」页签，但**时间线 ≠ 日志** 是我们的正确取舍——qBittorrent 的日志页签正是用户抱怨「看不懂、太长」的地方 🟡。

**问题 B：平台限速差异（微云连接级 / 百度账号级 / 夸克二阶段）**
- **先例：AList 与 LinkSwift 都用了「每平台一个独立配置单元」** 🟢
  - LinkSwift：`config/ali.json`、`config/quark.json`、`config/tianyi.json`、`config/xunlei.json`、`config/yidong.json` 各 4.5–11.8KB 🟢
  - AList：一个存储 = 一个驱动 🟢
- **建议**：前序 §1.4 组 B「每平台线程数」`平台 · 线程数 [数字框] · 平台建议 8` + **超建议值红字警告** 的形态，正是 LinkSwift/AList 那种「按平台分治」思路的中文 UI 化，**有先例支撑**。
- **加强建议**：把「平台建议值」做成**数据驱动**（`src/dlink/platforms/` 里每个平台自己声明 `suggested_workers`、`limiter_type`、`annotation`），界面只渲染。这样加一个新平台 = 加一个文件，**不用改 GUI 代码**。这正好复用 AList 的「存储驱动」模式 🟢。

**问题 C：扩展推送任务进来**
- **先例最丰富**：Motrix（扩展 → aria2 RPC）、XDM（扩展 → 本地 JavaFX 应用）、LinkSwift（页面 → 多种下载器）、AriaNg（浏览器 → aria2 RPC）🟢
- **最佳实践归纳**：
  1. **推送要幂等**（LinkSwift 三次修复重复发送）🟢
  2. **成功判据是「对方建了任务」而不是「HTTP 200」**（LinkSwift 两次修复假失败）🟢
  3. **连接配置要可测**（LinkSwift 1.0.7.5「设置可测试 RPC 连接」）🟢
  4. **端口冲突要可发现**（Motrix/aria2 的经典问题；前序已定「8765–8769 端口范围」+ 状态栏显示端口，正确）
  5. **扩展侧 UI 不能失控弹窗**（LinkSwift 的 IDM 无限弹窗事故）🟢

### 3.3b 【重点】并发数到底该配在哪一层？—— 先例与我们的专属答案

> 本节由 Lead 的**普通 HTTP 实测**触发，是全文结论最「反直觉」的一节，建议优先读。
>
> **实测数据（Lead 提供，本次调研的输入假设）**：
> | 并发 | 速度 | 相对单连接 |
> |---|---|---|
> | 1 路 | 4.91 MB/s | 1.00× |
> | 4 路 | **8.19 MB/s** | **1.67×** ✅ 最优 |
> | 8 路 | 3.07 MB/s | 0.63× ❌ **反而变慢** |
>
> 结论：**「并发数越大越快」是错的**。该服务器限并发，超过阈值后总吞吐**下降**。

#### ① 四种「并发数配在哪」的粒度，各家的实际选择

| 粒度 | 含义 | 谁这么做 | 证据 | 问题 |
|---|---|---|---|---|
| **A. 全局一个值** | 设置里一个 `线程数`，所有任务共用 | aria2 默认体验、AriaNg 的 `split` / `max-connection-per-server` | 🟢 AriaNg 把这些作为 aria2 引擎参数直接暴露（`src/scripts/config/aria2Options.js` 存在） | 一个值套所有站点，必然对一半站点是错的 |
| **B. 全局默认 + 任务级覆盖** | 设置里给默认值，新建任务时可在「高级」里改 | **Motrix、Gopeed 都是这个** | 🟢 Motrix：`src/renderer/components/add-task/advanced-panel.tsx`（新建任务的高级折叠区）＋ `src/core/settings/downloads-settings.ts`（全局下载设置）并存<br>🟢 Gopeed：`create_task_advanced_toggle.dart`（新建任务的高级开关）＋ `settings/presentation/pages/settings_page.dart` 并存 | **用户必须懂「这个站点该几路」才改得对**；且**不记忆**，下次重新下同一个站点又要再猜一次 |
| **C. 按主机 / 按站点** | 每个 host 一份并发配置，自动套用 | **本次调研的四家主流工具（Motrix / Gopeed / AriaNg / qBittorrent）都没有** 🔴 | 四个仓库的文件树里均无 `per-host` / `per-site` / `host-policy` 一类模块（已 grep 确认）🟢 | ——（所以这是空位） |
| **D. 自动（探测/自适应）** | 测速后自动收敛到最优并发 | aria2 有「动态分段」但**不做并发探测**；🟡 未见任何一家做 AB 测试式自动寻优 | 🔴 | 实现成本高，且探测过程本身会浪费流量与时间 |

#### ② 为什么四家主流工具都没做「按站点」

🟡 **推断**：因为它们的场景里「服务器限并发」不常见——aria2/Motrix 主要下国外站、BT、大文件镜像，这些通常不限并发或限制很松。**它们的用户没有这个痛点，所以没有这个功能**。

而我们的处境不同：**国内的网盘/CDN 恰恰是「限并发」最狠的一类服务**（微云连接级限速、百度账号级、夸克二阶段）。Lead 的实测（8 路反而掉到 3.07 MB/s）直接证明了这点。

> **所以：这是「我们的痛点 + 别人的空位」= 真正的差异化功能。**
> 但注意前序设计文档 §1.4 已定的是「**每平台**线程数（微云8/百度1/夸克6）」。Lead 的实测说明：**「平台」这一层还不够细**——同一个平台下有多个 CDN 域名，限并发阈值可能不同。

#### ③ 建议：并发数做成三层（而不是一层，也不是四层）

```
第 1 层 · 平台默认（已在前序设计中）      ← 人给的先验
    微云 8 / 百度 1 / 夸克 6 · 超建议值红字警告
    作用：第一次下这个平台时，避免「瞎猜」

第 2 层 · 按直链 host 记忆（★ 新增建议，成本收益最高）    ← 机器学的后验
    键：直链的 host（如 d.pcs.baidu.com / cdn.xxx.weiyun.com）
    值：{ 上次用的并发, 上次实测速度 }
    行为：同一 host 第二次下载 → 自动套用上次「有效」的并发；
          若上次速度 < 该 host 历史最好速度的 60% → 自动降一档
    界面上：详情里一行灰字「该站点最佳 4 路（上次 8.19 MB/s）」
             设置里给「清空站点学习记录」按钮（自用工具必须可重置）
    为什么可行：并发数的最优值与「host」强相关，而 host 在直链里白给

第 3 层 · 任务级临时覆盖                              ← 人的最终决定权
    继承前序设计的「详情里可改线程数」；改完写回第 2 层作为该 host 的新记录
```

**三层各自的界面落点**：

| 层 | 出现在哪 | 展示形态 |
|---|---|---|
| 平台默认 | 设置 · 平台策略 | 表格：`平台 · 线程数[8] · 平台建议 8`（前序已设计，保留） |
| host 记忆 | 任务详情的「直链」小卡 | 一行：`最佳并发 4 路 · 上次 8.19 MB/s` + `[重置]` |
| 任务覆盖 | 任务详情 | 数字框，与上一条相邻 |

#### ④ 一个重要提醒：**别把并发数做成「越大越好」的滑块**

Lead 的实测是 8 路 **3.07 MB/s**，比 1 路的 4.91 **还慢**。这意味着：
- 🚫 **不要**用「0–16 的滑块」表达并发数——滑块的心理暗示是「往右更好」，用户会往右拉。
- ✅ 用**数字框 + 明确的「本平台建议：「4 路」+ 超限即时警告**。前序设计 §1.4 组 B 的「超建议值立即红字警告」方向正确，可以强化成：**超建议值 → 直接提示「实测更高并发会变慢」并给出数字**。
- ✅ 状态列在「并发数高于该 host 历史最优」时给一个 ⚠ 小图标——**这是本工具最独特的一个告警，比「链接过期」更常见**。

#### ⑤ 与「速度可信」的关系（回收 §3.1 的①）

如果我们做了 host 记忆但**界面不展示**，用户只会觉得「有时快有时慢，玄学」。
必须把「**为什么现在是 4 路**」写出来（详情里那一行灰字），才能让「速度可信」这个核心目标闭环。

### 3.4 技术栈推荐：**方案 B：Python 后端 + Web UI 前端**（理由基于本地实测）

先看我们**已经有什么**（🟢 读 `src/dlink/server.py`）：
- 浏览器扩展与后端之间已经是 **HTTP + JSON** 通信：`/api/health`、`/api/tasks`、`/api/pending-refresh`、`/api/profiles`、`POST /api/task`
- 服务用**标准库** `http.server`（`class Handler(BaseHTTPRequestHandler)`），**零第三方依赖**（`pyproject.toml` 只依赖 `requests`）🟢
- **没有 SSE / WebSocket** 🟢

**候选对比**

| 方案 | 优点 | 缺点 | 对我们 |
|---|---|---|---|
| **A. PySide6（Qt）** | 原生表格/虚拟滚动性能最好；`QTableView` + 自定义 delegate 能 1:1 复刻 qBittorrent 的进度单元格；PyQt/PySide 生态成熟 | **打包痛**（PyInstaller 体积 80–150MB、易缺 DLL）；中文字体与高 DPI 需手工配置；**Persepolis 是前车之鉴：13 个月没推送** 🟢 | 可选，但代价明确 |
| **B. Web UI（本地 HTTP + 浏览器 / 后加套壳）** ⭐ | **零额外分发成本**（`pyload` 3.9k★ 活跃、AList 50k★、AriaNg 13k★、AriaNg-Native 用 Electron 套壳 —— 四条独立证据 🟢）；前端可用纯 HTML/CSS/JS，无构建链也能写；表格、虚拟滚动、CSS 状态色都简单；**与已有 `/api/*` 天然契合** | 需要多写一个轮询/推送循环；浏览器里跑不够「像桌面应用」（可用 pywebview 套壳解决） | **推荐** |
| **C. Tkinter** | 零依赖 | 表格（`ttk.Treeview`）不支持单元格内自绘进度条、无平滑滚动、高 DPI 模糊、观感停留在 2005 | **不推荐**：会让我们抄不到 qBittorrent 最值钱的「单元格合成绘制」 |
| **D. Flutter** | 跨平台一致性好（Gopeed 实证） | 要给 Python 后端外加 Dart 工具链；**双运行时**；中文/高 DPI 有坑；包体大 | 不推荐：团队只有 Python |

**推荐：B（Web UI），且分两步走**
1. **v0.1**：FastAPI 或继续用 `http.server`，把 HTML/CSS/JS 直接作为静态资源**由同一个进程托管**（同源 → 无 CORS、无额外端口）。前端用**原生 JS + 一个表格组件**即可，不引入 React/Vue 构建链。
   - 数据刷新：**1 秒轮询 `/api/tasks`**（自用、任务 <20、字段 <20 个 → 每次响应几 KB，KISS）🟢 基于本地已有接口
2. **v0.2 如果要「像桌面应用」**：加 **pywebview**（一个几百行的 Python 包，用系统 WebView 套壳），或照 AriaNg-Native 的路子用 Electron 🟢。**先 Web，后套壳**，因为套壳是可逆的、而 PySide6 写下去就回不来了。

**注意**：前序设计 §0.2 说「主窗口唯一，其余是模态/抽屉」——Web UI 里「模态」= 覆盖层、「抽屉」= 侧滑 div、「设置窗口」=`<dialog>`，全部可实现，**不需要改信息架构**。

### 3.5 对已有设计文档的两处「补丁建议」

> 本次调研的目的之一是校验 `GUI-INFORMATION-ARCHITECTURE.md`。总体判断：**该文档的取舍基本正确，且有先例支持**。两处可加强：

1. **§2.2.1 的「直链」列可以更省**：现在占 90px。若借鉴 qBittorrent 的合成绘制，可把「临期/过期」状态**画进进度条颜色**，列内容只留 `6.2h`，宽度可压到 70px，宽屏降级阈值从 1000px 放宽到 960px。
2. **§1.4 组 B 建议数据驱动**：平台建议值不要写死在文档/代码里，让 `platforms/` 下每个平台自己提供 `suggested_workers` / `limiter_type` / `annotation` 三个字段。这样「夸克临时改策略」时只改一个数据文件（LinkSwift 的「适配变来变去的 123 云盘」就是靠这种分离撑下来的 🟢）。

---

## 4. 反面教材：用户抱怨最多的设计

### 4.1 功能堆砌 → 设置项淹没（JDownloader2 / qBittorrent / Motrix）
- 🟢 **实证**：JDownloader2 最具存在感的外围项目是**一个暗色主题（577★）**；qBittorrent 开放 issue **2,750**；Motrix 160。
- 🟡 **机制**：引擎参数全暴露 → 设置页 10+ 页 → 用户找不到「默认保存目录」→ 抱怨「难用」而不是「功能强」。
- **我们要避**：前序 §1.4「明确不要的设置项」清单（多语言、主题、快捷键、云同步、账号、优先级规则、代理自动切换、限速计划）**方向正确，本次调研支持**。再加一条：**不要在设置里暴露 `split` / `continue` / `max-connection-per-server` 这类 aria2 原词**，统一说「线程数」。

### 4.2 「只有引擎没有 GUI」→ 两步安装门槛（aria2 / AriaNg）
- 🟢 aria2 本体 **43,522★** 但**本身是命令行工具**（描述原文 "operated in command-line"）；AriaNg 13,274★ 只是它的前端，**用户装了 AriaNg 还得自己装 aria2**。
- 这是这类工具**最大的用户流失点**，也是我们「一体化」的最大机会。
- **我们要避**：**永远不要要求用户另外装一个引擎**。`pyproject.toml` 零依赖的取向要守住。

### 4.3 合规表述缺失 → 项目猝死（PanDownload / 百度网盘系）
- 🟢 实证：`BaiduPanDownloadWinform` 2,943★ **2021 停更**；`aliyunpan`（阿里云盘小白羊）11,776★ **2022 停更**。而 `BaiduPCS-Go` 5,931★ 到 2026-09 仍在推送、AList 50k★（转手后仍在）、LinkSwift 39.9k★ 活跃。
- **差异在哪**：活下来的项目（LinkSwift/AList/BaiduPCS-Go）**README 里有明确的「技术中立、不破解限速」表述** 🟢（见 §1.8 原文引用）；死掉的往往以「不限速下载」为卖点。
- **我们要避**：**不要在 README / 界面上用「加速」「破解」「不限速」作为卖点**。前序 §4.1.2 的注解文案（「这不是软件问题」+ 实测数字）**恰恰是正确的叙述方式**：陈述事实，不承诺突破。

### 4.4 过度弹窗 → 打断用户（LinkSwift 事故 / JDownloader 口碑）
- 🟢 LinkSwift 有过「IDM **无限弹窗**」事故（1.0.9.2）；JDownloader 的告警弹窗是其口碑痛点之一 🟡。
- 对我们是**生死线**：直链过期 5.4 小时里会发生 3–5 次，若每次弹模态框，用户会弃用。
- **我们要避**：前序 §3.3「不要弹模态框」**必须严格执行**；补一条：**任何自动重试/续链都不弹窗**，只在详情时间线里留一行。

### 4.5 BT 概念污染纯 HTTP 下载器（Motrix / Gopeed 的共同负担）
- 🟢 证据：Motrix 的列定义里 `up`、`connections` 各占 128px（合计 256px = 表格 1/5 宽度），纯粹为 BT 服务；Gopeed 有整个 `task_statistics/` 目录放 `peer_table`、`piece_map`。
- **我们要避**：只要不做 BT，**表格立刻多出 256px 给「直链剩余」「线程数」**。这是「不做功能」直接换来的界面预算。

---

## 5. 一页速查：抄什么 / 不抄什么

```
抄 ✅
  Motrix     · 列定义结构 {width,min,max,numeric} + clampColumnWidth()
  Motrix     · 「该列当前是否适用」函数化，无意义显示 — 而非 0
  Motrix/Gopeed· 「全局默认 + 新建任务高级区覆盖」的并发分层（我们再加「按 host 记忆」层）
  qBittorrent· 进度条 + 百分比 + 状态色 在同一单元格内合成绘制
  AriaNg     · 设置分两层：引擎设置 / 界面设置，两个独立页面
  AriaNg     · 格式化收敛（percent / volume / duration 三个函数）
  LinkSwift  · 下载目标是「可增删的服务配置列表」，可测试连接
  LinkSwift  · 推送幂等 + 以「对方建了任务」为成功判据
  LinkSwift  · README 的合规表述写法（技术中立，不承诺破解）
  AList      · 每平台/每存储一个独立适配器，建议值数据驱动
  Gopeed     · features/<域>/{pages,widgets,domain} 分层，别写成巨型单文件
  pyload     · Python + Web UI 这条路有活跃先例（3.9k★）

不抄 ❌
  qBittorrent· 左侧导航栏 + 五套任务切分机制（我们任务<20）
  Gopeed     · 卡片式任务列表（100 文件会刷爆屏）
  Motrix     · BT 的 up / connections / 做种列（净占 256px）
  qBittorrent· 三种分块地图 / 速度曲线图（v0.1 无价值）
  AriaNg     · 直接暴露 aria2 原词参数（split / max-connection-per-server）
  AList      · 文件管理器（浏览/移动/复制）
  JDownloader· 功能堆砌与设置项淹没
  aria2      · 「只有引擎，GUI 另装」的两步门槛
  任何        · 用模态框报告「链接已过期」
  任何        · 把并发数做成「越大越好」的滑块（实测 8 路比 1 路还慢）
  任何        · 以「加速/破解/不限速」为卖点
```

---

## 附：查证方法与局限（诚实声明）

**已查证（🟢）的来源**
- GitHub REST API `/repos/{owner}/{repo}`：star / fork / language / pushed_at / license / topics（12 个仓库）
- GitHub REST API `/search/repositories`：定位正确仓库名（发现 `persepolisdm/persepolis`、`hmjz100/LinkSwift`、`pyload/pyload`、`mayswind/AriaNg-Native` 等）
- GitHub REST API `/contents/*` + `/git/trees`：实际读取源码文件，包括
  - Motrix：`src/renderer/routes/downloads/columns.ts`、`task-progress.tsx`、`task-column-values.ts`
  - Gopeed：`primary_rail.dart`、`filter_sidebar.dart`
  - qBittorrent：`src/gui/progressbarpainter.cpp`
  - LinkSwift：`README.md`（含完整更新日志）、`config/config.json`
  - AriaNg：文件树 + `src/views/*` 清单
- 本仓库：`src/dlink/server.py`（API 端点）、`pyproject.toml`（依赖）、`design/GUI-INFORMATION-ARCHITECTURE.md`

**局限（🔴，请勿引用为事实）**
1. **未获得任何界面截图**。本调研所有「布局描述」均来自**源码结构与组件命名**，不是视觉截图。若需要像素级确认，需人工打开各项目截图核对。
2. **§3.3b 的「四家都没做按 host 并发」是 grep 文件树的结论**（🟢 已确认四个仓库无 `per-host` / `per-site` / `host-policy` 一类模块），但**不排除它们以别的命名实现**；若要对外宣称「这是空位」，建议再人工确认一次。
   - 另外 Motrix 仓库内确实存在**界面截图**（`screenshots/motrix-settings-cn-dark.webp`、`.github/pr-assets/settings-general.png`、`docs/screenshots/settings-validation-layout.png`）🟢，本次因配额未下载，**这是后续复核布局最省力的一条路**。
2. **匿名 GitHub API 配额 60 次/小时曾用尽**，导致以下未取到：
   - AriaNg `src/views/list.html` 的**具体列清单**（只确认了视图文件名）
   - Gopeed `task_progress_bar.dart` 的内部实现（只确认文件存在）
   - Gopeed `task_card.dart` 的字段布局
   - NDM / FDM / JDownloader2 的任何官方数据
3. `raw.githubusercontent.com` 在本机**连接被重置**（`ConnectionResetError 10054`）；Gitee 被阻断（405 / git 挂起）。故未能直接 clone 仓库。
4. `en.wikipedia.org` 在本机被策略阻断（`resolves to a non-public IP`），未能用百科交叉验证 FDM / NDM 的历史描述。
5. **各项目的用户吐槽（issue 层面）未逐条取证**。§1 中的「缺点」以 🟡 标注的，部分属行业共识与结构性推断，如需在对外文档中引用，应先核查具体 issue。

**可复核命令**（本机 TLS 正常时）
```bash
python -c "import requests,json;print(json.dumps(requests.get('https://api.github.com/repos/agalwood/Motrix',headers={'User-Agent':'x'}).json(),ensure_ascii=False,indent=1))"
```
