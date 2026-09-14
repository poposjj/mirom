@echo off
chcp 65001 >nul
title 打包 mirom.exe  (by poposjj)
rem 本脚本在 tools\ 里, 先切到【项目根目录】再干活。
rem 不切的话 make_logo.py / mirom_gui.py / assets 全都找不到。
cd /d "%~dp0.."

echo ============================================================
echo   打包 mirom.exe  (PyInstaller onedir + windowed)   by poposjj
echo ============================================================
echo.

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [1/4] 安装 PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 ( echo 安装失败 & pause & exit /b 1 )
) else (
    echo [1/4] PyInstaller 已就绪
)

echo.
echo [2/4] 生成图标资源...
python tools\make_logo.py
if errorlevel 1 ( echo 图标生成失败 & pause & exit /b 1 )

echo.
echo [3/4] 开始打包 (约 1-3 分钟)...
echo.
echo 注意: 不要排除 PySide6.QtOpenGL / QtOpenGLWidgets ——
echo       pyqtgraph 的 PlotCurveItem 会 import 它, 排掉后 EXE 一启动就 ModuleNotFoundError。
echo.
echo 图标与署名:
echo   --icon          把 logo 写进 EXE 资源段 (资源管理器/任务栏/快捷方式都读它)
echo   --version-file  把作者写进 EXE 属性页 (右键属性 - 详细信息能看到 poposjj)
echo   --add-data      把 assets 复制进包, 运行时窗口图标与链接框图标要用
echo.

python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name mirom ^
  --icon assets\logo.ico ^
  --version-file tools\version_info.txt ^
  --add-data "assets;assets" ^
  --hidden-import vdesk ^
  --collect-submodules qfluentwidgets ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtWebEngineQuick ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuickWidgets ^
  --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.Qt3DRender ^
  --exclude-module PySide6.Qt3DAnimation ^
  --exclude-module PySide6.Qt3DExtras ^
  --exclude-module PySide6.Qt3DInput ^
  --exclude-module PySide6.Qt3DLogic ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtMultimediaWidgets ^
  --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization ^
  --exclude-module PySide6.QtBluetooth ^
  --exclude-module PySide6.QtNfc ^
  --exclude-module PySide6.QtPositioning ^
  --exclude-module PySide6.QtSerialPort ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.QtHelp ^
  --exclude-module PySide6.QtRemoteObjects ^
  --exclude-module PySide6.QtScxml ^
  --exclude-module PySide6.QtSensors ^
  --exclude-module PySide6.QtSpatialAudio ^
  --exclude-module PySide6.QtStateMachine ^
  --exclude-module PySide6.QtTextToSpeech ^
  --exclude-module PySide6.QtWebChannel ^
  --exclude-module PySide6.QtWebSockets ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtPdfWidgets ^
  --exclude-module tkinter ^
  --exclude-module matplotlib ^
  --exclude-module PIL ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module numpy.f2py ^
  mirom_gui.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败, 请把上面的输出发给开发者
    pause
    exit /b 1
)

echo.
echo [4/4] 复制随包文件...
rem --clean 会把 dist 整个删掉, 这些文件必须每次重新拷进去,
rem 否则用户拿到的压缩包里没有说明文档。
if exist "docs\使用说明.txt" (
    copy /y "docs\使用说明.txt" "dist\mirom\使用说明.txt" >nul
    echo   使用说明.txt  OK
) else (
    echo   [警告] docs\使用说明.txt 不存在, 跳过
)
if exist "LICENSE" (
    copy /y "LICENSE" "dist\mirom\LICENSE.txt" >nul
    echo   LICENSE.txt   OK
)
rem assets 由 --add-data 负责, 这里只确认它真的进去了 ——
rem 少这一层的话界面会静默退化成"没有图标", 很难发现。
rem PyInstaller 6.x 的 onedir 会把资源放进 _internal\, 不是 exe 同级。
set "ICONOK="
if exist "dist\mirom\_internal\assets\logo.ico" set "ICONOK=1"
if exist "dist\mirom\assets\logo.ico" set "ICONOK=1"
if not defined ICONOK (
    echo   [错误] 打包产物里找不到 logo.ico! 窗口和任务栏会没有图标。
    pause
    exit /b 1
)
echo   assets\logo.ico  OK

echo.
echo ============================================================
echo   打包完成!                       mirom 1.0.0  by poposjj
echo   产物: %cd%\dist\mirom\mirom.exe
echo   分发时请打包整个 dist\mirom 文件夹 (不能只发 exe)
echo ============================================================
echo.
dir /b dist\mirom\*.exe dist\mirom\*.txt
pause
