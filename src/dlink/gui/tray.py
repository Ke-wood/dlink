"""系统托盘。

为什么需要：
    「过夜下载」是这个工具的典型场景。用户不想让窗口一直占着
    屏幕，但需要随时知道「还在动吗、还要多久」。

设计要点：
    · 图标随状态变化（空闲/下载中/有失败）
    · 悬停提示显示汇总（任务数、总速度）
    · 下载中在图标上画进度弧（不用打开窗口就能看进度）
    · 双击显示主窗口；右键菜单常用操作
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .theme import GRAY_400, GRAY_500, GRAY_600, GRAY_700, WHITE

ASSETS = Path(__file__).parent / "assets"


def _base_icon(size: int = 32) -> QPixmap:
    """基础图标：黑白下载箭头（复用生成的 png）。"""
    p = ASSETS / f"icon{size}.png"
    if not p.exists():
        p = ASSETS / "icon32.png"
    pm = QPixmap(str(p))
    if pm.isNull():
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
    return pm


def make_state_icon(state: str, progress: float = 0.0, size: int = 32) -> QIcon:
    """按状态生成托盘图标。

    state:
        idle    —— 原图（灰）
        active  —— 画进度弧
        error   —— 加一个红点？（黑白主题下用白边空心圆 + 感叹）
        done    —— 加对勾
    """
    pm = _base_icon(size).scaled(size, size, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)

    p = QPainter(canvas)
    p.setRenderHint(QPainter.Antialiasing)
    p.drawPixmap(0, 0, pm)

    if state == "idle":
        # 变灰：叠一层半透明黑
        p.setOpacity(0.45)
        p.fillRect(canvas.rect(), QColor(0, 0, 0))
        p.setOpacity(1.0)

    elif state == "active" and progress > 0:
        # 外圈进度弧
        pen = QPen(QColor(WHITE), max(2, size // 14))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        margin = max(2, size // 16)
        rect = canvas.rect().adjusted(margin, margin, -margin, -margin)
        span = int(-360 * 16 * max(0.0, min(1.0, progress)))
        p.drawArc(rect, 90 * 16, span)

    elif state == "error":
        # 右下角白点
        d = max(6, size // 4)
        p.setBrush(QColor(GRAY_400))
        p.setPen(QPen(QColor(0, 0, 0), 1))
        p.drawEllipse(size - d - 1, size - d - 1, d, d)

    elif state == "done":
        # 右下角对勾
        pen = QPen(QColor(WHITE), max(2, size // 12))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        x0, y0 = size * 0.62, size * 0.58
        x1, y1 = size * 0.74, size * 0.70
        x2, y2 = size * 0.94, size * 0.42
        p.drawLine(int(x0), int(y0), int(x1), int(y1))
        p.drawLine(int(x1), int(y1), int(x2), int(y2))

    p.end()
    return QIcon(canvas)


class TrayController(QSystemTrayIcon):
    """托盘控制器。由 MainWindow 创建并持有。"""

    showWindow = Signal()
    quitRequested = Signal()
    pauseAllRequested = Signal()
    resumeAllRequested = Signal()
    openFolderRequested = Signal()
    settingsRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._progress = 0.0
        self._last_tip = ""

        self._menu = QMenu()
        self._build_menu()
        self.setContextMenu(self._menu)

        self.setIcon(make_state_icon("idle"))
        self.setToolTip("DLink 下载器")
        self.activated.connect(self._on_activated)

    # ---------- 菜单 ----------

    def _build_menu(self) -> None:
        a_show = QAction("显示主窗口", self)
        a_show.triggered.connect(self.showWindow.emit)
        self._menu.addAction(a_show)
        self._menu.addSeparator()

        a_pause = QAction("全部暂停", self)
        a_pause.triggered.connect(self.pauseAllRequested.emit)
        self._menu.addAction(a_pause)

        a_resume = QAction("全部继续", self)
        a_resume.triggered.connect(self.resumeAllRequested.emit)
        self._menu.addAction(a_resume)

        a_folder = QAction("打开下载目录", self)
        a_folder.triggered.connect(self.openFolderRequested.emit)
        self._menu.addAction(a_folder)
        self._menu.addSeparator()

        a_settings = QAction("设置", self)
        a_settings.triggered.connect(self.settingsRequested.emit)
        self._menu.addAction(a_settings)

        self._menu.addSeparator()
        a_quit = QAction("退出", self)
        a_quit.triggered.connect(self.quitRequested.emit)
        self._menu.addAction(a_quit)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.showWindow.emit()

    # ---------- 状态更新 ----------

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        if not bps or bps <= 0:
            return "—"
        units = ["B/s", "KB/s", "MB/s", "GB/s"]
        i = 0
        v = bps
        while v >= 1024 and i < len(units) - 1:
            v /= 1024
            i += 1
        return f"{v:.1f} {units[i]}"

    def update_state(self, tasks: list[dict], queue_stats: Optional[dict] = None) -> None:
        """根据任务列表更新图标与提示。每 1~2 秒调用一次即可。"""
        running = [t for t in tasks if t.get("status") == "running"]
        need_refresh = [t for t in tasks if t.get("status") == "need_refresh"]
        failed = [t for t in tasks if t.get("status") == "failed"]

        total_speed = sum(float(t.get("speed") or 0) for t in running)

        if running:
            state = "active"
            # 进度取所有运行任务的平均
            progs = []
            for t in running:
                tot = int(t.get("total") or 0)
                if tot > 0:
                    progs.append(int(t.get("done") or 0) / tot)
            self._progress = sum(progs) / len(progs) if progs else 0.0
        elif need_refresh:
            state = "error"
            self._progress = 0.0
        elif failed:
            state = "error"
            self._progress = 0.0
        elif any(t.get("status") == "done" for t in tasks):
            state = "done"
            self._progress = 0.0
        else:
            state = "idle"
            self._progress = 0.0

        if state != self._state or abs(self._progress - getattr(self, "_last_prog", -1)) > 0.02:
            self.setIcon(make_state_icon(state, self._progress))
            self._state = state
            self._last_prog = self._progress

        # 提示文字
        lines = []
        if running:
            lines.append(f"下载中 {len(running)} 个 · {self._fmt_speed(total_speed)}")
            if self._progress > 0:
                lines.append(f"平均进度 {self._progress * 100:.0f}%")
        if need_refresh:
            lines.append(f"等待续链 {len(need_refresh)} 个（请打开浏览器）")
        if failed:
            lines.append(f"失败 {len(failed)} 个")
        if not lines:
            lines.append("DLink · 空闲")
        tip = "\n".join(lines)

        if tip != self._last_tip:
            self.setToolTip(tip)
            self._last_tip = tip
