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
