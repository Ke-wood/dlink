"""粘贴任意下载链接：普通 HTTP 直链的入口。

为什么需要：
    扩展只能在「分享页」提取直链。普通 HTTP 链接（软件包、镜像、直链文件）
    没有入口。这个对话框补上。

设计：
    · 一次可粘多条（每行一条）
    · 后端会自己探测 size / Range 支持
    · 不做复杂解析，URL 原样交给后端
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout,
)

from .api import ApiClient
from .theme import GRAY_400, WHITE


class AddLinkDialog(QDialog):
    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加下载链接")
        self.resize(640, 360)
        self.api = api

        v = QVBoxLayout(self)
        tip = QLabel("每行一个链接。支持 http:// 和 https://\n"
                     "下载器会自动探测文件大小与是否支持分段并行。")
        tip.setStyleSheet(f"color:{GRAY_400}; padding:4px;")
        v.addWidget(tip)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("https://example.com/file.zip")
        v.addWidget(self.edit, 1)

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

    def _submit(self) -> None:
        text = self.edit.toPlainText().strip()
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
