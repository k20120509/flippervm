@echo off
REM ============================================================
REM  FlipperVM Windows 构建脚本
REM  在本机 Windows 上运行此脚本,生成单文件 FlipperVM.exe
REM  版本号单一事实源: flipper_vm\_version.py
REM ============================================================
setlocal
set PYTHON=python
where %PYTHON% >nul 2>&1
if errorlevel 1 (
    echo [error] 未找到 python,请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

REM 读取版本号
for /f "tokens=2 delims= " %%v in ('%PYTHON% -c "from flipper_vm._version import __version__; print(__version__)"') do set APP_VER=%%v
echo ============================================
echo   FlipperVM 构建 — 版本 %APP_VER%
echo ============================================

echo [1/3] 安装依赖...
%PYTHON% -m pip install --upgrade pip
%PYTHON% -m pip install -r requirements.txt
%PYTHON% -m pip install pyinstaller

echo [2/3] 清理旧构建...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist build_version_info.txt del /q build_version_info.txt

echo [3/3] 使用 PyInstaller 打包...
%PYTHON% -m PyInstaller --noconfirm --clean flippervm.spec

if exist dist\FlipperVM.exe (
    echo.
    echo ============================================
    echo  构建成功: dist\FlipperVM.exe
    echo  版本: %APP_VER%
    echo  直接双击运行即可
    echo ============================================
) else (
    echo [error] 构建失败,请查看上方日志
    pause
    exit /b 1
)
pause
