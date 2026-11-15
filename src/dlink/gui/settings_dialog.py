"""设置窗口：分平台下载策略 + 全局设置 + 按主机记忆。

用户明确要求：
  · 每种网盘和常规下载链接的设置**分开**
  · 推荐设置和理由要能看见（README 里也写了，这里也要有）
  · 按 host 学习的并发数要**暴露**出来
  · 转存清理**可选**（默认关）
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .api import ApiClient
from .theme import GRAY_300, GRAY_400, GRAY_700, GRAY_900, WHITE

# 平台展示信息（含推荐理由 —— 与 README 一致）
PLATFORM_INFO = {
    "direct": {
        "name": "普通链接",
        "why": "大多数 HTTP 服务器不限总量，但单连接常常跑不满带宽。\n"
               "实测同文件：1 路 4.91 → 4 路 8.19 → 8 路 3.07 MB/s（该站 4 路最优）。\n"
               "不同站点差异极大，建议开启「按主机学习」。",
        "warn": "",
    },
    "baidu": {
        "name": "百度网盘",
        "why": "账号级总带宽封顶，实测恒定 80KB/s 且 40 秒零波动（服务端硬编码）。\n"
               "并发实测：1 路 75 / 3 路 130 / 8 路 9 KB/s —— 越高越差，8 路 6 个 403。\n"
               "BaiduPCS-Go 作者原文：调大线程数会「导致几小时至几天内账号接近 0 速」。",
        "warn": "改大于 1 会触发风控，实测降到 9 KB/s。",
    },
    "weiyun": {
        "name": "腾讯微云",
        "why": "限的是「连接年龄」：单连接约 14MB 后掉到 240B/s，但新连接立刻满速。\n"
               "所以分段并行有效 —— 8 路实测聚合 9.7 MB/s，1.53GB 约 2 分钟。",
        "warn": "",
    },
    "quark": {
        "name": "夸克网盘",
        "why": "二阶段限速：窗口期可到 15-20 MB/s，18:00-23:00 可能被限到约 2 MB/s。\n"
               "6 路是折中。限流期建议手动降到 2 路。",
        "warn": "",
    },
    "uc": {
        "name": "UC 网盘",
        "why": "与夸克同架构（阿里系），推测同样的二阶段限速。\n"
               "⚠️ 尚未实测（缺测试账号），6 路是照夸克给的。",
        "warn": "",
    },
}


class SettingsDialog(QDialog):
    def __init__(self, api: ApiClient, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(760, 620)
        self.api = api

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._build_platforms_tab(), "分平台下载策略")
        tabs.addTab(self._build_global_tab(), "全局")
        tabs.addTab(self._build_hosts_tab(), "按主机记忆")
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox()
        ok = buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._save_all)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load()

    # ---------------- 分平台 ----------------

    def _build_platforms_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        top = QHBoxLayout()
        top.addWidget(QLabel("平台："))
        self.cmb_platform = QComboBox()
        for key, info in PLATFORM_INFO.items():
            self.cmb_platform.addItem(info["name"], key)
        self.cmb_platform.currentIndexChanged.connect(self._load_platform)
        top.addWidget(self.cmb_platform, 1)
        v.addLayout(top)

        # 推荐理由
        self.lbl_why = QLabel()
        self.lbl_why.setWordWrap(True)
        self.lbl_why.setStyleSheet(
            f"background:{GRAY_900}; border:1px solid {GRAY_700};"
            f" padding:12px; border-radius:5px; color:{GRAY_300};"
            " font-family: Consolas, 'Courier New', monospace;")
        v.addWidget(self.lbl_why)

        self.lbl_warn = QLabel()
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setStyleSheet(f"color:{WHITE}; font-weight:600; padding:6px 4px;")
        v.addWidget(self.lbl_warn)

        # 参数
        form = QFormLayout()
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 32)
        self.spin_workers.setMinimumWidth(120)
        self.spin_workers.valueChanged.connect(self._on_workers_changed)
        form.addRow("线程数（同时下载的分段数）：", self.spin_workers)

        self.chk_single = QCheckBox("强制单连接（忽略线程数）")
        form.addRow("", self.chk_single)

        self.spin_seg = QSpinBox()
        self.spin_seg.setRange(1, 256)
        self.spin_seg.setSuffix(" MB")
        form.addRow("分段大小：", self.spin_seg)

        self.spin_retry = QSpinBox()
        self.spin_retry.setRange(1, 20)
        form.addRow("重试次数：", self.spin_retry)

        self.spin_timeout = QDoubleSpinBox()
        self.spin_timeout.setRange(10, 600)
        self.spin_timeout.setDecimals(0)
        self.spin_timeout.setSuffix(" 秒")
        self.spin_timeout.setMinimumWidth(120)
        form.addRow("超时：", self.spin_timeout)

        v.addLayout(form)

        # 操作
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.clicked.connect(self._reset_platform)
        row.addWidget(self.btn_reset)
        v.addLayout(row)

        v.addStretch(1)
        return w

    def _current_platform(self) -> str:
        return self.cmb_platform.currentData()

    def _on_workers_changed(self, val: int) -> None:
        info = PLATFORM_INFO.get(self._current_platform(), {})
        if info.get("warn") and val > 1:
            self.lbl_warn.setText("⚠ " + info["warn"])
        else:
            self.lbl_warn.setText("")

    def _load_platform(self) -> None:
        key = self._current_platform()
        info = PLATFORM_INFO.get(key, {})
        self.lbl_why.setText(info.get("why", ""))

        profs = self.api.profiles()
        p = profs.get(key) or {}
        self.spin_workers.setValue(int(p.get("workers") or 8))
        self.chk_single.setChecked(bool(p.get("force_single")))
        self.spin_seg.setValue(max(1, int((p.get("seg_size") or 33554432) // 1048576)))
        self.spin_retry.setValue(int(p.get("max_retries") or 6))
        self.spin_timeout.setValue(float(p.get("timeout") or 180))
        self._on_workers_changed(self.spin_workers.value())

    def _reset_platform(self) -> None:
        key = self._current_platform()
        if self.api.reset_profile(key) is not None:
            self._load_platform()
            QMessageBox.information(self, "已重置", f"{PLATFORM_INFO[key]['name']} 已恢复默认。")

    # ---------------- 全局 ----------------

    def _build_global_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        g1 = QGroupBox("下载")
        f1 = QFormLayout(g1)

        row = QHBoxLayout()
        self.ed_dir = QLineEdit()
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.clicked.connect(self._pick_dir)
        row.addWidget(self.ed_dir, 1)
        row.addWidget(self.btn_browse)
        holder = QWidget()
        holder.setLayout(row)
        f1.addRow("保存目录：", holder)

        self.spin_concurrent = QSpinBox()
        self.spin_concurrent.setRange(1, 20)
        f1.addRow("同时下载任务数：", self.spin_concurrent)

        self.chk_verify = QCheckBox("下载完成后校验（SHA1/MD5/size）")
        f1.addRow("", self.chk_verify)

        self.chk_open = QCheckBox("完成后打开所在文件夹")
        f1.addRow("", self.chk_open)

        v.addWidget(g1)

        g2 = QGroupBox("网盘")
        f2 = QFormLayout(g2)
        self.chk_cleanup = QCheckBox("下载完成后清理「为取直链而临时转存」的文件")
        f2.addRow("", self.chk_cleanup)
        tip = QLabel("夸克分享文件不能直取直链，需先转存到你的网盘。\n"
                     "开启后会删除这些临时文件（移至回收站）。\n"
                     "默认关闭 —— 避免万一下载失败却把源删了。")
        tip.setStyleSheet(f"color:{GRAY_400}; font-size:12px; padding-left:24px;")
        tip.setWordWrap(True)
        f2.addRow("", tip)
        v.addWidget(g2)

        v.addStretch(1)
        return w

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    # ---------------- 按主机记忆 ----------------

    def _build_hosts_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        tip = QLabel("下载普通链接时，程序会记住每个站点上次有效的并发数。\n"
                     "「只降不升」——成功后保持，只有出现大量连接被拒才下调，避免震荡。")
        tip.setStyleSheet(f"color:{GRAY_400}; padding:6px;")
        tip.setWordWrap(True)
        v.addWidget(tip)

        self.tbl_hosts = QTableWidget(0, 4)
        self.tbl_hosts.setHorizontalHeaderLabels(["站点", "并发数", "失败率", "说明"])
        self.tbl_hosts.verticalHeader().setVisible(False)
        self.tbl_hosts.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl_hosts.setColumnWidth(0, 220)
        v.addWidget(self.tbl_hosts, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_forget = QPushButton("忘记选中")
        btn_forget.clicked.connect(self._forget_host)
        btn_all = QPushButton("全部忘记")
        btn_all.clicked.connect(self._forget_all_hosts)
        row.addWidget(btn_forget)
        row.addWidget(btn_all)
        v.addLayout(row)
        return w

    def _load_hosts(self) -> None:
        hs = self.api.hosts()
        self.tbl_hosts.setRowCount(len(hs))
        for i, (host, e) in enumerate(sorted(hs.items())):
            self.tbl_hosts.setItem(i, 0, QTableWidgetItem(host))
            it1 = QTableWidgetItem(str(e.get("workers", "")))
            it1.setTextAlignment(Qt.AlignCenter)
            self.tbl_hosts.setItem(i, 1, it1)
            ratio = e.get("fail_ratio")
            it2 = QTableWidgetItem(f"{float(ratio) * 100:.0f}%" if ratio is not None else "—")
            it2.setTextAlignment(Qt.AlignCenter)
            self.tbl_hosts.setItem(i, 2, it2)
            self.tbl_hosts.setItem(i, 3, QTableWidgetItem(e.get("reason", "")))

    def _forget_host(self) -> None:
        r = self.tbl_hosts.currentRow()
        if r < 0:
            return
        host = self.tbl_hosts.item(r, 0).text()
        self.api.forget_host(host)
        self._load_hosts()

    def _forget_all_hosts(self) -> None:
        for h in list(self.api.hosts()):
            self.api.forget_host(h)
        self._load_hosts()

    # ---------------- 读写 ----------------

    def _load(self) -> None:
        s = self.api.settings()
        self.ed_dir.setText(s.get("save_dir") or "")
        self.spin_concurrent.setValue(int(s.get("max_concurrent") or 3))
        self.chk_verify.setChecked(bool(s.get("verify", True)))
        self.chk_open.setChecked(bool(s.get("open_folder_when_done")))
        self.chk_cleanup.setChecked(bool(s.get("auto_cleanup_after_download")))
        self._load_platform()
        self._load_hosts()

    def _save_all(self) -> None:
        # 全局
        self.api.save_settings(
            save_dir=self.ed_dir.text().strip(),
            max_concurrent=self.spin_concurrent.value(),
            verify=self.chk_verify.isChecked(),
            open_folder_when_done=self.chk_open.isChecked(),
            auto_cleanup_after_download=self.chk_cleanup.isChecked(),
        )
        # 当前选中的平台
        key = self._current_platform()
        self.api.save_profile(
            key,
            workers=self.spin_workers.value(),
            force_single=self.chk_single.isChecked(),
            seg_size=self.spin_seg.value() * 1024 * 1024,
            max_retries=self.spin_retry.value(),
            timeout=self.spin_timeout.value(),
        )
        self.accept()
