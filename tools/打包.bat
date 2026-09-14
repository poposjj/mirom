@echo off
chcp 65001 >nul
title 打包 mirom.exe  (by poposjj)
rem 本脚本在 tools\ 里, 先切到【项目根目录】再干活。
cd /d "%~dp0.."

echo ============================================================
echo   打包 mirom.exe                        mirom 1.0.0  by poposjj
echo ============================================================
echo.

rem 真正的打包逻辑在 tools\build.py —— 本地和 GitHub Actions 共用同一份,
rem 免得两边参数漂移 (比如某天本地排掉一个 Qt 模块, CI 打出来的包就不一样了)。
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [准备] 安装依赖...
    python -m pip install -r requirements.txt pyinstaller
    if errorlevel 1 ( echo 依赖安装失败 & pause & exit /b 1 )
)

python tools\build.py %*
if errorlevel 1 (
    echo.
    echo [错误] 打包失败, 请把上面的输出发给开发者
    pause
    exit /b 1
)

echo.
dir /b dist\mirom\*.exe dist\mirom\*.txt
pause
