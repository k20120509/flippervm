# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec:打包 FlipperVM 为单文件 Windows exe.
版本号单一事实源: flipper_vm/_version.py
注意:
  - 本文件被 PyInstaller exec() 执行,默认 __file__ 不可用,
    必须使用 PyInstaller 注入的 SPECPATH 全局变量。
"""
from PyInstaller.utils.hooks import collect_all
import os
import sys

# ============ 版本号:从 _version.py 读取,同时生成 VersionInfo ============
sys.path.insert(0, SPECPATH)
from flipper_vm._version import __version__, APP_NAME

_major, _minor, _patch = (int(x) for x in __version__.split('.'))

# 生成 Windows 版本资源(filevers/prodvers + CompanyName/ProductName/FileDescription)
_version_info_content = f"""
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({_major}, {_minor}, {_patch}, 0),
    prodvers=({_major}, {_minor}, {_patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'k20120509'),
        StringStruct('FileDescription', 'FlipperVM - Flipper Zero 主板级虚拟机'),
        StringStruct('FileVersion', '{__version__}'),
        StringStruct('InternalName', 'FlipperVM'),
        StringStruct('LegalCopyright', 'MIT License. (c) 2026 k20120509'),
        StringStruct('OriginalFilename', 'FlipperVM.exe'),
        StringStruct('ProductName', 'FlipperVM'),
        StringStruct('ProductVersion', '{__version__}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
_version_info_path = os.path.join(SPECPATH, 'build_version_info.txt')
with open(_version_info_path, 'w', encoding='utf-8') as f:
    f.write(_version_info_content)

# ============ 收集依赖 ============
datas = []
binaries = []
hiddenimports = []

for pkg in ('PySide6', 'unicorn'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += ['unicorn.lib', 'unicorn.unicorn', 'flipper_vm._version']

# ============ 打包固件文件 ============
# 把 firmware_files 目录打包进 exe,这样 Windows 用户无需额外下载固件
_firmware_dir = os.path.join(SPECPATH, 'firmware_files')
if os.path.isdir(_firmware_dir):
    datas += [(_firmware_dir, 'firmware_files')]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'unittest', 'capstone', 'keystone'],
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
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_info_path,
)
