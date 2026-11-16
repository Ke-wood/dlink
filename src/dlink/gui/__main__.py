"""DLink GUI 入口。

用法：
    python -m dlink.gui

设计：后端与 GUI 解耦，GUI 只通过 HTTP API 通信。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    from PySide6.QtGui import QFont, QIcon
    from PySide6.QtWidgets import QApplication

    from .main_window import MainWindow
    from .theme import STYLESHEET

    app = QApplication(sys.argv)
    app.setApplicationName("DLink")
    app.setStyle("Fusion")          # Fusion 才能让 QSS 完全生效
    app.setStyleSheet(STYLESHEET)

    # 应用图标（任务栏/Alt-Tab）
    from ..resources import asset_path
    ico = asset_path("icon256.png")
    if ico.exists():
        app.setWindowIcon(QIcon(str(ico)))

    # 字体：优先系统 UI 字体，等宽用于数字列
    f = QFont("Microsoft YaHei UI", 9)
    f.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(f)

    # 关闭最后一个窗口不退出（托盘常驻）
    app.setQuitOnLastWindowClosed(False)

    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
