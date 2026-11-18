"""添加下载任务：普通链接 与 aria2c 命令两个入口。

为什么需要 aria2c 那一栏：
    很多直链（百度 token 链、微云 ftn_handler、网盘解析站给的）都是直接甩一条
    aria2c 命令过来，长这样：

        aria2c "https://d.pcs.baidu.com/file/xxx?..." --out "PC.7z" \
               --header "User-Agent:pan.baidu.com"

    URL 里带 &、带签名，手动拆容易截断；UA/Referer/Cookie 又是能不能下成功的
    关键。让软件直接吃这条命令，比让用户自己拆更靠谱。

设计：
    · 两个页签：普通链接（每行一条）/ aria2c 命令（可多条、可续行）
    · 解析交给 `dlink.aria2`（纯函数，有单测）；这里只管界面与提交
    · 提交前把「会被用到的信息」显示出来（文件名 / UA / Referer / Cookie 条数），
      用户能一眼确认没解析错
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QTabWidget, QVBoxLayout,
)

from dlink.aria2 import looks_like_aria2, parse_aria2

from .api import ApiClient
from .theme import GRAY_400, WHITE

_PLAIN_TIP = "每行一个 http(s) 链接。下载器会自动探测文件大小与是否支持分段并行。"

_ARIA2_TIP = (
    "直接粘贴 aria2c 命令，可以多条（每行一条）。支持 --out / --header /\n"
    "--referer / -U / -x；Cookie 头会自动拆出来单独发送。"
)

_ARIA2_PLACEHOLDER = (
    'aria2c "https://d.pcs.baidu.com/file/xxx?fid=...&sign=..." --out "PC.7z" '
    '--header "User-Agent:pan.baidu.com"'
)


class AddLinkDialog(QDialog):
    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加下载任务")
        self.resize(680, 420)
        self.api = api

        v = QVBoxLayout(self)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        # ---- 普通链接 ----
        page_plain = QVBoxLayout()
        tip1 = QLabel(_PLAIN_TIP)
        tip1.setStyleSheet(f"color:{GRAY_400}; padding:4px;")
        page_plain.addWidget(tip1)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("https://example.com/file.zip")
        page_plain.addWidget(self.edit, 1)
        holder1 = QVBoxLayout()
        holder1.addLayout(page_plain)
        self.tabs.addTab(_wrap(holder1), "普通链接")

        # ---- aria2c ----
        page_aria = QVBoxLayout()
        tip2 = QLabel(_ARIA2_TIP)
        tip2.setStyleSheet(f"color:{GRAY_400}; padding:4px;")
        page_aria.addWidget(tip2)
        self.edit_aria = QPlainTextEdit()
        self.edit_aria.setPlaceholderText(_ARIA2_PLACEHOLDER)
        page_aria.addWidget(self.edit_aria, 1)
        holder2 = QVBoxLayout()
        holder2.addLayout(page_aria)
        self.tabs.addTab(_wrap(holder2), "aria2c 命令")

        # 切到 aria2c 页时自动聚焦，粘贴即用
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"color:{WHITE};")
        v.addWidget(self.lbl_status)

        btns = QDialogButtonBox()
        ok = btns.addButton("开始下载", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        btns.addButton("取消", QDialogButtonBox.RejectRole)
        btns.accepted.connect(self._submit)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

    # ---------------------------------------------------------------- 内部

    def _on_tab_changed(self, idx: int) -> None:
        (self.edit, self.edit_aria)[idx].setFocus()

    def _active_text(self) -> str:
        w = self.edit if self.tabs.currentIndex() == 0 else self.edit_aria
        return w.toPlainText().strip()

    def _submit(self) -> None:
        text = self._active_text()
        if not text:
            self.lbl_status.setText("内容为空")
            return

        # 不管在哪个页签，只要内容像 aria2c 命令就按命令解析（容错）
        if self.tabs.currentIndex() == 1 or looks_like_aria2(text):
            items = [i.to_dict() for i in parse_aria2(text)]
            if not items:
                self.lbl_status.setText("没解析出可下载的 http(s) 链接，检查粘贴内容")
                return
            summary = self._summary(items)
            self.lbl_status.setText(f"已解析 {len(items)} 个任务：{summary}")
        else:
            urls = [ln.strip() for ln in text.splitlines()
                    if ln.strip().startswith(("http://", "https://"))]
            if not urls:
                self.lbl_status.setText("没有有效的 http(s) 链接")
                return
            items = []
            for u in urls:
                name = u.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "download.bin"
                items.append({"url": u, "filename": name})

        r = self.api.add_task(items, platform="direct")
        if r is None:
            self.lbl_status.setText(f"提交失败：{self.api.last_error}")
            return
        self.accept()

    @staticmethod
    def _summary(items: list[dict]) -> str:
        parts = []
        for it in items[:3]:
            name = it.get("filename") or "?"
            bits = [name]
            h = it.get("headers") or {}
            if h.get("User-Agent"):
                bits.append(f"UA={h['User-Agent']}")
            if h.get("Referer"):
                bits.append("Ref=✓")
            n_cookie = len(it.get("cookies") or {})
            if n_cookie:
                bits.append(f"Cookie×{n_cookie}")
            if it.get("workers"):
                bits.append(f"{it['workers']}路")
            parts.append(" · ".join(bits))
        more = "" if len(items) <= 3 else f" …共 {len(items)} 个"
        return "；".join(parts) + more


def _wrap(inner: QVBoxLayout):
    """把 layout 装进一个普通 QWidget，方便塞进 QTabWidget。"""
    from PySide6.QtWidgets import QWidget
    w = QWidget()
    w.setLayout(inner)
    return w
