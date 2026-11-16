"""主窗口：任务表 + 工具条 + 状态栏。

设计依据 design/GUI-INFORMATION-ARCHITECTURE.md：
  · 主窗口唯一，详情用右侧抽屉
  · 8 列：名称 / 进度(合成) / 速度 / 大小 / 剩余时间 / 直链 / 平台 / 状态
  · 进度条 + 百分比 + 状态色画在同一个单元格（省列宽）
  · 速度做 3 秒平滑（百度恒 80KB/s 时不能显示成 0/240/0 锯齿）
  · 「直链剩余」独立成列常驻（与普通下载器最大差别）
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSystemTrayIcon,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .api import ApiClient
from .add_link_dialog import AddLinkDialog
from .settings_dialog import SettingsDialog
from .tray import TrayController
from ..resources import asset_path

# ---- 黑白主题（统一从 theme.py 取）----
from .theme import (  # noqa: E402
    BLACK, GRAY_100, GRAY_200, GRAY_300, GRAY_400, GRAY_500, GRAY_600,
    GRAY_700, GRAY_800, GRAY_850, GRAY_900, GRAY_950, STATE_FILL,
    STATE_TEXT, STATE_TEXT_COLOR, WHITE,
)

# 兼容旧引用
STATE_COLORS = {k: QColor(v) for k, v in STATE_TEXT_COLOR.items()}

# 平台显示名
PLATFORM_NAMES = {
    "direct": "普通链接", "baidu": "百度", "weiyun": "微云",
    "quark": "夸克", "uc": "UC",
}

COLUMNS = ["名称", "进度", "速度", "大小", "剩余时间", "直链", "平台", "状态"]


def fmt_size(n: float) -> str:
    if not n or n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{n:.0f} {units[i]}" if i == 0 or n >= 100 else f"{n:.1f} {units[i]}"


def fmt_speed(bps: float) -> str:
    """★ 关键：速度为 0 时显示 —，不要显示 0 KB/s。

    百度恒 80KB/s，网络抖动会让瞬时值掉到 0。显示 0 会让用户以为断了。
    """
    if not bps or bps <= 0:
        return "—"
    return fmt_size(bps) + "/s"


def fmt_eta(sec: Optional[float]) -> str:
    if sec is None or sec <= 0:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分"
    h, m = divmod(sec // 60, 60)
    if h < 24:
        return f"{h} 时 {m} 分" if m else f"{h} 时"
    return f"{h // 24} 天 {h % 24} 时"


def fmt_ttl(expires_at: Optional[float]) -> str:
    if not expires_at:
        return "未知"
    left = expires_at - time.time()
    if left <= 0:
        return "已过期"
    h = left / 3600
    return f"{h:.1f} 小时" if h >= 1 else f"{left / 60:.0f} 分"


class SpeedSmoother:
    """3 秒滑动平均。

    为什么必须做：实测百度恒定 80KB/s，但 1 秒瞬时采样会在 0~240 之间跳。
    不平滑的话数字像坏掉一样。
    """

    def __init__(self, window: float = 3.0) -> None:
        self.window = window
        self.samples: dict[str, list[tuple[float, float]]] = {}

    def feed(self, task_id: str, bps: float) -> float:
        now = time.time()
        arr = self.samples.setdefault(task_id, [])
        arr.append((now, bps))
        cutoff = now - self.window
        while arr and arr[0][0] < cutoff:
            arr.pop(0)
        if not arr:
            return 0.0
        return sum(v for _, v in arr) / len(arr)

    def forget(self, task_id: str) -> None:
        self.samples.pop(task_id, None)


class ProgressDelegate:
    """进度单元格：进度条 + 百分比 + 状态色合成在一格里。

    参照 qBittorrent 的 progressbarpainter 思路：省一个列宽。
    """

    @staticmethod
    def paint(painter: QPainter, rect, value: float, status: str) -> None:
        """黑白版：轨道用深灰，填充用白/浅灰，百分比文字反色。

        要点：当填充超过一半时，文字翻成黑色压在白条上，保证可读。
        """
        frac = max(0.0, min(1.0, value))
        fill_color = QColor(STATE_FILL.get(status, GRAY_500))

        painter.save()
        painter.setPen(Qt.NoPen)

        text_w = 42
        track = rect.adjusted(5, 9, -(text_w + 8), -9)

        # 轨道
        painter.setBrush(QBrush(QColor(GRAY_800)))
        painter.drawRoundedRect(track, 3, 3)

        # 填充
        if frac > 0:
            fill = type(track)(track)
            fill.setWidth(max(2, int(track.width() * frac)))
            painter.setBrush(QBrush(fill_color))
            painter.drawRoundedRect(fill, 3, 3)

        # 文字画在条的右侧独立区域，不做反色 —— 黑白主题下反色对比不足
        painter.setPen(QColor(GRAY_200))
        right = rect.adjusted(track.width() + 10, 0, 0, 0)
        painter.drawText(right, Qt.AlignVCenter | Qt.AlignLeft,
                         f"{frac * 100:.0f}%")
        painter.restore()


class MainWindow(QMainWindow):
    REFRESH_MS = 800

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DLink 下载器")
        _ico = asset_path("icon256.png")
        if _ico.exists():
            self.setWindowIcon(QIcon(str(_ico)))
        self.resize(1500, 780)

        self.api = ApiClient()
        self.smoother = SpeedSmoother()
        self._last_rows: dict[str, dict] = {}
        self._note_inflight: dict[str, float] = {}

        self._build_ui()
        self._build_menu()
        self._setup_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(self.REFRESH_MS)

        self.refresh()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(8)

        # 工具条
        bar = QHBoxLayout()
        self.btn_add = QPushButton("＋ 添加链接")
        self.btn_add.setObjectName("primary")
        self.btn_pause_all = QPushButton("全部暂停")
        self.btn_resume_all = QPushButton("全部继续")
        self.btn_clear = QPushButton("清除已完成")
        self.btn_open = QPushButton("打开下载目录")
        self.btn_settings = QPushButton("设置")
        for b in (self.btn_add, self.btn_pause_all, self.btn_resume_all,
                  self.btn_clear, self.btn_open, self.btn_settings):
            bar.addWidget(b)
        bar.addStretch(1)
        self.lbl_ext = QLabel("扩展：—")
        bar.addWidget(self.lbl_ext)
        layout.addLayout(bar)

        self.btn_add.clicked.connect(self._add_link)
        self.btn_pause_all.clicked.connect(lambda: self._bulk("pause"))
        self.btn_resume_all.clicked.connect(lambda: self._bulk("resume"))
        self.btn_clear.clicked.connect(self._clear_finished)
        self.btn_open.clicked.connect(self._open_dir)
        self.btn_settings.clicked.connect(self._open_settings)

        # 任务表
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.setSortingEnabled(True)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

        hdr = self.table.horizontalHeader()
        widths = {
            "名称": (280, 180, 900),
            "进度": (140, 120, 190),
            "速度": (100, 80, 140),
            "大小": (160, 130, 230),
            "剩余时间": (95, 80, 130),
            "直链": (95, 80, 130),
            "平台": (85, 70, 120),
            "状态": (85, 70, 120),
        }
        for i, name in enumerate(COLUMNS):
            mn, w, mx = widths[name]
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
            self.table.setColumnWidth(i, w)
            # 数值列右对齐
            if name in ("速度", "大小", "剩余时间", "直链"):
                item = self.table.horizontalHeaderItem(i)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # 所有列固定初始宽度，用户可拖拽调整；不自动 Stretch（会挤掉其他列）
        hdr.setStretchLastSection(True)

        layout.addWidget(self.table, 1)

        # 状态栏
        self.status = QLabel("正在连接后端…")
        self.status.setStyleSheet(f"color:{GRAY_400}; font-size:12px; padding:4px;")
        layout.addWidget(self.status)

        self.setCentralWidget(central)

    def _build_menu(self) -> None:
        m = self.menuBar().addMenu("文件")
        a_open_dir = QAction("打开下载目录", self)
        a_open_dir.triggered.connect(self._open_dir)
        m.addAction(a_open_dir)
        a = QAction("设置", self)
        a.triggered.connect(self._open_settings)
        m.addAction(a)
        m.addSeparator()
        a2 = QAction("退出", self)
        a2.triggered.connect(self._quit_app)
        m.addAction(a2)

        h = self.menuBar().addMenu("帮助")
        a_ext = QAction("导出浏览器扩展…", self)
        a_ext.triggered.connect(self._export_extension)
        h.addAction(a_ext)
        h.addSeparator()
        a3 = QAction("平台推荐并发说明", self)
        a3.triggered.connect(self._show_help)
        h.addAction(a3)
        a4 = QAction("关于 DLink", self)
        a4.triggered.connect(self._show_about)
        h.addAction(a4)

    def _export_extension(self) -> None:
        """把内置的浏览器扩展释放到用户选的目录。"""
        from PySide6.QtWidgets import QFileDialog

        from .extension_export import ExtensionExportError, export_extension

        target = QFileDialog.getExistingDirectory(
            self, "选择导出位置（会创建 dlink-extension 子文件夹）")
        if not target:
            return
        try:
            dest, n = export_extension(Path(target))
        except ExtensionExportError as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {n} 个文件到：\n{dest}\n\n"
            "接下来在浏览器里安装：\n"
            "1. 打开 edge://extensions/ 或 chrome://extensions/\n"
            "2. 开启「开发人员模式」\n"
            "3. 点「加载解压缩的扩展」，选择上面的文件夹")

    def _show_about(self) -> None:
        from dlink import __version__ as ver
        QMessageBox.about(
            self, "关于 DLink",
            f"<b>DLink 下载器</b> v{ver}<br><br>"
            "网盘分享链接 → 直链 → 多线程下载。<br>"
            "浏览器扩展负责取直链，本程序负责下载。<br><br>"
            "<span style='color:#888'>不劫持浏览器原生下载</span>")

    # ---------------- 托盘 ----------------

    def _setup_tray(self) -> None:
        self.tray = TrayController(self)
        self.tray.showWindow.connect(self._restore_from_tray)
        self.tray.quitRequested.connect(self._quit_app)
        self.tray.pauseAllRequested.connect(lambda: self._bulk("pause"))
        self.tray.resumeAllRequested.connect(lambda: self._bulk("resume"))
        self.tray.openFolderRequested.connect(self._open_dir)
        self.tray.settingsRequested.connect(self._open_settings)
        self.tray.show()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        self._really_quit = True
        if hasattr(self, "tray"):
            self.tray.hide()
        self.close()
        QApplication.quit()

    def closeEvent(self, ev) -> None:
        """关闭窗口 = 最小化到托盘（除非用户选了退出）。"""
        if getattr(self, "_really_quit", False):
            ev.accept()
            return
        ev.ignore()
        self.hide()
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "DLink 仍在后台运行",
                "下载不会中断。双击托盘图标可重新打开窗口。",
                QSystemTrayIcon.Information, 3000,
            )

    # ---------------- 数据刷新 ----------------

    def refresh(self) -> None:
        health = self.api.health()
        if not health:
            self.status.setText("⚠ 下载器后端未连接（请先运行 python src/dlink/server.py）")
            self.lbl_ext.setText("扩展：—")
            return

        ext = health.get("extension") or {}
        ago = ext.get("seconds_ago")
        if ext.get("alive"):
            self.lbl_ext.setText(f"扩展：在线")
            self.lbl_ext.setStyleSheet(f"color:{WHITE}; font-size:12px;")
        elif ago:
            self.lbl_ext.setText(f"扩展：{int(ago // 60)} 分钟前")
            self.lbl_ext.setStyleSheet(f"color:{GRAY_500}; font-size:12px;")
        else:
            self.lbl_ext.setText("扩展：从未连接")
            self.lbl_ext.setStyleSheet(f"color:{GRAY_300}; font-size:12px;")

        tasks = self.api.tasks()
        stats = (health.get("queue") or {})

        # 保留选中与排序
        selected = {self._row_id(r) for r in self.table.selectionModel().selectedRows()} \
            if self.table.selectionModel() else set()
        sort_col = self.table.horizontalHeader().sortIndicatorSection()
        sort_ord = self.table.horizontalHeader().sortIndicatorOrder()
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)

        self.table.setRowCount(len(tasks))
        for row, t in enumerate(tasks):
            tid = t.get("id", "")
            raw_speed = float(t.get("speed") or 0)
            sp = self.smoother.feed(tid, raw_speed)
            done = int(t.get("done") or 0)
            total = int(t.get("total") or t.get("size") or 0)
            frac = (done / total) if total else 0.0
            status = t.get("status", "")

            vals = {
                "名称": t.get("name") or "(未命名)",
                "进度": "",   # 由 widget 画
                "速度": fmt_speed(sp) if status == "running" else "—",
            
                "大小": f"{fmt_size(done)} / {fmt_size(total)}",
                "剩余时间": fmt_eta(t.get("eta")),
                "直链": fmt_ttl(t.get("expires_at")) if status in ("running", "queued", "paused", "need_refresh") else "—",
                "平台": PLATFORM_NAMES.get(t.get("platform"), t.get("platform") or "—"),
                "状态": STATE_TEXT.get(status, status),
            }

            for col, name in enumerate(COLUMNS):
                if name == "进度":
                    w = self.table.cellWidget(row, col)
                    if w is None:
                        w = _ProgressCell()
                        self.table.setCellWidget(row, col, w)
                    w.set_value(frac, status)
                    continue
                it = QTableWidgetItem(vals[name])
                if name in ("速度", "大小", "剩余时间", "直链"):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if name == "状态":
                    it.setForeground(QColor(STATE_TEXT_COLOR.get(status, GRAY_400)))
                if name == "名称":
                    it.setForeground(QColor(WHITE))
                it.setData(Qt.UserRole, tid)
                if name == "名称" and t.get("error"):
                    it.setToolTip(t["error"])
                self.table.setItem(row, col, it)

        self.table.setSortingEnabled(sorting)
        if sorting and sort_col >= 0:
            self.table.sortItems(sort_col, sort_ord)

        # 恢复选中
        for r in range(self.table.rowCount()):
            if self._row_id(r) in selected:
                self.table.selectRow(r)

        if hasattr(self, "tray"):
            self.tray.update_state(tasks, stats)

        busy = stats.get("active", 0)
        pend = stats.get("pending", 0)
        total_speed = sum(self.smoother.feed(t.get("id", ""), float(t.get("speed") or 0))
                          for t in tasks if t.get("status") == "running")
        self.status.setText(
            f"后端 :{self.api.port}   ·   {len(tasks)} 个任务   ·   "
            f"下载中 {busy} / 等待 {pend}   ·   "
            f"总速度 {fmt_speed(total_speed)}   ·   保存到 {self.api.settings().get('save_dir', '?')}"
        )

    def _row_id(self, row: int) -> str:
        it = self.table.item(row, 0)
        return (it.data(Qt.UserRole) if it else "") or ""

    # ---------------- 操作 ----------------

    def _add_link(self) -> None:
        dlg = AddLinkDialog(self.api, self)
        if dlg.exec():
            self.refresh()

    def _bulk(self, action: str) -> None:
        for t in self.api.tasks():
            st = t.get("status")
            if action == "pause" and st in ("running", "queued"):
                self.api.task_action(t["id"], "pause")
            elif action == "resume" and st == "paused":
                self.api.task_action(t["id"], "resume")
        self.refresh()

    def _clear_finished(self) -> None:
        n = self.api.clear_finished()
        self.status.setText(f"已清除 {n} 个已完成/失败任务")
        self.refresh()

    def _open_dir(self) -> None:
        import os
        d = self.api.settings().get("save_dir")
        if d and os.path.isdir(d):
            os.startfile(d)  # type: ignore[attr-defined]
        else:
            QMessageBox.information(self, "提示", f"目录不存在：{d}")

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.api, self)
        dlg.exec()
        self.refresh()

    def _show_help(self) -> None:
        QMessageBox.information(
            self, "平台推荐并发",
            "各平台限速机制不同，推荐值来自实测：\n\n"
            "· 普通链接 8 路：多数服务器不限总量，但单连接常跑不满带宽\n"
            "  （实测 OVH: 1路4.91 → 4路8.19 → 8路3.07 MB/s，4路最优）\n"
            "· 微云 8 路：限的是「连接年龄」，新连接满速，并行有效\n"
            "· 百度 1 路：账号级总带宽封顶 80KB/s，并发反而触发风控掉到 9KB/s\n"
            "· 夸克 6 路：二阶段限速，18:00-23:00 可能限流，建议降到 2\n\n"
            "「按主机学习」会记住每个站点上次有效的并发数。"
        )

    def _context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        tid = self._row_id(row)
        if not tid:
            return
        t = next((x for x in self.api.tasks() if x.get("id") == tid), None)
        if not t:
            return

        menu = QMenu(self)
        st = t.get("status")
        if st in ("running", "queued"):
            menu.addAction("暂停", lambda: (self.api.task_action(tid, "pause"), self.refresh()))
        if st == "paused":
            menu.addAction("继续", lambda: (self.api.task_action(tid, "resume"), self.refresh()))
        menu.addAction("取消", lambda: (self.api.task_action(tid, "cancel"), self.refresh()))
        menu.addSeparator()
        menu.addAction("移除", lambda: (self.api.task_action(tid, "remove"), self.refresh()))
        menu.exec(self.table.viewport().mapToGlobal(pos))


class _ProgressCell(QWidget):
    """进度单元格：进度条 + 百分比合成绘制。"""

    def __init__(self) -> None:
        super().__init__()
        self._v = 0.0
        self._status = ""

    def set_value(self, v: float, status: str) -> None:
        if abs(v - self._v) > 0.001 or status != self._status:
            self._v = v
            self._status = status
            self.update()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        ProgressDelegate.paint(p, self.rect(), self._v, self._status)
        p.end()
