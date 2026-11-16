# 构建与发布

## 本地打包 exe

```bash
pip install pyinstaller
python -m PyInstaller DLink.spec --noconfirm --clean
```

产物：`dist/DLink.exe`（单文件，无需安装 Python）

### 打包配置要点（`DLink.spec`）

| 项 | 说明 |
|---|---|
| `hiddenimports` | `collect_submodules('dlink')` —— 平台模块是动态 import 的，不显式列出会 ImportError |
| `datas` | 把 `src/dlink/gui/assets`（图标）打进去 |
| `excludes` | 排除 QtWebEngine / QtMultimedia / QtQuick 等未使用模块，**能省 40MB+** |
| `console=False` | 双击不弹黑框 |
| `icon` | 用 256px 图标 |

### 单文件 vs 目录模式

当前用**单文件**（`--onefile` 语义）：方便分发，但启动时要解压到临时目录，首次启动约 2-3 秒。

如果要更快启动，改成目录模式：把 `EXE(...)` 拆成 `EXE` + `COLLECT`，产物是文件夹。本项目选单文件是因为分发便利性更重要。

### 目录版（onedir）：单文件版起不来时的兜底

单文件版启动时要把自己解压到 `%TEMP%\_MEIxxxxxx`。这一步失败时，
PyInstaller 的引导程序只弹一句 `could not create temporary directory` 就退出 ——
**此时 Python 代码一行都没执行**，日志和异常处理全都用不上（2026-11-16 实际遇到，
用户侧表现为「双击没反应 / 弹个报错就没了」，我们这边拿不到任何线索）。

```bash
python -m PyInstaller DLink-onedir.spec --noconfirm --clean
python scripts/pack_exe_onedir.py      # → dist/dlink-v<ver>-windows-x64-onedir.zip
```

目录版不解压任何东西，双击直接跑，启动还快 2-3 秒；代价是产物为文件夹（zip 分发）。

两个版本都要冒烟：

```bash
python scripts/smoke_exe.py                                     # 单文件
python scripts/smoke_exe.py --exe dist/DLink-onedir/DLink.exe    # 目录版
```

> 顺带：被 `taskkill /F` 强杀的单文件版会在 `%TEMP%` 留下 `_MEI*` 目录，
> 每个约 130MB。本机曾堆了 20 个、共 2.57GB，已清理。

### ⚠️ `console=False` 的陷阱：`sys.stderr` 是 `None`
windowed 进程里没有控制台，`sys.stderr`（以及 `sys.stdout`）是 `None`。
任何 `sys.stderr.write(...)` 都会抛 `AttributeError` —— 而
`http.server.BaseHTTPRequestHandler` 是**每个请求开一个线程**调 `log_message()` 的，
异常直接把线程带走，客户端看到的是「连接被重置且没有任何响应」。

实测症状：**exe 里所有 POST 全挂**（扩展发任务 → 失败；GUI 改设置 → 失败），
而 `GET /api/health` 因为代码里跳过日志所以一切正常 —— 极易误判成「后端没问题」。

所以：
- 日志写入一律 `if stream is not None` 保护（见 `server.py` 的 `log_message`）
- 打包后必须跑 `python scripts/smoke_exe.py`（真起后端 + 真下文件 + 校验字节数）

### ⚠️ 浏览器给的 `filename` 是完整路径

`chrome.downloads` 的 `item.filename` 是 `C:\Users\x\Downloads\a.zip`。
`Path(save_dir) / 该字符串` 在 Windows 上会把 `save_dir` 整个替换掉，
文件写到下载目录之外。`server.safe_basename()` 统一收敛，并有回归测试。

---

## 发布 Release

### 手动步骤

```bash
# 1. 打 tag
git tag -a v0.1.0 -m "DLink v0.1.0"
git push origin v0.1.0

# 2. 用 GitHub CLI 上传（需装 gh）
gh release create v0.1.0 dist/DLink.exe --title "v0.1.0" --notes-file RELEASE_NOTES.md
```

### 用 GitHub API（无需 gh）

见 `scripts/release.py`（本仓库自带）。

### 注意

- `dist/` 和 `build/` 已在 `.gitignore` 里，**不要提交**
- exe 体积较大（60-90MB），GitHub Release 单文件限制 2GB，没问题
- 没有代码签名，Windows SmartScreen 会提示「未知发布者」，这是正常的

---

## CI 自动打包（可选，未配置）

如果要以后自动化，`.github/workflows/build.yml` 大致是：

```yaml
name: build
on:
  push:
    tags: ['v*']
jobs:
  win:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt pyinstaller
      - run: python -m PyInstaller DLink.spec --noconfirm
      - uses: softprops/action-gh-release@v1
        with:
          files: dist/DLink.exe
```

> 当前**未启用**，因为本地打包已经很方便，且能立刻验证产物可用。
