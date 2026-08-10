# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec:打包 FlipperVM 为单文件 Windows exe."""
from PyInstaller.utils.hooks import collect_all

# 收集 PySide6 与 unicorn 的全部数据/二进制/隐藏导入
datas = []
binaries = []
hiddenimports = []

for pkg in ('PySide6', 'unicorn'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# unicorn 的 native 库必须确保被收集
hiddenimports += ['unicorn.lib', 'unicorn.unicorn']

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'unittest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FlipperVM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # 保留控制台以便看 UART 输出与异常
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
