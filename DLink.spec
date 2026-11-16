# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物：dist/DLink.exe（单文件，约 60-90MB）

为什么要写成一个 spec 文件而不是纯命令行：
    · 需要精确控制 hiddenimports（dlink 的子模块是动态 import 的）
    · 需要把 assets 图标打进去
    · 需要排除一堆用不到的 Qt 模块（体积能省 40MB+）
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# 动态 import 的模块必须显式列出，否则运行时 ImportError
hidden = (
    collect_submodules('dlink')
    + [
        'dlink.server',
        'dlink.gui.__main__',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ]
)

# 资源文件：GUI 图标 + 浏览器扩展
# 扩展也打包进去，用户可在「帮助 → 导出浏览器扩展」一键释放，
# 这样 exe 是真正自包含的（不用另外找扩展文件）。
datas = [
    ('src/dlink/gui/assets', 'dlink/gui/assets'),
    ('extension', 'extension'),
]

# 排除用不到的 Qt 模块（大幅减小体积）
excludes = [
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D',
    'PySide6.QtQuickWidgets', 'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets', 'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
    'PySide6.QtWebChannel', 'PySide6.QtWebSockets', 'PySide6.QtBluetooth',
    'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtNfc',
    'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtTest',
    'PySide6.QtSql', 'PySide6.QtCharts', 'PySide6.QtDataVisualization',
    'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtSerialPort',
    'PySide6.QtRemoteObjects', 'PySide6.QtScxml', 'PySide6.QtStateMachine',
    'PySide6.QtSvg', 'PySide6.QtSvgWidgets', 'PySide6.QtPdf',
    'PySide6.QtPdfWidgets', 'PySide6.QtLinguist',
    'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
    # 测试/开发工具
    'pytest', '_pytest', 'tkinter', 'unittest',
    'numpy', 'pandas', 'matplotlib', 'PIL',
]

a = Analysis(
    ['launch.py'],
    pathex=['src'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='DLink',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/dlink/gui/assets/icon256.png',
)
