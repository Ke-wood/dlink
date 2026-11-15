"""统一的黑白主题。

设计原则：
    · 纯黑背景 + 白/灰前景，不用任何彩色
    · 状态靠**明度**与**文字**区分，不靠颜色
    · 进度条用白色填充，完成态用更亮的白
"""

from __future__ import annotations

# ---- 基础色板（黑 → 白，中间 8 级灰）----
BLACK = "#000000"
GRAY_950 = "#0a0a0a"
GRAY_900 = "#111111"
GRAY_850 = "#171717"
GRAY_800 = "#1f1f1f"
GRAY_700 = "#2e2e2e"
GRAY_600 = "#444444"
GRAY_500 = "#666666"
GRAY_400 = "#8a8a8a"
GRAY_300 = "#b0b0b0"
GRAY_200 = "#d4d4d4"
GRAY_100 = "#eeeeee"
WHITE = "#ffffff"

# 状态用明度区分（不用颜色）
STATE_FILL = {
    "running": WHITE,        # 下载中：纯白，最醒目
    "queued": GRAY_500,      # 等待：中灰
    "paused": GRAY_300,      # 暂停：浅灰
    "done": GRAY_200,        # 完成：淡白
    "failed": GRAY_600,      # 失败：深灰（文字说清楚）
    "canceled": GRAY_600,
    "need_refresh": GRAY_400,   # 等待续链：中浅灰，区别于失败的深灰
}

STATE_TEXT = {
    "running": "下载中",
    "queued": "等待",
    "paused": "已暂停",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
    "need_refresh": "等待续链",
}

STATE_TEXT_COLOR = {
    "running": WHITE,
    "queued": GRAY_400,
    "paused": GRAY_300,
    "done": GRAY_200,
    "failed": GRAY_300,
    "canceled": GRAY_500,
    "need_refresh": GRAY_300,
}

STYLESHEET = f"""
QMainWindow, QDialog, QWidget {{
    background: {BLACK};
    color: {GRAY_200};
    font-size: 13px;
}}
QMenuBar {{
    background: {GRAY_950};
    color: {GRAY_300};
    border-bottom: 1px solid {GRAY_700};
}}
QMenuBar::item:selected {{ background: {GRAY_700}; color: {WHITE}; }}
QMenu {{
    background: {GRAY_900};
    color: {GRAY_200};
    border: 1px solid {GRAY_700};
}}
QMenu::item:selected {{ background: {GRAY_700}; color: {WHITE}; }}
QMenu::separator {{ height:1px; background:{GRAY_700}; margin:4px 8px; }}

QTableWidget {{
    background: {GRAY_950};
    alternate-background-color: {GRAY_900};
    color: {GRAY_200};
    gridline-color: {GRAY_800};
    border: 1px solid {GRAY_700};
    selection-background-color: {GRAY_800};
    selection-color: {WHITE};
}}
QTableWidget::item {{ padding: 4px 8px; }}
QHeaderView::section {{
    background: {GRAY_850};
    color: {GRAY_400};
    border: 0;
    border-bottom: 1px solid {GRAY_700};
    border-right: 1px solid {GRAY_800};
    padding: 7px 8px;
    font-weight: 500;
}}
QTableCornerButton::section {{ background: {GRAY_850}; border: 0; }}

QPushButton {{
    background: {GRAY_700};
    color: {GRAY_100};
    border: 1px solid {GRAY_600};
    border-radius: 4px;
    padding: 7px 16px;
}}
QPushButton:hover {{ background: {GRAY_600}; color: {WHITE}; }}
QPushButton:pressed {{ background: {GRAY_500}; }}
QPushButton:disabled {{ background: {GRAY_850}; color: {GRAY_500};
                        border-color: {GRAY_800}; }}

/* 主操作按钮：反白（白底黑字），黑白主题里最醒目 */
QPushButton#primary {{
    background: {WHITE};
    color: {BLACK};
    border: 1px solid {WHITE};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {GRAY_100}; }}
QPushButton#primary:pressed {{ background: {GRAY_200}; }}

QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {GRAY_900};
    border: 1px solid {GRAY_600};
    border-radius: 3px;
    padding: 6px;
    color: {GRAY_100};
    selection-background-color: {GRAY_600};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {GRAY_400}; }}
QComboBox::drop-down {{ border: 0; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {GRAY_900};
    color: {GRAY_200};
    selection-background-color: {GRAY_700};
    border: 1px solid {GRAY_700};
}}

QGroupBox {{
    border: 1px solid {GRAY_700};
    border-radius: 5px;
    margin-top: 14px;
    padding-top: 12px;
    color: {GRAY_300};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {GRAY_400};
}}

QTabWidget::pane {{ border: 1px solid {GRAY_700}; top: -1px; }}
QTabBar::tab {{
    background: {GRAY_950};
    color: {GRAY_500};
    padding: 8px 18px;
    border: 1px solid {GRAY_800};
    border-bottom: 0;
}}
QTabBar::tab:selected {{
    background: {GRAY_900};
    color: {WHITE};
    border-color: {GRAY_600};
}}
QTabBar::tab:hover:!selected {{ color: {GRAY_300}; }}

QCheckBox {{ color: {GRAY_200}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {GRAY_500};
    border-radius: 3px;
    background: {GRAY_900};
}}
QCheckBox::indicator:checked {{
    background: {WHITE};
    border-color: {WHITE};
}}
QCheckBox::indicator:hover {{ border-color: {GRAY_300}; }}

QLabel {{ color: {GRAY_300}; }}

QScrollBar:vertical {{
    background: {GRAY_950}; width: 11px; border: 0;
}}
QScrollBar::handle:vertical {{
    background: {GRAY_600}; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {GRAY_500}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{
    background: {GRAY_950}; height: 11px; border: 0;
}}
QScrollBar::handle:horizontal {{
    background: {GRAY_600}; border-radius: 5px; min-width: 28px;
}}

QToolTip {{
    background: {GRAY_800};
    color: {GRAY_100};
    border: 1px solid {GRAY_600};
    padding: 5px;
}}
"""
