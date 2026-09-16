# -*- coding: utf-8 -*-
"""
一键打包 mirom.exe。

★ 为什么单独抽成脚本, 而不是把命令写在 .bat 里:
  本地双击打包 和 GitHub Actions 自动打包 用的是【同一套参数】。
  写在 .bat 里的话 CI 要么去调用 .bat (末尾有 pause, 会把 runner 挂住),
  要么把 40 多个 --exclude-module 再抄一遍 —— 抄一遍就一定会漂移:
  某天在本地排掉了一个模块, CI 打出来的包就不一样了。

用法:
    python tools/build.py                 # 完整打包
    python tools/build.py --skip-logo     # 跳过图标生成 (图标没改时省几秒)
    python tools/build.py --zip           # 打完再压一个 zip

by poposjj
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _console import force_utf8      # noqa: E402

# ⚠ 必须在任何 print 之前。英文版 Windows 的控制台是 cp1252, 编不出中文,
#   不处理的话脚本第一句中文就 UnicodeEncodeError 崩掉 (CI 上就是这么炸的)。
force_utf8()

VERSION = "1.1.0"

# 排掉用不到的 Qt 模块 —— 不排的话 dist 会从 156MB 涨到 600MB+。
# ⚠ 绝对不要排 PySide6.QtOpenGL / QtOpenGLWidgets: pyqtgraph 的 PlotCurveItem
#   会 import 它, 排掉之后 EXE 一启动就 ModuleNotFoundError (实测踩过)。
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSpatialAudio", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "tkinter", "matplotlib", "PIL", "scipy", "pandas", "numpy.f2py",
]


def run(cmd, **kw):
    print("  $ %s" % " ".join(cmd[:4] + (["..."] if len(cmd) > 4 else [])))
    return subprocess.run(cmd, cwd=ROOT, **kw)


def main():
    args = sys.argv[1:]
    t0 = time.time()

    print("=" * 62)
    print("  打包 mirom.exe  v%s  by poposjj" % VERSION)
    print("=" * 62)

    # 1) 图标
    if "--skip-logo" not in args:
        print("\n[1/4] 生成图标资源...")
        r = run([sys.executable, os.path.join("tools", "make_logo.py")])
        if r.returncode != 0:
            raise SystemExit("图标生成失败")
    else:
        print("\n[1/4] 跳过图标生成")

    # 2) 清掉旧产物 (--clean 也会清 build, 但 dist 得自己删, 免得留下上次的残留文件)
    print("\n[2/4] 清理旧产物...")
    for d in ("build", "dist"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            print("  删除 %s" % d)

    # 3) PyInstaller
    print("\n[3/4] PyInstaller 打包中 (约 1-3 分钟)...")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--windowed", "--name", "mirom",
           "--icon", os.path.join("assets", "logo.ico"),
           "--version-file", os.path.join("tools", "version_info.txt"),
           "--add-data", "assets;assets",
           "--hidden-import", "vdesk",
           "--collect-submodules", "qfluentwidgets"]
    for e in EXCLUDES:
        cmd += ["--exclude-module", e]
    cmd.append("mirom_gui.py")

    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("打包失败")

    # 4) 随包文件 + 验收
    print("\n[4/4] 收尾...")
    dist = os.path.join(ROOT, "dist", "mirom")
    doc = os.path.join(ROOT, "docs", "使用说明.txt")
    if os.path.exists(doc):
        shutil.copy2(doc, os.path.join(dist, "使用说明.txt"))
        print("  使用说明.txt  OK")
    lic = os.path.join(ROOT, "LICENSE")
    if os.path.exists(lic):
        shutil.copy2(lic, os.path.join(dist, "LICENSE.txt"))
        print("  LICENSE.txt   OK")

    # 自检: 少一层校验的话, 界面会静默退化成"没有图标", 极难发现
    exe = os.path.join(dist, "mirom.exe")
    if not os.path.exists(exe):
        raise SystemExit("没有生成 mirom.exe")
    icon_ok = any(os.path.exists(os.path.join(dist, sub, "assets", "logo.ico"))
                  for sub in ("", "_internal"))
    if not icon_ok:
        raise SystemExit("dist 里找不到 assets\\logo.ico —— 窗口和任务栏会没有图标")
    # 0 字节资源也是坑 (实测踩过: 生成过程被打断留下空的 logo_16.png)
    adir = None
    for sub in ("_internal", ""):
        p = os.path.join(dist, sub, "assets")
        if os.path.isdir(p):
            adir = p
            break
    if adir:
        empty = [f for f in os.listdir(adir) if os.path.getsize(os.path.join(adir, f)) == 0]
        if empty:
            raise SystemExit("assets 里有 0 字节文件: %s" % ", ".join(empty))
    print("  图标资源     OK")
    print("  大小         %.1f MB" % (sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _d, fs in os.walk(dist) for f in fs) / 1048576.0))

    # 5) 可选 zip
    if "--zip" in args:
        z = os.path.join(ROOT, "dist", "mirom-%s-win64.zip" % VERSION)
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for dp, _d, fs in os.walk(dist):
                for f in fs:
                    fp = os.path.join(dp, f)
                    zf.write(fp, os.path.join("mirom", os.path.relpath(fp, dist)))
        print("  zip          %s (%.1f MB)" % (z, os.path.getsize(z) / 1048576.0))

    print("\n" + "=" * 62)
    print("  打包完成!   %.0fs" % (time.time() - t0))
    print("  产物: %s" % exe)
    print("  分发时请打包整个 dist\\mirom 文件夹 (不能只发 exe)")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
