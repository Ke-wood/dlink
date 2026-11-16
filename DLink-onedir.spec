# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（**目录版 / onedir**）。

产物：dist/DLink-onedir/DLink.exe + 一堆 dll/pyd

为什么要另出一个目录版：
    单文件版（DLink.spec）启动时要先把自己解压到 %TEMP%\\_MEIxxxxxx。
    如果这个解压失败，PyInstaller 的引导程序会直接弹
        「could not create temporary directory」
    然后退出 —— 此时 Python 代码一行都还没跑，日志、异常处理全都没有，
    用户只能看到一个莫名其妙的报错。实测原因可能是 TEMP 不可写、
    杀软锁住临时目录、企业策略限制等，**和程序本身无关**。

    目录版不解压任何东西，双击直接跑，天然没有这个问题；启动也快 2-3 秒。
    代价是产物是一个文件夹（用 zip 分发，解压即用）。

构建：
    python -m PyInstaller DLink-onedir.spec --noconfirm --clean
    python scripts/pack_exe_onedir.py        # 打成 zip
"""

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

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

datas = [
    ('src/dlink/gui/assets', 'dlink/gui/assets'),
    ('extension', 'extension'),
]

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
    [],
    exclude_binaries=True,      # ★ 关键：二进制不塞进 exe，交给 COLLECT
    name='DLink',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='src/dlink/gui/assets/icon256.png',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DLink-onedir',
)
