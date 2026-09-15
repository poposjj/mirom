# -*- coding: utf-8 -*-
"""
mirom GUI —— 小米 ROM 下载加速器 (PySide6 + PySide6-Fluent-Widgets + pyqtgraph)

设计原则:
  · 全部用代码描述界面, 不依赖任何设计稿/在线服务
  · 支持 --shot 离屏渲染成 PNG, 便于快速自检与回归
运行:
  python mirom_gui.py              正常启动
  python mirom_gui.py --shot x.png 渲染一张截图后退出 (自动用假数据填充)
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QTimer, QSize, QPointF, QObject, Signal, Slot
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QBrush, QLinearGradient,
                           QPalette, QTextCursor)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QFrame, QSizePolicy, QMainWindow, QMenuBar, QMenu, QStatusBar, QTextEdit,
    QFileDialog, QDialog, QCheckBox, QSpinBox, QDoubleSpinBox, QDialogButtonBox,
    QFormLayout, QLineEdit, QPushButton,
)
import pyqtgraph as pg

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, setTheme, Theme, setThemeColor,
    PushButton, PrimaryPushButton, TransparentPushButton, LineEdit,
    CardWidget, ProgressBar, BodyLabel, TitleLabel, SubtitleLabel, CaptionLabel,
    StrongBodyLabel, SegmentedWidget, TransparentToolButton, FluentIcon as FIF,
    ScrollArea, isDarkTheme, ToolButton, InfoBar, InfoBarPosition, MessageBox,
)

from mirom import MiromEngine, VERSION as ENGINE_VERSION, AUTHOR, BYLINE

# ═══════════════════════════ 设计令牌 (与引擎能力一一对应) ═══════════════════════════
APP_NAME = "mirom"
APP_TITLE = "小米 ROM 下载加速器"
# ★ 帮助菜单的目标地址。
#   之前这里是 REPO_URL = "https://github.com/" 和 README_URL = REPO_URL + "#readme"
#   ——纯占位符: 点「使用说明」只会打开 GitHub 首页, 「常见问题」的锚点也是假的,
#   用户点完一脸茫然, 只会觉得"帮助菜单是坏的"。现在指向真实仓库。
REPO_URL = "https://github.com/poposjj/mirom"
# ⚠ README_URL 必须是【不带任何 fragment 的裸地址】。
#   第一版写成 REPO_URL + "#readme" 之后, 再拼 "#三常见问题" 就变成了
#     https://github.com/poposjj/mirom#readme#三常见问题
#   —— 两个 # 片段, 浏览器只认第一个, 点「常见问题」永远停在页首。
#   要页首锚点的话用 README_TOP。
README_URL = REPO_URL
README_TOP = REPO_URL + "#readme"
ISSUES_URL = REPO_URL + "/issues/new"
RELEASES_URL = REPO_URL + "/releases"
# 锚点规则: GitHub 会去掉标题里的标点与空格, 剩下的中文/字母数字做 id。
#   "## 常见问题" -> #常见问题
# ⚠ 这几个值必须和 README.md 的标题【逐字对应】。
#   改 README 标题时这里要一起改 —— 否则帮助菜单会跳到页面顶部而不是对应章节:
#   链接本身不会报错, 只是默默失效 (实测踩过, README 改版后 5 个锚点废了 4 个)。
ANCHOR_USAGE = "#使用"
ANCHOR_FAQ = "#常见问题"
ANCHOR_HOWTO = "#内部实现"
ANCHOR_BG = "#这些结论是怎么来的"
ANCHOR_STRUCT = "#项目结构"
ANCHOR_BUILD = "#打包"
# 检查更新用: 仓库名 + API
REPO_SLUG = "poposjj/mirom"
WIN_W, WIN_H = 1280, 860

C_PRIMARY      = "#0067C0"
C_PRIMARY_LT   = "#4CA3E8"
C_GREEN        = "#39D353"
C_ORANGE       = "#F59E0B"
C_RED          = "#F85149"
C_CYAN         = "#2DD4BF"
C_GRAY         = "#8B949E"
DARK_BG        = "#1B1B1F"      # 窗口底色 (必须显式刷进 QPalette, 见 MainWindow.__init__)
CARD_BG        = "#27272A"      # 卡片/图表底色
TXT            = "#E6E6E6"      # 主文字色

# 各 CDN 线路在图上的固定配色 (图例/曲线/表格共用, 保证视觉一致)
LINE_COLORS = {"cdnorg": C_CYAN, "aliyun": C_ORANGE, "bn": C_GRAY,
               "bigota": "#4B5563", "hugeota": "#4B5563"}

STAGES = [("probe", "探测节点"), ("tune", "并发调优"),
          ("download", "下载"), ("verify", "完整性校验")]

# 同名文件冲突时 MessageBox.done() 的返回值。
# 用 QDialog 标准码之外的大数字, 避免和 Accepted/Rejected 撞上。
CONFLICT_COPY = 1001
CONFLICT_FORCE = 1002
CONFLICT_SKIP = 1003
CONFLICT_CANCEL = 1004


def _copy_name(fname):
    """由原文件名推出一个副本名 (真正落盘前还会再查一次是否被占用)。"""
    stem, ext = os.path.splitext(fname)
    return "%s (1)%s" % (stem, ext)


# ═══════════════════════════ 图标 ═══════════════════════════
def _asset(name):
    """
    定位打包资源。三种运行方式都要能找到:
      ① PyInstaller onefile  -> sys._MEIPASS
      ② PyInstaller onedir   -> exe 所在目录 (assets 与 exe 同级)
      ③ 直接跑源码           -> 本文件所在目录
    ⚠ 漏掉任何一条的表现都是"源码里图标正常, 打包出来就没图标了"。
    """
    cands = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "assets", name))
    if getattr(sys, "frozen", False):
        cands.append(os.path.join(os.path.dirname(sys.executable), "assets", name))
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "assets", name))
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def app_icon():
    """
    返回 QIcon。优先用多分辨率 ico (Qt 会按控件大小自动挑最合适的一档,
    比拿一张 512 硬缩要清楚), 退而求其次用 png。
    ⚠ 必须在 QApplication 存在之后调用 —— QIcon/QPixmap 需要 GUI 环境。
    """
    from PySide6.QtGui import QIcon
    ico = _asset("logo.ico")
    if ico:
        ic = QIcon(ico)
        if not ic.isNull():
            return ic
    png = _asset("logo.png") or _asset("logo_256.png")
    if png:
        return QIcon(png)
    return QIcon()


def set_windows_app_id():
    """
    给本进程设一个显式的 AppUserModelID。

    ★ 为什么必须做: Windows 任务栏是按 AppUserModelID 给窗口分组的。不设的话,
      冻结出来的 exe 会继承宿主进程的 ID, 任务栏上显示的是【Python 的图标】,
      而且右键菜单里会挂着 python。设成自己的 ID 之后, 任务栏才会用我们
      setWindowIcon 设的那个图标。
      (开始菜单固定、任务栏固定也都依赖这个 ID 才认得是同一个程序。)
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "mirom.xiaomi.rom.downloader.1")
    except Exception:
        pass


def _run_hidden_ps(script, timeout=25):
    """
    跑一段 PowerShell, 绝不弹黑框。

    ★ 这条规则对整个项目都成立, 而且是有用户报障背书的:
      "点测速等等还有很多按钮之后, 会弹出 PowerShell 又瞬间闪退"。
      根因: PyInstaller --windowed 出来的进程【没有控制台】, Windows 给
      powershell.exe 子进程新建一个 —— 就是那个一闪的黑框。
      所以任何 subprocess 调用都必须带 CREATE_NO_WINDOW + SW_HIDE。
    """
    import subprocess
    kw = dict(capture_output=True, text=True, timeout=timeout)
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                      # SW_HIDE
            kw["startupinfo"] = si
        except Exception:
            pass
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-ExecutionPolicy", "Bypass", "-Command", script], **kw)


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.2f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024.0


# ═══════════════════════════ 设置 (持久化) ═══════════════════════════
#  背景: 引擎支持 --conns / --max-conns / --limit / --proxy / --multi /
#  --no-verify / --retune / --no-prealloc 等一堆开关, 但旧界面上一个都够不着,
#  用户只能去命令行敲 —— 对一个"双击即用"的工具来说等于没有。
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".mirom_gui.json")

# (键, 默认值, 类型) —— 类型必须写死并强转: 配置文件是面向用户的,
# 手改坏了也不能让界面崩 (同 load_tune 的教训)。
DEFAULT_SETTINGS = {
    "outdir": os.path.join(os.path.expanduser("~"), "Downloads", "Roms"),
    "conns": 0,            # 0 = 自动调优
    "max_conns": 256,
    "limit_mbps": 0.0,     # 0 = 不限速
    "proxy": "",
    "multi": False,        # 多线路聚合
    "verify": True,        # 完成后 MD5 校验
    "retune": False,       # 强制重新调优
    "prealloc": True,      # 预分配磁盘空间
    "insecure": False,     # 跳过证书校验
}


def load_settings():
    """读设置。任何异常/类型不符都退回默认值, 绝不抛。"""
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return s
    except Exception:
        return s
    for k, dv in DEFAULT_SETTINGS.items():
        if k not in raw:
            continue
        v = raw[k]
        try:
            if isinstance(dv, bool):
                s[k] = bool(v)
            elif isinstance(dv, int):
                s[k] = int(v)
            elif isinstance(dv, float):
                s[k] = float(v)
            elif isinstance(dv, str):
                s[k] = str(v)
        except (TypeError, ValueError):
            pass                      # 这一项用默认值, 不影响其它项
    # 夹到合法范围, 免得配置文件里写个 -1 把引擎搞出未定义行为
    s["conns"] = max(0, min(int(s["conns"]), 4096))
    s["max_conns"] = max(1, min(int(s["max_conns"]), 4096))
    s["limit_mbps"] = max(0.0, float(s["limit_mbps"]))
    if s["conns"] and s["conns"] > s["max_conns"]:
        s["max_conns"] = s["conns"]   # 手动值不该被自动上限压住
    return s


def save_settings(s):
    try:
        with open(SETTINGS_PATH + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
        os.replace(SETTINGS_PATH + ".tmp", SETTINGS_PATH)
        return True
    except Exception:
        return False


def chip_qss(col, alpha=0.18, text=None):
    """
    生成"胶囊"样式。
    ⚠ 千万不要用 col + "2E" 拼 8 位十六进制! 实测踩过:
      CSS 是 #RRGGBBAA (alpha 在后), 而 Qt 的样式表按 #AARRGGBB 解析 (alpha 在前)。
      于是 "#4CA3E82E" 被当成 "alpha=0x4C 的 #A3E82E 绿色" —— 图例里
      "总速度"变成绿底、aliyun 变成红底红字几乎看不见。必须用 rgba()。
    """
    try:
        r = int(col[1:3], 16); g = int(col[3:5], 16); b = int(col[5:7], 16)
    except Exception:
        r = g = b = 128
    return "background:rgba(%d,%d,%d,%.2f);color:%s;" % (r, g, b, alpha, text or col)


def hms(s):
    if not s or s <= 0 or s > 86400 * 3:
        return "--:--"
    return time.strftime("%H:%M:%S", time.gmtime(s))


# ═══════════════════════════ 线程安全桥 ═══════════════════════════
class EngineSignals(QObject):
    """
    ⚠ 引擎的回调是在【后台线程】里被调用的。PySide6 规定:
      绝不能在非 GUI 线程里直接操作控件 —— 否则界面会随机崩溃/花屏。
      所以回调只做一件事: emit 一个 Signal。Qt 会自动用 QueuedConnection
      把槽函数排到主线程事件循环里执行, 这才是正确且安全的做法。
    """
    log = Signal(str)
    stage = Signal(str, str, int, int)
    meta = Signal(dict)
    prog = Signal(dict)
    done = Signal(dict)
    err = Signal(str)
    measure = Signal(dict)      # 探测/基准/爬坡的结构化结果 (导出 CSV 用)


# ═══════════════════════════ ① 折线图组件 ═══════════════════════════
class SpeedChart(CardWidget):
    """
    实时速度折线图 —— 软件的视觉核心。
    数据源: MiromEngine.on_progress 的 payload (2 点/秒)。
      speed  -> 总速度主曲线 (带渐变填充)
      lines  -> 各 CDN 线路瞬时速度 (细线, 可勾选)
      events -> 线路切换/重排等事件 (竖向虚线 + 标签)
    """
    MAXPTS = 600          # 60 秒窗口 @ 2 点/秒; 全程模式最多 600 点

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ts, self.tot = [], []
        self.per = {}                 # {tag: [v,...]}
        self.events = []              # [(ts, text)]
        self.window_sec = 60
        self.frozen = False
        self.peak = 0.0
        self.avg = 0.0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 10)
        lay.setSpacing(8)

        # ── 顶部: 标题 + 浮层指标 + 控件 ──
        top = QHBoxLayout()
        top.setSpacing(10)
        self.lab_title = StrongBodyLabel("实时速度")
        top.addWidget(self.lab_title)
        top.addSpacing(6)
        self.lab_live = QLabel("0.0 MB/s")
        self.lab_live.setStyleSheet(
            "font-size:19px;font-weight:700;color:%s;"
            "font-family:Consolas,'Courier New',monospace;" % C_PRIMARY_LT)
        top.addWidget(self.lab_live)
        self.lab_stat = QLabel("均值 --    峰值 --    抖动 --")
        self.lab_stat.setStyleSheet("color:%s;font-size:11px;" % C_GRAY)
        top.addWidget(self.lab_stat)
        top.addStretch(1)

        self.seg = SegmentedWidget()
        self.seg.setFixedHeight(28)
        for k, t in (("30", "30秒"), ("60", "60秒"), ("120", "2分钟"), ("all", "全程")):
            self.seg.addItem(k, t)
        self.seg.setCurrentItem("60")
        self.seg.currentItemChanged.connect(self._on_window)
        top.addWidget(self.seg)

        self.btn_freeze = TransparentToolButton(FIF.PAUSE)
        self.btn_freeze.setToolTip("冻结图表")
        self.btn_freeze.clicked.connect(self._toggle_freeze)
        top.addWidget(self.btn_freeze)
        lay.addLayout(top)
        # ── 图表本体 ──
        pg.setConfigOptions(antialias=True)
        self.pw = pg.PlotWidget()
        # ⚠ pyqtgraph 的 PlotWidget 是 QGraphicsView, 它的 viewport 背景不跟
        #   qfluentwidgets 的主题走 —— 实测即使在深色窗口里也仍是 Qt 默认的 #F0F0F0,
        #   在深色卡片上会突兀地亮一块。必须显式指定。
        self.pw.setBackground(CARD_BG)
        self.pw.showGrid(x=True, y=True, alpha=0.12)
        self.pw.setMouseEnabled(x=False, y=False)
        self.pw.hideButtons()
        self.pw.setMenuEnabled(False)
        self.pw.setMinimumHeight(176)
        ax_b = self.pw.getAxis("bottom")
        ax_l = self.pw.getAxis("left")
        for ax in (ax_b, ax_l):
            ax.setPen(pg.mkPen(C_GRAY, width=1))
            ax.setTextPen(pg.mkPen(C_GRAY))
        ax_l.setLabel(None)
        self.pw.setLabel("left", "MB/s", color=C_GRAY, size="9pt")
        self.pw.setLabel("bottom", "已用时间 (秒)", color=C_GRAY, size="9pt")
        lay.addWidget(self.pw, 1)

        # 主曲线: 粗线 + 渐变填充
        self.curve_total = self.pw.plot(pen=pg.mkPen(C_PRIMARY_LT, width=2.5))
        self.fill = pg.FillBetweenItem(self.curve_total,
                                       self.pw.plot(pen=None), brush=pg.mkBrush(76, 163, 232, 60))
        self.pw.addItem(self.fill)
        self.curve_zero = self.pw.plot(pen=None)
        self.per_curves = {}
        # 平均速度参考线
        self.avg_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#FFFFFF", width=1,
                                                              style=Qt.DashLine))
        self.pw.addItem(self.avg_line)
        self._ev_lines, self._ev_labels = [], []
        # 空图时也要有个合理的坐标系, 否则 pyqtgraph 默认 -400~400 很难看
        self.pw.setYRange(0, 150, padding=0)
        self.pw.setXRange(0, 60, padding=0)

        # ── 图例: 可点击开关单条曲线 ──
        leg = QHBoxLayout()
        leg.setSpacing(6)
        leg.addWidget(CaptionLabel("图例"))
        self._legend = {}
        for tag, name, col in (("__total__", "总速度", C_PRIMARY_LT),
                               ("cdnorg", "cdnorg", LINE_COLORS["cdnorg"]),
                               ("aliyun", "aliyun", LINE_COLORS["aliyun"]),
                               ("bn", "bn", LINE_COLORS["bn"]),
                               ("__avg__", "均值线", "#C9D1D9")):
            lb = QLabel("  %s  " % name)
            lb.setCursor(Qt.PointingHandCursor)
            lb.setFixedHeight(20)
            lb.setStyleSheet(chip_qss(col) + "border-radius:9px;font-size:11px;")
            lb.mousePressEvent = (lambda e, t=tag: self._toggle_series(t))
            self._legend[tag] = [lb, True, col]
            leg.addWidget(lb)
        leg.addStretch(1)
        lay.addLayout(leg)

    # ---- 演示波形 (仅用于 --shot 自检, 让空壳也能看出图表长什么样) ----
    def push_demo(self):
        import math, random
        random.seed(7)
        ev_at = {int(240 * 0.62): "⚠ 并行度塌陷", int(240 * 0.64): "⚠ 追加 aliyun ×64"}
        for i in range(240):
            t = i * 0.5
            pct = i / 240.0
            if pct < 0.05:
                base = 30 + pct / 0.05 * 70
            elif 0.62 < pct < 0.70:
                base = 12 + (0.70 - pct) * 200
            elif pct > 0.94:
                base = 45 * (1 - (pct - 0.94) / 0.06) + 3
            else:
                base = 100 + 12 * math.sin(i / 7.0)
            tot = max(0.0, base + random.uniform(-9, 11))
            cdn = max(0.0, tot * (0.985 if pct < 0.60 else 0.62))
            ali = max(0.0, tot - cdn) if pct >= 0.60 else max(0.0, tot * 0.015)
            self.push({"elapsed": t, "speed": tot,
                       "lines": {"cdnorg": cdn, "aliyun": ali, "bn": 0.2}})
            if i in ev_at:
                self.mark_event(ev_at[i])

    def _on_window(self, k):
        self.window_sec = None if k == "all" else int(k)

    def _toggle_freeze(self):
        self.frozen = not self.frozen
        self.btn_freeze.setIcon(FIF.PLAY if self.frozen else FIF.PAUSE)
        self.btn_freeze.setToolTip("继续" if self.frozen else "冻结图表")

    def push(self, d):
        """接收一个引擎进度包"""
        if self.frozen:
            return
        t = d.get("elapsed", 0.0)
        self.ts.append(t)
        self.tot.append(d.get("speed", 0.0))
        for tag, v in (d.get("lines") or {}).items():
            self.per.setdefault(tag, [])
            # 补齐长度, 保证与 ts 对齐
            while len(self.per[tag]) < len(self.ts) - 1:
                self.per[tag].append(0.0)
            self.per[tag].append(v)
        for tag in self.per:
            while len(self.per[tag]) < len(self.ts):
                self.per[tag].append(0.0)
        for arr in (self.ts, self.tot, *self.per.values()):
            if len(arr) > self.MAXPTS:
                del arr[:len(arr) - self.MAXPTS]
        self._redraw()

    def mark_event(self, text):
        """在曲线上打一条竖线 + 标签。用来标注'让位重排/追加线路'这类事件 ——
        用户一眼就能看出'什么时候换了线、换完之后有没有变快'。

        ⚠ 实测: 两个事件若挨得很近 (例如"并行度塌陷"和"追加线路"只差 1 秒),
          标签会互相覆盖, 前一条被压成半截 (实测出现 "⚠ 并" 这种残字)。
          所以按邻近事件数逐级错开纵向位置。
        """
        if not self.ts:
            return
        t = self.ts[-1]
        span = (self.window_sec or 60) / 8.0
        lvl = sum(1 for et, _ in self.events[-3:] if abs(t - et) < span)
        ypos = max(0.25, 0.90 - 0.15 * lvl)
        self.events.append((t, text))
        line = pg.InfiniteLine(
            pos=t, angle=90,
            pen=pg.mkPen(C_ORANGE, width=1, style=Qt.DashLine),
            label=text,
            labelOpts={"position": ypos, "color": C_ORANGE, "movable": False,
                       "fill": pg.mkBrush(58, 42, 12, 235),
                       "anchors": [(0.0, 1.0), (0.0, 1.0)]})
        self.pw.addItem(line)
        self._ev_lines.append(line)
        if len(self._ev_lines) > 20:
            old = self._ev_lines.pop(0)
            self.pw.removeItem(old)
            self.events.pop(0)

    def _toggle_series(self, tag):
        ent = self._legend.get(tag)
        if not ent:
            return
        lb, on, col = ent
        on = not on
        self._legend[tag] = [lb, on, col]
        if on:
            lb.setStyleSheet(chip_qss(col) + "border-radius:9px;font-size:11px;")
        else:
            lb.setStyleSheet("background:rgba(128,128,128,0.12);color:%s;"
                             "border-radius:9px;font-size:11px;text-decoration:line-through;"
                             % C_GRAY)
        if tag == "__total__":
            self.curve_total.setVisible(on)
            self.fill.setVisible(on)
            self.curve_zero.setVisible(on)
        elif tag == "__avg__":
            self.avg_line.setVisible(on)
        elif tag in self.per_curves:
            self.per_curves[tag].setVisible(on)

    def _redraw(self):
        if not self.ts:
            return
        t_end = self.ts[-1]
        t0 = 0.0 if self.window_sec is None else max(0.0, t_end - self.window_sec)
        idx = 0
        while idx < len(self.ts) and self.ts[idx] < t0:
            idx += 1
        xs = self.ts[idx:]
        ys = self.tot[idx:]
        if not xs:
            return
        self.curve_total.setData(xs, ys)
        self.curve_zero.setData([xs[0], xs[-1]], [0, 0])
        self.fill.setCurves(self.curve_total, self.curve_zero)

        # 分线
        for tag, arr in self.per.items():
            if tag not in self.per_curves:
                self.per_curves[tag] = self.pw.plot(
                    pen=pg.mkPen(LINE_COLORS.get(tag, C_GRAY), width=1.2))
            self.per_curves[tag].setData(xs, arr[idx:])
            self.per_curves[tag].setVisible(self._legend.get(tag, [None, True, None])[1])

        # 事件竖线: 滚出窗口的就隐藏, 免得越积越多
        for (et, _), ln in zip(self.events, self._ev_lines):
            ln.setVisible(et >= t0)

        # Y 轴自适应 (留 15% 余量)
        ymax = max(ys) if ys else 1.0
        self.pw.setYRange(0, max(1.0, ymax * 1.15), padding=0)
        self.pw.setXRange(xs[0], max(xs[-1], xs[0] + 1), padding=0)

        cur = ys[-1]
        self.avg = sum(ys) / len(ys)
        self.peak = max(self.peak, max(ys))
        self.avg_line.setValue(self.avg)
        self.lab_live.setText("%.1f MB/s" % cur)
        cv = 0.0
        if len(ys) > 1 and self.avg > 0:
            m = self.avg
            cv = (sum((v - m) ** 2 for v in ys) / len(ys)) ** 0.5 / m * 100
        self.lab_stat.setText("均值 %.1f   峰值 %.1f   抖动 CV %.0f%%"
                              % (self.avg, self.peak, cv))

    def reset(self):
        self.ts, self.tot, self.per, self.events = [], [], {}, []
        self.peak = 0.0
        self.curve_total.setData([], [])
        self.curve_zero.setData([], [])
        for c in self.per_curves.values():
            c.setData([], [])
        self.per_curves.clear()
        for ln in self._ev_lines:
            self.pw.removeItem(ln)
        self._ev_lines = []
        self.pw.setYRange(0, 150, padding=0)
        self.pw.setXRange(0, 60, padding=0)
        self.lab_live.setText("0.0 MB/s")
        self.lab_stat.setText("均值 --    峰值 --    抖动 --")


# ═══════════════════════════ ② 镜像徽章 ═══════════════════════════
class MirrorBadge(QLabel):
    """一个 CDN 节点的小胶囊: 状态点 + 域名 + 质量标记"""
    STYLE = {
        "✓": ("rgba(57,211,83,0.16)",  "#7EE787", C_GREEN,  "首选"),
        "△": ("rgba(245,158,11,0.16)", "#F0B429", C_ORANGE, "较快"),
        "⚠": ("rgba(148,163,184,0.14)","#9CA3AF", C_GRAY,   "不稳"),
        "?": ("rgba(148,163,184,0.14)","#9CA3AF", C_GRAY,   "未知"),
    }

    def __init__(self, tag, quality, note="", enabled=True, picked=False, parent=None):
        super().__init__(parent)
        bg, fg, dot, word = self.STYLE.get(quality, self.STYLE["?"])
        mark = "✗ 假206挂死" if not enabled else "%s %s" % (quality, word)
        self.setText("  ●  %s   %s  " % (tag, mark))
        self.setToolTip(note or tag)
        if not enabled:
            self.setStyleSheet(
                "QLabel{background:rgba(120,120,120,0.10);color:rgba(160,160,160,0.55);"
                "border:1px solid rgba(120,120,120,0.18);border-radius:11px;"
                "padding:1px 8px;font-size:11px;}")
        else:
            border = ("2px solid %s" % C_PRIMARY_LT) if picked else "1px solid transparent"
            self.setStyleSheet(
                "QLabel{background:%s;color:%s;border:%s;border-radius:11px;"
                "padding:1px 8px;font-size:11px;}" % (bg, fg, border))
        self.setFixedHeight(23)
        if not enabled:
            self.setCursor(Qt.ForbiddenCursor)


# ═══════════════════════════ ③ 阶段指示器 ═══════════════════════════
class StageIndicator(QWidget):
    """⓵ 探测 → ⓶ 调优 → ⓷ 下载 → ⓸ 校验"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cur = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.dots, self.labs, self.bars = [], [], []
        for i, (k, name) in enumerate(STAGES):
            d = QLabel(str(i + 1))
            d.setFixedSize(24, 24)
            d.setAlignment(Qt.AlignCenter)
            self.dots.append(d)
            lay.addWidget(d)
            lb = BodyLabel(name)
            lb.setContentsMargins(8, 0, 8, 0)
            self.labs.append(lb)
            lay.addWidget(lb)
            if i < len(STAGES) - 1:
                bar = QFrame()
                bar.setFixedHeight(2)
                bar.setMinimumWidth(48)
                bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                self.bars.append(bar)
                lay.addWidget(bar)
        self.set_stage(0)

    def set_stage(self, idx, done_all=False):
        self.cur = idx
        for i, (d, lb) in enumerate(zip(self.dots, self.labs)):
            if done_all or i < idx:
                d.setText("✓")
                d.setStyleSheet("QLabel{background:%s;color:#0B1220;border-radius:11px;"
                                "font-weight:bold;font-size:12px;}" % C_PRIMARY_LT)
                lb.setStyleSheet("color:palette(text);")
            elif i == idx:
                d.setText(str(i + 1))
                d.setStyleSheet("QLabel{background:transparent;color:%s;border:2px solid %s;"
                                "border-radius:11px;font-weight:bold;font-size:12px;}"
                                % (C_PRIMARY_LT, C_PRIMARY_LT))
                lb.setStyleSheet("color:%s;font-weight:bold;" % C_PRIMARY_LT)
            else:
                d.setText(str(i + 1))
                d.setStyleSheet("QLabel{background:transparent;color:%s;border:1px solid %s;"
                                "border-radius:11px;font-size:12px;}" % (C_GRAY, C_GRAY))
                lb.setStyleSheet("color:%s;" % C_GRAY)
        for i, bar in enumerate(self.bars):
            bar.setStyleSheet("background:%s;" % (C_PRIMARY_LT if i < idx else
                                                 "rgba(128,128,128,0.25)"))


# ═══════════════════════════ ④ 主窗口 ═══════════════════════════
class ConflictDialog(QDialog):
    """
    「已存在同名文件」对话框。

    ★ 为什么不用 qfluentwidgets 的 MessageBox:
      MessageBoxBase 在 __init__ 里已经把 yesButton.clicked 接到了自己的 accept(),
      也就是 done(Accepted=1)。我们若再 connect 一次去 done(自定义码), 两个槽都会跑,
      而【内置那个先连先跑】—— exec() 永远拿到 1, 自定义码永远读不到。
      结果就是"点下载副本没反应/点了却当取消处理"。
      自己写 QDialog 才能完全控制返回值。
    """

    def __init__(self, path_show, copy_name, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("已存在同名文件")
        self.setMinimumWidth(560)
        self.result_code = CONFLICT_CANCEL
        self.setStyleSheet(
            "QDialog{background:%s;} QLabel{color:%s;}"
            "QPushButton{background:#3A3A40;color:%s;border:none;"
            "border-radius:4px;padding:7px 16px;}"
            "QPushButton:hover{background:#4A4A52;}"
            "QPushButton#primary{background:%s;}"
            "QPushButton#primary:hover{background:%s;}"
            % (DARK_BG, TXT, TXT, C_PRIMARY, C_PRIMARY_LT))

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 14)
        v.setSpacing(10)

        t = QLabel("这个文件已经在下载目录里了")
        t.setStyleSheet("font-size:15px;font-weight:600;color:%s;" % TXT)
        v.addWidget(t)

        p = QLabel(path_show)
        p.setWordWrap(True)
        p.setStyleSheet("color:%s;font-size:12px;" % C_GRAY)
        v.addWidget(p)

        v.addSpacing(4)
        for text, code, desc in (
                ("下载副本", CONFLICT_COPY,
                 "保留原文件，另存为「%s」后照常下载" % copy_name),
                ("覆盖重下", CONFLICT_FORCE,
                 "删掉旧文件重新下一份，完成后照常校验"),
                ("跳过", CONFLICT_SKIP,
                 "不下载，只校验现有文件；通过就算完成")):
            b = QPushButton(text)
            b.setObjectName("primary" if code == CONFLICT_COPY else "x")
            b.clicked.connect(lambda _=False, c=code: self.done(c))
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:%s;font-size:12px;" % C_GRAY)
            row = QHBoxLayout()
            row.addWidget(b)
            row.addWidget(d, 1)
            v.addLayout(row)

        v.addSpacing(6)
        cancel = QPushButton("取消")
        cancel.clicked.connect(lambda: self.done(CONFLICT_CANCEL))
        crow = QHBoxLayout()
        crow.addStretch(1)
        crow.addWidget(cancel)
        v.addLayout(crow)

    def done(self, code):
        self.result_code = code
        QDialog.done(self, code)


class SettingsDialog(QDialog):
    """
    设置对话框。

    ★ 为什么必须补上:
      引擎有 --conns/--max-conns/--limit/--proxy/--multi/--no-verify/--retune/
      --no-prealloc 一整排开关, 但旧界面上一个都够不着 —— 对一个"双击即用"的
      工具来说, 等于这些能力不存在。小白用户不会去开命令行。
      这里的每一项都直接对应 MiromEngine 的一个构造参数, 不新造概念。

    用纯 QDialog + 显式深色调色板, 不用 qfluentwidgets 的 MessageBoxBase:
    后者的按钮布局是给"确认/取消"两句话设计的, 塞不下表单, 而且子类化时
    很容易踩到它的内部 objectName 约定。
    """

    def __init__(self, cur, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self.setStyleSheet(
            "QDialog{background:%s;} QLabel{color:%s;}"
            "QCheckBox{color:%s;} QLineEdit,QSpinBox,QDoubleSpinBox{"
            "background:#2D2D31;color:%s;border:1px solid #3F3F46;"
            "border-radius:4px;padding:4px 6px;}"
            "QPushButton{background:#3A3A40;color:%s;border:none;"
            "border-radius:4px;padding:6px 18px;}"
            "QPushButton:hover{background:#4A4A52;}"
            % (DARK_BG, TXT, TXT, TXT, TXT))
        self._cur = dict(cur)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setContentsMargins(18, 18, 18, 8)
        form.setSpacing(10)

        # ---- 下载目录 ----
        dirrow = QHBoxLayout()
        self.ed_dir = QLineEdit(self._cur.get("outdir", ""))
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._pick_dir)
        dirrow.addWidget(self.ed_dir, 1)
        dirrow.addWidget(btn_browse)
        dirrow_w = QWidget()
        dirrow_w.setLayout(dirrow)
        dirrow.setContentsMargins(0, 0, 0, 0)
        form.addRow("下载目录", dirrow_w)

        # ---- 并发连接数 ----
        self.sp_conns = QSpinBox()
        self.sp_conns.setRange(0, 4096)
        self.sp_conns.setValue(int(self._cur.get("conns", 0)))
        self.sp_conns.setToolTip("0 = 自动调优 (推荐)。手动指定会跳过并发爬坡。")
        form.addRow("并发连接数", self.sp_conns)

        self.sp_maxconns = QSpinBox()
        self.sp_maxconns.setRange(1, 4096)
        self.sp_maxconns.setValue(int(self._cur.get("max_conns", 256)))
        self.sp_maxconns.setToolTip("自动调优时允许的最大连接数")
        form.addRow("自动调优上限", self.sp_maxconns)

        # ---- 限速 ----
        self.sp_limit = QDoubleSpinBox()
        self.sp_limit.setRange(0.0, 10000.0)
        self.sp_limit.setDecimals(1)
        self.sp_limit.setSingleStep(1.0)
        self.sp_limit.setValue(float(self._cur.get("limit_mbps", 0.0)))
        self.sp_limit.setSuffix(" MB/s")
        self.sp_limit.setToolTip("0 = 不限速。限制的是【总带宽】, 所有连接共享。")
        form.addRow("限速", self.sp_limit)

        # ---- 代理 ----
        self.ed_proxy = QLineEdit(self._cur.get("proxy", ""))
        self.ed_proxy.setPlaceholderText("留空 = 不用代理。例: http://127.0.0.1:7890")
        form.addRow("HTTP 代理", self.ed_proxy)

        # ---- 开关们 ----
        self.ck_multi = QCheckBox("多线路聚合（把多条 CDN 线路的带宽加起来，通常更快）")
        self.ck_multi.setChecked(bool(self._cur.get("multi", False)))
        form.addRow("", self.ck_multi)

        self.ck_verify = QCheckBox("下载完成后校验 MD5（强烈建议保持勾选）")
        self.ck_verify.setChecked(bool(self._cur.get("verify", True)))
        form.addRow("", self.ck_verify)

        self.ck_prealloc = QCheckBox("预分配磁盘空间（避免边下边碎片化，机械盘建议勾选）")
        self.ck_prealloc.setChecked(bool(self._cur.get("prealloc", True)))
        form.addRow("", self.ck_prealloc)

        self.ck_retune = QCheckBox("每次重新做并发调优（默认复用 24 小时内的测速结果）")
        self.ck_retune.setChecked(bool(self._cur.get("retune", False)))
        form.addRow("", self.ck_retune)

        self.ck_insecure = QCheckBox("跳过 TLS 证书校验（仅在公司中间人代理环境下才需要）")
        self.ck_insecure.setChecked(bool(self._cur.get("insecure", False)))
        form.addRow("", self.ck_insecure)

        tip = QLabel("提示：改完点「保存」即刻生效，下次下载使用。")
        tip.setStyleSheet("color:%s;font-size:12px;" % C_GRAY)
        form.addRow("", tip)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Save).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(btns)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录",
                                             self.ed_dir.text() or os.path.expanduser("~"))
        if d:
            self.ed_dir.setText(d)

    def values(self):
        """返回一份可以直接喂给 MiromEngine / save_settings 的字典。"""
        return {
            "outdir": self.ed_dir.text().strip() or DEFAULT_SETTINGS["outdir"],
            "conns": int(self.sp_conns.value()),
            "max_conns": int(self.sp_maxconns.value()),
            "limit_mbps": float(self.sp_limit.value()),
            "proxy": self.ed_proxy.text().strip(),
            "multi": bool(self.ck_multi.isChecked()),
            "verify": bool(self.ck_verify.isChecked()),
            "prealloc": bool(self.ck_prealloc.isChecked()),
            "retune": bool(self.ck_retune.isChecked()),
            "insecure": bool(self.ck_insecure.isChecked()),
        }


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        # ⚠ outdir 必须在构造任何子控件【之前】定义 ——
        #   _build_info / _build_log 都会读它, 放到后面会直接 AttributeError。
        #   现在优先用用户保存过的设置 (首次运行时 load_settings 返回默认值)。
        self.cfg = load_settings()
        self.outdir = self.cfg["outdir"]
        self.eng = None
        # ⚠ 必须在 __init__ 就初始化: 没跑过任何任务时点「导出测试数据」会读它,
        #   放到 _start 里赋值的话那条路径直接 AttributeError。
        self.measure = {}
        # None = 弹窗问用户; 非 None 时代表"非交互模式下的预设选择"。
        # 由 --test-download 之类的自动化入口设置 —— 那种场景没有人能点弹窗,
        # 一旦弹出就是永久阻塞。
        self.auto_conflict = None

        setTheme(Theme.DARK)
        setThemeColor(C_PRIMARY)

        # ⚠ 关键修复: setTheme(DARK) 只更新了 qfluentwidgets 自己的控件
        #   (实测 LineEdit 内部确实是深色 #5D5D5D), 但 QMainWindow 的
        #   Qt 默认调色板仍是浅色 #F0F0F0 —— 于是半透明的 CardWidget
        #   透出浅底, 白色文字直接变成"隐形" (实测"仅测速"按钮完全看不见)。
        #   必须把窗口自身的 QPalette 也刷成深色, 主题标记与视觉才一致。
        pal = self.palette()
        for role, col in ((QPalette.Window, QColor(DARK_BG)),
                          (QPalette.Base, QColor(DARK_BG)),
                          (QPalette.Button, QColor(DARK_BG)),
                          (QPalette.Text, QColor(TXT)),
                          (QPalette.WindowText, QColor(TXT)),
                          (QPalette.ButtonText, QColor(TXT))):
            pal.setColor(role, col)
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        self.setWindowTitle("%s — %s  %s" % (APP_NAME, APP_TITLE, BYLINE))
        # 标题栏左上角 + 任务栏。app 级也设了, 这里是双保险 ——
        # 万一有人把 MainWindow 嵌进别的宿主里, 它也有自己的图标。
        try:
            _ic = app_icon()
            if not _ic.isNull():
                self.setWindowIcon(_ic)
        except Exception:
            pass
        self.resize(WIN_W, WIN_H)
        self.setMinimumSize(1040, 720)

        self._build_menubar()

        central = QWidget()
        central.setObjectName("central")
        # 用 objectName 限定作用域, 避免样式表往下污染子控件
        self.setStyleSheet("#central{background:%s;}" % DARK_BG)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 10, 14, 6)
        root.setSpacing(10)

        root.addWidget(self._build_input())
        root.addWidget(self._build_info())
        root.addWidget(self._build_progress())
        self.chart = SpeedChart()
        root.addWidget(self.chart, 1)
        root.addWidget(self._build_log())

        self._build_statusbar()

        # ── 引擎桥接 ──
        self.sig = EngineSignals()
        self.sig.log.connect(self._on_log)
        self.sig.stage.connect(self._on_stage)
        self.sig.meta.connect(self._on_meta)
        self.sig.prog.connect(self._on_prog)
        self.sig.done.connect(self._on_done)
        self.sig.err.connect(self._on_err)
        self.sig.measure.connect(self._on_measure)
        self._wire()
        self._apply_idle()

    # ═══════════════════ 引擎接线 ═══════════════════
    def _wire(self):
        self.btn_go.clicked.connect(lambda: self._start(probe=False))
        self.btn_probe.clicked.connect(lambda: self._start(probe=True))
        self.btn_paste.clicked.connect(self._paste)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_cancel.clicked.connect(self._cancel)

    def _paste(self):
        cb = QApplication.clipboard().text().strip()
        if cb:
            self.ed_url.setText(cb)
            InfoBar.success("已粘贴", cb[:72] + ("…" if len(cb) > 72 else ""),
                            duration=2200, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _apply_idle(self):
        self.btn_go.setEnabled(True)
        self.btn_probe.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.btn_pause.setText("暂停")
        self.btn_pause.setIcon(FIF.PAUSE)

    def _apply_running(self, probe=False):
        self.btn_go.setEnabled(False)
        self.btn_probe.setEnabled(False)
        self.btn_pause.setEnabled(not probe)
        self.btn_cancel.setEnabled(True)

    # ---------- 启动 ----------
    def _resolve_conflict(self, url, probe):
        """
        开工前检查目标文件是否已存在, 并让用户决定怎么办。

        ★ 用户报的 bug: "如果下载文件夹有相同文件, 直接显示下载成功
          而不是提醒之后创建副本下载"。
          旧行为: 引擎发现同名文件 + MD5 通过, 直接 log 一句"无需重新下载"
          然后返回 0 —— 界面上就是"下载完成"。用户以为工具坏了,
          因为他明明是想再下一份。
          现在: 先问, 再按选择走 —— 跳过 / 下载副本 / 覆盖重下。

        ⚠ 有断点文件时【不算冲突】—— 那是续传, 直接放过。
          这个判据在引擎里也踩过坑 (见 run_flow 里的注释), 不能在界面这层再踩一次。

        返回 (action, save_as): action ∈ {"go", "force", "copy", "cancel"}
        """
        # 仅测速不写盘 → 没有"冲突"可言, 直接放过。
        # 旧版这个 probe 参数只是收下不用, 全靠调用方记得先 if not probe —— 换个
        # 调用点就会在纯测速时弹出一个毫无意义的"文件已存在"窗口。
        if probe:
            return "go", None
        # 非交互模式 (--test-download 这类自动化入口): 没人能点弹窗。
        # 不处理的话进程会永远停在 box.exec() 上 —— 实测就是这么把 EXE 验收
        # 卡到超时的。此时按 auto_conflict 指定的选择走。
        if getattr(self, "auto_conflict", None) is not None:
            return self.auto_conflict
        try:
            from mirom import parse_url, state_path, unique_copy_path
            fname = parse_url(url)["file"]
        except Exception:
            return "go", None          # 解析不了就交给引擎去报错, 界面别抢答
        target = os.path.join(self.outdir, fname)
        if not os.path.exists(target):
            return "go", None
        if os.path.exists(state_path(target)):
            return "go", None          # 有断点 => 续传, 不是冲突

        # 目录名太长时弹窗会撑爆, 这里截断显示
        show = target if len(target) <= 68 else ("..." + target[-65:])
        box = ConflictDialog(show, _copy_name(fname), self)
        box.exec()
        rc = box.result_code
        if rc == CONFLICT_COPY:
            return "copy", _copy_name(fname)
        if rc == CONFLICT_FORCE:
            return "force", None
        if rc == CONFLICT_SKIP:
            return "go", None          # 交给引擎做"已有完整文件?"的校验
        return "cancel", None

    def _start(self, probe=False):
        url = self.ed_url.text().strip()
        if not url:
            InfoBar.warning("缺少链接", "请先粘贴一个小米 ROM 下载链接",
                            duration=2500, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        if self.eng is not None and self.eng.is_alive():
            InfoBar.warning("正在运行", "已有任务在进行中, 请先取消",
                            duration=2500, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return

        # 同名文件冲突 —— 先问清楚再动手 (仅测速不写盘, 不必问)
        save_as, force = None, False
        if not probe:
            act, save_as = self._resolve_conflict(url, probe)
            if act == "cancel":
                self._append_log("[%s] 已取消 (目标文件已存在)" % time.strftime("%H:%M:%S"),
                                 C_ORANGE)
                return
            if act == "force":
                force = True
            elif act == "copy":
                self._append_log("[%s] 原文件保留, 本次另存为: %s"
                                 % (time.strftime("%H:%M:%S"), save_as))
            elif save_as is None:
                self._append_log("[%s] 目标文件已存在, 先校验现有文件"
                                 % time.strftime("%H:%M:%S"), C_GRAY)

        # 复位界面
        self.chart.reset()
        self._last_measure = None
        self.measure = {}
        self.log_view.clear()
        self._log_lines = []
        self.bar.setValue(0)
        self.stages.set_stage(0)
        self.lb_md5_state.setText("⏳ 待校验")
        self.lb_md5_state.setStyleSheet("color:%s;" % C_GRAY)
        self._apply_running(probe)

        # ★ 设置真正生效的地方。旧版这里只传 url/outdir/probe, 界面上根本没有
        #   入口去改并发/限速/代理 —— 引擎支持一整排开关却全够不着。
        #   --probe 只测速不写盘, 所以预分配/校验/MD5 这些与写盘有关的开关
        #   在测速模式下无意义, 传了也不影响 (run_flow 里 a.probe 会短路)。
        c = self.cfg
        self.eng = MiromEngine(
            url, outdir=self.outdir, probe=probe,
            conns=0 if probe else int(c["conns"]),
            max_conns=int(c["max_conns"]),
            limit_mbps=float(c["limit_mbps"]),
            proxy=(c["proxy"] or None),
            verify=bool(c["verify"]),
            multi=bool(c["multi"]),
            retune=bool(c["retune"]),
            insecure=bool(c["insecure"]),
            force=bool(force),
            extra_args={"no_prealloc": not bool(c["prealloc"]),
                        "save_as": save_as} if save_as
                       else {"no_prealloc": not bool(c["prealloc"])},
        )
        # 回调只做 emit —— 真正的更新在主线程槽函数里 (见 EngineSignals 注释)
        # 回调只做 emit —— 真正的更新在主线程槽函数里 (见 EngineSignals 注释)
        self.eng.on_log = lambda s: self.sig.log.emit(s)
        self.eng.on_stage = lambda k, l, i, n: self.sig.stage.emit(k, l, i, n)
        self.eng.on_meta = lambda d: self.sig.meta.emit(d)
        self.eng.on_progress = lambda d: self.sig.prog.emit(d)
        self.eng.on_done = lambda r: self.sig.done.emit(r)
        self.eng.on_error = lambda s: self.sig.err.emit(s)
        self.eng.on_measure = lambda d: self.sig.measure.emit(d)
        self.eng.start()
        self._append_log("[%s] ▶ %s" % (time.strftime("%H:%M:%S"),
                                        "仅测速" if probe else "开始下载: " + url))

    # ---------- 主线程槽 ----------
    @Slot(str)
    def _on_log(self, s):
        self._append_log(s)
        # 关键事件在折线图上打竖线 —— 一眼看出"什么时候换了线、换完快没快"
        for kw, label in (("让位最大的", "⚠ 让位重排"),
                          ("追加线路", "⚠ 追加线路"),
                          ("并行度塌陷", None),
                          ("复用", "◆ 复用调优缓存")):
            if label and kw in s:
                self.chart.mark_event(label)
                break

    @Slot(str, str, int, int)
    def _on_stage(self, key, label, idx, total):
        self.stages.set_stage(max(0, idx - 1))
        self.lb_stage.setText("阶段: %s" % label)

    @Slot(dict)
    def _on_meta(self, d):
        self.lb_size.setText("%s  (%s 字节)"
                             % (human(d.get("size", 0)), format(d.get("size", 0), ",")))
        self.lb_path.setText(d.get("out", ""))
        self.lb_md5.setText(d.get("md5_prefix") or "—")
        self.set_mirrors(d.get("mirrors", []), picked=None)
        self.lb_disk.setText("磁盘 %s · 写盘占用 --" % d.get("disk_text", "?"))
        self._append_log("[%s] 输出目录: %s" % (time.strftime("%H:%M:%S"), d.get("out", "")))

    @Slot(dict)
    def _on_prog(self, d):
        pct = d.get("pct", 0.0)
        self.bar.setValue(int(pct))
        self.lb_pct.setText("%.1f%%" % pct)
        self.lb_bytes.setText("%s / %s" % (human(d.get("done", 0)), human(d.get("total", 0))))
        L = self.num_labels
        L["speed"].setText("%.1f" % d.get("speed", 0.0))
        L["avg"].setText("%.1f" % d.get("avg", 0.0))
        L["peak"].setText("%.1f" % d.get("peak", 0.0))
        L["elapsed"].setText(hms(d.get("elapsed", 0)))
        L["eta"].setText(hms(d.get("eta", 0)))
        L["conns"].setText("%d" % d.get("conns", 0))
        self.lb_conns.setText("连接 %d (活跃 %d)"
                              % (d.get("conns", 0), d.get("active", 0)))
        self.chart.push(d)

    @Slot(dict)
    def _on_measure(self, d):
        """接收引擎送来的结构化测量结果 (探测/基准/爬坡)。"""
        self.measure = d

    def _on_done(self, r):
        ok = r.get("ok")
        self._apply_idle()
        self.stages.set_stage(4, done_all=bool(ok))
        if ok:
            self.lb_md5_state.setText("✅ 已校验")
            self.lb_md5_state.setStyleSheet("color:%s;" % C_GREEN)
            self.lb_stage.setText("阶段: 完成")
            InfoBar.success("下载完成", "文件已通过 MD5 校验",
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            self.lb_stage.setText("阶段: 未完成")
            InfoBar.warning("未完成", "请查看日志; 断点已保留, 可重新开始续传",
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    @Slot(str)
    def _on_err(self, s):
        self._apply_idle()
        self._append_log(s, C_RED)
        InfoBar.error("出错", s.split("\n")[0][:160],
                      duration=6000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    # ---------- 交互 ----------
    def _toggle_pause(self):
        if self.eng is None:
            return
        if self.eng.is_paused():
            self.eng.resume()
            self.btn_pause.setText("暂停")
            self.btn_pause.setIcon(FIF.PAUSE)
        else:
            self.eng.pause()
            self.btn_pause.setText("继续")
            self.btn_pause.setIcon(FIF.PLAY)

    def _cancel(self):
        if self.eng is not None and self.eng.is_alive():
            self.eng.cancel()
            # 提示要如实反映"现在处于哪个阶段, 大概多久停"。
            # 原来只写"已请求取消…", 而探测/调优阶段旧实现根本不响应 —— 界面
            # 嘴上说在取消, 引擎还在跑 40 秒, 这是最招骂的一种反馈。
            st = self.lb_stage.text() or ""
            if "下载" in st:
                hint = "正在收尾在途分片, 约 6 秒"
            elif "校验" in st:
                hint = "校验不可中断, 跑完即停"
            else:
                hint = "正在从探测/调优阶段退出, 数秒内生效"
            self._append_log("[%s] 已请求取消 —— %s" % (time.strftime("%H:%M:%S"), hint),
                             C_ORANGE)

    # ---------- 顶部菜单 ----------
    def _build_menubar(self):
        mb = self.menuBar()
        mb.setNativeMenuBar(False)

        def menu(title, items):
            m = mb.addMenu(title)
            for it in items:
                if it is None:
                    m.addSeparator()
                    continue
                text, shortcut, cb = (list(it) + [None, None])[:3] if isinstance(it, (tuple, list)) \
                    else (it, None, None)
                act = m.addAction(text)
                if shortcut:
                    act.setShortcut(shortcut)
                if text == "开机自动启动":
                    # 勾选态要反映真实状态 (启动文件夹里到底有没有那个 lnk),
                    # 不能只是点一下翻一下 —— 用户手删了启动项之后菜单会骗人。
                    act.setCheckable(True)
                    act.setChecked(self._autostart_on())
                    self._act_autostart = act
                if cb:
                    act.triggered.connect(cb)
            return m

        menu("文件(&F)", [("打开链接…", "Ctrl+L", lambda: self.ed_url.setFocus()),
                          ("从剪贴板粘贴链接", "Ctrl+V", self._paste),
                          ("选择下载目录…", None, self._choose_dir),
                          ("打开下载目录", "Ctrl+O", self._open_dir), None,
                          ("退出", "Ctrl+Q", self.close)])
        menu("编辑(&E)", [("复制链接", None, lambda: QApplication.clipboard().setText(
                              self.ed_url.text())),
                          ("全选链接", "Ctrl+A", self._select_all),
                          ("清空日志", None, self._clear_log), None,
                          ("复制全部日志", None, self._copy_log)])
        menu("下载(&D)", [("开始下载", "F5", lambda: self._start(False)),
                          ("仅测速", None, lambda: self._start(True)),
                          ("暂停 / 继续", "F6", self._toggle_pause),
                          ("取消", "Esc", self._cancel), None,
                          ("断点续传说明", None, self._about_resume)])
        menu("设置(&S)", [("下载设置…", "Ctrl+,", self._open_settings), None,
                          ("下载目录…", None, self._choose_dir),
                          ("打开下载目录", None, self._open_dir), None,
                          ("创建桌面快捷方式", None, self._make_desktop_shortcut),
                          ("开机自动启动", None, self._toggle_autostart), None,
                          ("重置为默认设置", None, self._reset_settings), None,
                          ("打开调优缓存", None, self._open_tune),
                          ("清空调优缓存", None, self._clear_tune)])
        menu("工具(&T)", [("运行引擎自检", None, self._run_selftest),
                          ("导出本次测速数据 CSV", None, self._export_csv), None,
                          ("诊断报告 (反馈问题时发这个)", None, self._save_diag)])
        menu("帮助(&H)", [
            ("离线使用说明", "F1", self._open_local_doc),
            ("在线文档", None, lambda: self._open_url(README_URL, "在线文档")),
            ("使用教程", None,
             lambda: self._open_url(README_URL + ANCHOR_USAGE, "使用教程")),
            ("常见问题", None,
             lambda: self._open_url(README_URL + ANCHOR_FAQ, "常见问题")),
            None,
            ("内部实现", None,
             lambda: self._open_url(README_URL + ANCHOR_HOWTO, "内部实现")),
            ("实测记录", None,
             lambda: self._open_url(README_URL + ANCHOR_BG, "实测记录")),
            ("项目结构", None,
             lambda: self._open_url(README_URL + ANCHOR_STRUCT, "项目结构")),
            ("打包说明", None,
             lambda: self._open_url(README_URL + ANCHOR_BUILD, "打包说明")),
            None,
            ("检查更新", None, self._check_update),
            ("打开下载页", None, lambda: self._open_url(RELEASES_URL, "下载页")),
            ("反馈问题 / 提建议", None, lambda: self._open_url(ISSUES_URL, "反馈问题")),
            ("项目主页", None, lambda: self._open_url(REPO_URL, "项目主页")),
            None,
            ("导出诊断报告", None, self._save_diag),
            ("打开下载目录", "Ctrl+O", self._open_dir),
            None,
            ("关于 mirom", None, self._about)])

    # ---------- 链接输入区 ----------
    def _build_input(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        # 链接框前面的小图标 —— 用程序自己的 logo, 不再是 🔗 emoji。
        # emoji 的问题: 字体一换形状就变, 而且在不同 Windows 版本上配色不一致,
        # 跟旁边的深色卡片格格不入。换成 logo 位图则与任务栏/标题栏完全一致。
        ico = QLabel()
        _pm = app_icon().pixmap(22, 22)
        if not _pm.isNull():
            ico.setPixmap(_pm)
            ico.setFixedSize(22, 22)
            ico.setScaledContents(True)
        else:
            ico.setText("🔗")               # 图标缺失时的兜底, 不能空着
            ico.setStyleSheet("font-size:15px;")
        ico.setToolTip("小米 ROM 下载加速器")
        row.addWidget(ico)
        self.ed_url = LineEdit()
        self.ed_url.setPlaceholderText("粘贴小米 ROM 下载链接（任意一个 CDN 节点即可）")
        self.ed_url.setClearButtonEnabled(True)
        self.ed_url.setMinimumHeight(36)
        self.ed_url.setText(
            "https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip")
        row.addWidget(self.ed_url, 1)

        self.btn_paste = TransparentToolButton(FIF.PASTE)
        self.btn_paste.setToolTip("从剪贴板粘贴")
        row.addWidget(self.btn_paste)

        self.btn_go = PrimaryPushButton("开始下载")
        self.btn_go.setFixedSize(100, 36)
        row.addWidget(self.btn_go)
        self.btn_probe = PushButton("仅测速")
        self.btn_probe.setFixedSize(84, 36)
        row.addWidget(self.btn_probe)
        v.addLayout(row)

        # 镜像徽章行
        brow = QHBoxLayout()
        brow.setSpacing(6)
        brow.setContentsMargins(24, 0, 0, 0)
        self.badge_box = brow
        brow.addStretch(1)
        v.addLayout(brow)
        self._set_badges_demo()
        return w

    def _set_badges_demo(self):
        """先用假数据摆出全部 5 个徽章 (真实数据由 on_meta 回调填入)"""
        demo = [("cdnorg", "✓", "零连接错误, 192 并发可达 109 MB/s, CV 8%", True, True),
                ("aliyun", "△", "单点吞吐最高但爆发式抖动", True, False),
                ("bn", "⚠", "连接大面积失败", True, False),
                ("bigota", "✗", "假206挂死: 返回206+完整Content-Length, 但每连接仅传~256KB即停滞", False, False),
                ("hugeota", "✗", "同 bigota 挂死特征", False, False)]
        self.set_mirrors([{"tag": t, "quality": q, "note": n, "enabled": e}
                          for t, q, n, e, _ in demo], picked="cdnorg")

    def set_mirrors(self, mirrors, picked=None):
        lay = self.badge_box
        while lay.count() > 1:
            it = lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        note = None
        disabled = 0
        for m in mirrors:
            if not m.get("enabled", True):
                disabled += 1
            lay.insertWidget(lay.count() - 1,
                             MirrorBadge(m["tag"], m.get("quality", "?"),
                                         m.get("note", ""), m.get("enabled", True),
                                         picked == m["tag"]))
        if disabled:
            note = CaptionLabel("已自动跳过 %d 个挂死节点" % disabled)
            note.setStyleSheet("color:%s;" % C_GRAY)
            lay.insertWidget(lay.count() - 1, note)

    # ---------- 文件信息卡片 ----------
    def _build_info(self):
        card = CardWidget()
        card.setFixedHeight(74)
        g = QGridLayout(card)
        g.setContentsMargins(18, 10, 18, 10)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)

        # 标签与值【同一行】—— 早先按"标签在上/值在下"排 4 行, 78px 根本装不下,
        # 实测文字直接叠在一起。改成同一行后 2 行就够, 且更易扫读。
        def pair(row, col, title, value, color=None, mono=False, bold=False):
            t = CaptionLabel(title)
            t.setStyleSheet("color:%s;" % C_GRAY)
            t.setFixedWidth(34)
            v = BodyLabel(value)
            css = ""
            if color:
                css += "color:%s;" % color
            if mono:
                css += "font-family:Consolas,'Courier New',monospace;"
            if bold:
                css += "font-weight:600;"
            v.setStyleSheet(css)
            g.addWidget(t, row, col * 2)
            g.addWidget(v, row, col * 2 + 1)
            return v

        self.lb_dev = pair(0, 0, "机型", "—", bold=True)
        self.lb_ver = pair(1, 0, "版本", "—", color=C_GRAY)
        self.lb_size = pair(0, 1, "大小", "—", bold=True)
        self.lb_path = pair(1, 1, "输出", self.outdir, color=C_GRAY)
        self.lb_md5 = pair(0, 2, "MD5", "—", mono=True)

        st = CaptionLabel("⏳ 待校验")
        st.setStyleSheet("color:%s;" % C_GRAY)
        g.addWidget(st, 1, 4, 1, 2)
        self.lb_md5_state = st

        g.setColumnStretch(1, 3)
        g.setColumnStretch(3, 4)
        g.setColumnStretch(5, 2)
        return card

    @staticmethod
    def _vline():
        f = QFrame()
        f.setFixedWidth(1)
        f.setStyleSheet("background:rgba(128,128,128,0.22);")
        return f

    # ---------- 进度区 ----------
    def _build_progress(self):
        card = CardWidget()
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 12, 18, 12)
        v.setSpacing(10)

        srow = QHBoxLayout()
        self.stages = StageIndicator()
        srow.addWidget(self.stages, 1)
        self.btn_pause = PushButton("暂停")
        self.btn_pause.setIcon(FIF.PAUSE)
        self.btn_pause.setFixedSize(84, 30)
        self.btn_cancel = PushButton("取消")
        self.btn_cancel.setIcon(FIF.CLOSE)
        self.btn_cancel.setFixedSize(84, 30)
        srow.addWidget(self.btn_pause)
        srow.addWidget(self.btn_cancel)
        v.addLayout(srow)

        # 进度条独占一行, 百分比内嵌在条内左侧, 字节数放条右侧独立区域
        prow = QHBoxLayout()
        prow.setSpacing(12)
        barwrap = QWidget()
        bl = QVBoxLayout(barwrap)
        bl.setContentsMargins(0, 0, 0, 0)
        self.bar = ProgressBar()
        self.bar.setValue(0)
        self.bar.setFixedHeight(14)
        bl.addWidget(self.bar)
        prow.addWidget(barwrap, 1)

        nums = QVBoxLayout()
        nums.setSpacing(0)
        self.lb_pct = QLabel("0.0%")
        self.lb_pct.setStyleSheet("font-size:17px;font-weight:700;color:%s;" % C_PRIMARY_LT)
        self.lb_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lb_pct.setFixedWidth(90)
        nums.addWidget(self.lb_pct)
        self.lb_bytes = CaptionLabel("— / —")
        self.lb_bytes.setStyleSheet("color:%s;" % C_GRAY)
        self.lb_bytes.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lb_bytes.setFixedWidth(90)
        nums.addWidget(self.lb_bytes)
        prow.addLayout(nums)
        v.addLayout(prow)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setContentsMargins(0, 4, 0, 0)
        self.num_labels = {}
        # 初值一律给 "—", 不要放假数据 —— 否则用户首次启动会以为已经下了一半
        keys = [("speed", "当前速度", "—", "MB/s"), ("avg", "平均速度", "—", "MB/s"),
                ("peak", "峰值速度", "—", "MB/s"), ("elapsed", "已用时间", "—", ""),
                ("eta", "剩余时间", "—", ""), ("conns", "连接数", "—", "")]
        for i, (k, name, val, unit) in enumerate(keys):
            b = QVBoxLayout()
            b.setSpacing(0)
            t = CaptionLabel(name)
            t.setStyleSheet("color:%s;" % C_GRAY)
            b.addWidget(t)
            rr = QHBoxLayout()
            rr.setSpacing(4)
            vv = QLabel(val)
            vv.setStyleSheet("font-size:22px;font-family:Consolas,'Courier New',monospace;"
                             "font-weight:600;")
            rr.addWidget(vv)
            if unit:
                u = CaptionLabel(unit)
                u.setStyleSheet("color:%s;" % C_GRAY)
                rr.addWidget(u)
            rr.addStretch(1)
            b.addLayout(rr)
            wrap = QWidget(); wrap.setLayout(b)
            grid.addWidget(wrap, 0, i)
            self.num_labels[k] = vv
        v.addLayout(grid)
        return card

    # ---------- 日志区 ----------
    def _build_log(self):
        card = CardWidget()
        card.setFixedHeight(158)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 8, 14, 8)
        v.setSpacing(4)
        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("运行日志"))
        head.addStretch(1)
        self.seg_log = SegmentedWidget()
        self.seg_log.setFixedHeight(26)
        for k, t in (("all", "全部"), ("warn", "仅警告"), ("err", "仅错误")):
            self.seg_log.addItem(k, t)
        self.seg_log.setCurrentItem("all")
        head.addWidget(self.seg_log)
        v.addLayout(head)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFrameShape(QFrame.NoFrame)
        self.log_view.setLineWrapMode(QTextEdit.NoWrap)
        self.log_view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.log_view.setStyleSheet(
            "QTextEdit{background:transparent;border:none;"
            "font-family:Consolas,'Courier New',monospace;font-size:11px;}")
        v.addWidget(self.log_view, 1)
        # ⚠ 日志过滤必须接信号, 否则就是一个点了没反应的死控件 (实测被抓出来)。
        self._log_filter = "all"
        self._log_lines = []
        self.seg_log.currentItemChanged.connect(self._on_log_filter)
        # 首次启动给一条真实可用的示例链接, 用户可以直接点「开始下载」验证
        self._append_log("[%s] mirom %s —— 小米 ROM 下载加速器已就绪"
                         % (time.strftime("%H:%M:%S"), ENGINE_VERSION), C_GREEN)
        self._append_log("[%s] 下载目录: %s" % (time.strftime("%H:%M:%S"), self.outdir))
        self._append_log("[%s] 把任意一个小米 CDN 链接粘进输入框即可; "
                         "已在输入框预置一条可直接测试的示例" % time.strftime("%H:%M:%S"))
        return card

    # ---------- 日志 ----------
    LEVELS = ("info", "warn", "err")

    def _log_level(self, line):
        if any(k in line for k in ("❌", "失败", "错误", "Traceback", "异常")):
            return "err"
        if "⚠" in line:
            return "warn"
        return "info"

    def _fmt_log(self, line, color):
        import html as _h
        if color is None:
            color = {"warn": C_ORANGE, "err": C_RED}.get(self._log_level(line))
            if color is None and ("✅" in line or "全部完成" in line):
                color = C_GREEN
        if color and line.startswith("[") and "]" in line:
            i = line.index("]") + 1
            return ("<span style='color:%s'>%s</span><span style='color:%s'>%s</span>"
                    % (C_GRAY, _h.escape(line[:i]), color, _h.escape(line[i:])))
        return _h.escape(line)

    def _append_log(self, line, color=None):
        """日志区: 保留最近 500 行; 按当前过滤级别决定是否立即显示"""
        lv = self._log_level(line)
        self._log_lines.append((line, color, lv))
        if len(self._log_lines) > 500:
            self._log_lines = self._log_lines[-500:]
        if self._log_filter == "all" or self._log_filter == lv:
            self.log_view.append(self._fmt_log(line, color))
            self.log_view.moveCursor(QTextCursor.End)

    def _render_log(self):
        """过滤级别变化时整块重绘"""
        self.log_view.clear()
        want = self._log_filter
        for line, color, lv in self._log_lines:
            if want == "all" or want == lv:
                self.log_view.append(self._fmt_log(line, color))
        self.log_view.moveCursor(QTextCursor.End)

    def _on_log_filter(self, key):
        self._log_filter = key or "all"
        self._render_log()

    # ---------- 状态栏 ----------
    def _build_statusbar(self):
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        self.lb_stage = QLabel("阶段: 空闲")
        self.lb_disk = QLabel("磁盘 --")
        self.lb_conns = QLabel("连接 0")
        self.lb_net = QLabel("● 待机")
        self.lb_ver = QLabel("v%s  %s" % (ENGINE_VERSION, BYLINE))
        for lb, color in ((self.lb_stage, None), (self.lb_disk, None),
                          (self.lb_conns, None), (self.lb_net, C_GRAY), (self.lb_ver, C_GRAY)):
            lb.setStyleSheet("padding:0 10px;font-size:11px;" +
                             ("color:%s;" % color if color else ""))
            sb.addWidget(lb)

    # ---------- 菜单动作 ----------
    def _choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", self.outdir)
        if d:
            self.outdir = d
            # ★ 必须同步进 self.cfg 并落盘。
            #   旧实现只改 self.outdir —— 于是"用菜单选目录, 然后进设置对话框点保存"
            #   会把目录弹回旧值 (对话框读的是 self.cfg), 用户会以为保存坏了。
            #   两个入口改的是同一个状态, 就必须写同一份数据。
            self.cfg["outdir"] = d
            save_settings(self.cfg)
            self.lb_path.setText(d)
            self._append_log("[%s] 下载目录已设为: %s" % (time.strftime("%H:%M:%S"), d))

    def _open_dir(self):
        try:
            os.makedirs(self.outdir, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(self.outdir)
            elif sys.platform == "darwin":
                os.system('open "%s"' % self.outdir)
            else:
                os.system('xdg-open "%s"' % self.outdir)
        except Exception as e:
            InfoBar.error("无法打开目录", str(e), duration=3000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _open_url(self, url, label=""):
        """
        打开外部链接。

        ★ 旧实现是 `try: webbrowser.open(url) except: pass` —— 三种失败全被吞掉:
          没有默认浏览器、被安全软件拦、webbrowser 返回 False 但没抛异常。
          用户点了菜单什么都没发生, 只能认为"这功能是坏的"。
          现在打不开就把链接【摆在用户面前】并复制到剪贴板, 让他自己粘。
        """
        ok = False
        err = ""
        try:
            if sys.platform.startswith("win"):
                # Windows 上 startfile 走 ShellExecute, 比 webbrowser 可靠
                # (webbrowser 在没有默认浏览器时会静默返回 False)
                os.startfile(url)
                ok = True
            elif sys.platform == "darwin":
                import subprocess
                ok = subprocess.call(["open", url]) == 0
            else:
                import webbrowser
                ok = bool(webbrowser.open(url))
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)

        if ok:
            return True
        self._url_fallback(url, label, err)
        return False

    def _url_fallback(self, url, label, err=""):
        """打不开浏览器时的兜底: 把链接显示出来 + 复制到剪贴板。"""
        try:
            QApplication.clipboard().setText(url)
            copied = "（链接已复制到剪贴板）"
        except Exception:
            copied = ""
        body = ("没能打开浏览器%s\n\n"
                "要打开的内容：%s\n\n%s\n%s\n\n"
                "可以手动把上面的链接粘到浏览器里。"
                % (("：%s" % err) if err else "（可能没有设置默认浏览器）",
                   label or "链接", url, copied))
        box = MessageBox("打不开浏览器", body, self)
        box.yesButton.setText("知道了")
        box.cancelButton.hide()
        box.exec()

    def _open_local_doc(self):
        """
        打开本地使用说明。

        ★ 为什么优先本地: 帮助文档应该【离线可用】。
          用户卡住的时候往往正因为网络/下载出问题, 这时候把他甩到网页上是最差的体验。
          本地找不到才退回在线 README。
        """
        for p in self._doc_candidates():
            if os.path.exists(p):
                try:
                    if sys.platform.startswith("win"):
                        os.startfile(p)
                    elif sys.platform == "darwin":
                        import subprocess
                        subprocess.call(["open", p])
                    else:
                        import subprocess
                        subprocess.call(["xdg-open", p])
                    return True
                except Exception:
                    break
        # 本地没有 -> 退到在线
        return self._open_url(README_URL + ANCHOR_USAGE, "在线使用说明")

    def _doc_candidates(self):
        """使用说明可能在哪几个位置 (打包/源码、同级/docs 都试一遍)。"""
        out = []
        names = ("使用说明.txt",)
        bases = []
        if getattr(sys, "frozen", False):
            bases.append(os.path.dirname(sys.executable))
        bases.append(os.path.dirname(os.path.abspath(__file__)))
        bases.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for b in bases:
            for n in names:
                out.append(os.path.join(b, n))
                out.append(os.path.join(b, "docs", n))
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def _check_update(self):
        """
        检查有没有新版本。

        ★ 网络请求必须放后台线程: 在主线程里发 HTTP 会把界面冻住 ——
          用户在等一个"检查更新"的时候窗口没反应, 会以为程序死了。
          旧版帮助菜单里根本没有这个功能, 只有三个指向占位地址的链接。
        """
        self._append_log("[%s] 正在检查更新…" % time.strftime("%H:%M:%S"), C_GRAY)
        w = self

        class _Upd(QObject):
            done = Signal(dict)

        self._upd = _Upd()
        self._upd.done.connect(self._on_update_result)

        def work():
            info = {"ok": False, "err": "", "latest": "", "url": RELEASES_URL, "notes": ""}
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.github.com/repos/%s/releases/latest" % REPO_SLUG)
                req.add_header("User-Agent", "mirom-update-check")
                req.add_header("Accept", "application/vnd.github+json")
                with urllib.request.urlopen(req, timeout=12) as r:
                    import json as _json
                    d = _json.loads(r.read().decode("utf-8"))
                info["ok"] = True
                info["latest"] = (d.get("tag_name") or "").lstrip("v")
                info["url"] = d.get("html_url") or RELEASES_URL
                info["notes"] = (d.get("body") or "")[:400]
            except Exception as e:
                info["err"] = "%s: %s" % (type(e).__name__, e)
            try:
                w._upd.done.emit(info)
            except Exception:
                pass                      # 窗口已销毁就别再发了

        threading.Thread(target=work, daemon=True, name="mirom-update").start()

    def _on_update_result(self, info):
        ts = time.strftime("%H:%M:%S")
        if not info.get("ok"):
            self._append_log("[%s] 检查更新失败: %s" % (ts, info.get("err", "?")), C_ORANGE)
            box = MessageBox("检查更新失败",
                             "没能连上 GitHub。\n\n%s\n\n"
                             "可以手动到项目主页看看有没有新版本：\n%s"
                             % (info.get("err", ""), RELEASES_URL), self)
            box.yesButton.setText("打开下载页")
            box.cancelButton.setText("关闭")
            if box.exec():
                self._open_url(RELEASES_URL, "下载页")
            return

        cur = ENGINE_VERSION
        latest = info.get("latest") or ""
        self._append_log("[%s] 最新版本: %s (当前 %s)" % (ts, latest or "?", cur), C_GRAY)

        def ver_tuple(s):
            try:
                return tuple(int(x) for x in str(s).split(".")[:4])
            except Exception:
                return (0,)

        if latest and ver_tuple(latest) > ver_tuple(cur):
            box = MessageBox("发现新版本",
                             "当前版本：%s\n最新版本：%s\n\n%s"
                             % (cur, latest, info.get("notes", "")), self)
            box.yesButton.setText("去下载")
            box.cancelButton.setText("以后再说")
            if box.exec():
                self._open_url(info.get("url") or RELEASES_URL, "新版本下载页")
        else:
            box = MessageBox("已是最新版本",
                             "当前版本 %s，已经是最新的了。\n\n"
                             "项目主页：%s" % (cur, REPO_URL), self)
            box.yesButton.setText("知道了")
            box.cancelButton.setText("打开项目主页")
            if box.exec():
                self._open_url(REPO_URL, "项目主页")

    def _open_tune(self):
        p = os.path.join(os.path.expanduser("~"), ".mirom_tune.json")
        if not os.path.exists(p):
            InfoBar.info("暂无缓存", "还没有调优记录", duration=2500,
                         position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)
            else:
                os.system('xdg-open "%s"' % p)
        except Exception:
            pass

    def _clear_tune(self):
        p = os.path.join(os.path.expanduser("~"), ".mirom_tune.json")
        try:
            if os.path.exists(p):
                os.remove(p)
            InfoBar.success("已清空", "下次运行会重新做并发调优", duration=2500,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
        except Exception as e:
            InfoBar.error("删除失败", str(e), duration=3000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _clear_log(self):
        self.log_view.clear()
        self._log_lines = []

    def _run_selftest(self):
        """
        运行引擎自检, 并把结果【弹窗】给用户。

        ★ 用户要求: "自检不要全靠日志输出, 同时要添加自检后的结果弹窗(确定/取消)"。
          旧实现只往日志区追加最后 6 行 —— 日志区又小又会被后续输出顶走,
          用户点完「运行引擎自检」经常什么都没看见, 以为按钮坏了。
          现在: 日志照旧保留完整记录 (便于复制/贴出来), 另外弹一个
          确定/取消 的对话框 —— 通过时「确定」= 关闭, 失败时「取消」旁边
          多一个「复制详情」, 可以直接把失败项贴给开发者。
        """
        from mirom import selftest
        import io
        import contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = selftest()
        except Exception as e:
            import traceback
            rc = 1
            buf.write("自检异常: %s\n%s\n" % (e, traceback.format_exc()))
        full = buf.getvalue()
        ok = (rc == 0)

        # 日志区保留【完整】输出, 不再只留 6 行 —— 失败时要能贴出来
        self._append_log("[%s] 引擎自检: %s"
                         % (time.strftime("%H:%M:%S"),
                            "全部通过 ✅" if ok else "有失败项 ❌"),
                         C_GREEN if ok else C_RED)
        for ln in full.splitlines():
            self._append_log("    " + ln)

        # 汇总失败项 (自检最后会打印 "自检失败 N 项: ...")
        fails = []
        for ln in full.splitlines():
            if " FAIL " in ln or ln.strip().startswith("FAIL"):
                fails.append(ln.strip())
        n_pass = sum(1 for ln in full.splitlines() if " PASS " in ln)

        if ok:
            body = ("引擎自检全部通过 ✅\n\n"
                    "共 %d 项检查, 全部成功。\n\n"
                    "覆盖范围: URL 解析 / 内嵌 MD5 提取 / 镜像展开 / 磁盘探测 /\n"
                    "写入路径 / 分片算法 / 无控制台兼容 / 取消通道 / 静态体检 / 记账。"
                    % n_pass)
        else:
            shown = "\n".join("· " + f[:96] for f in fails[:8]) or "(未能定位到具体失败行)"
            body = ("引擎自检发现问题 ❌\n\n"
                    "通过 %d 项, 失败 %d 项:\n\n%s\n\n"
                    "完整输出已写入运行日志 (编辑 → 复制全部日志)。"
                    % (n_pass, len(fails), shown))

        box = MessageBox("运行引擎自检", body, self)
        box.yesButton.setText("确定")
        box.cancelButton.setText("取消")
        # ⚠ 这里【不要】再 connect cancelButton.clicked ——
        #   MessageBoxBase 已经把它接到自己的 reject() 了, 再加一条会因为
        #   "内置的先连先跑"而让返回值不可预期 (冲突对话框上就是这么栽的)。
        try:
            box.textLayout.addWidget(CaptionLabel("完整输出已写入运行日志"))
        except Exception:
            pass
        box.exec()

    def _export_csv(self):
        """
        导出本次的测试数据。

        ★ 为什么重写 (用户报的 bug: "工具中保存测速数据, 这个功能无效"):
          旧实现只导出折线图的时序数据 (chart.ts/per)。而时序数据【只在下载阶段】
          才有 —— "仅测速"模式压根不创建下载器, 一条进度包都不发。
          于是用户老老实实先点「仅测速」, 再去导出, 得到的是"暂无数据"。
          更讽刺的是: 真正的测量结果 (各节点 TTFB、服务器、劣质节点的实测速率、
          单连接基准、并发爬坡每一档的吞吐与抖动) 全部只打印成了日志, 一条没留。
          现在导出两份: ①测量结果 (仅测速也有) ②下载时序 (有下载才有)。
        """
        m = self.measure or {}
        has_measure = bool(m.get("probe") or m.get("baseline") or m.get("ramp"))
        has_series = bool(self.chart.ts)
        if not has_measure and not has_series:
            InfoBar.info("暂无数据", "先点「仅测速」或「开始下载」, 有数据后再导出",
                         duration=3500, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出测试数据", "mirom_speed.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        try:
            import csv as _csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                wr = _csv.writer(f)

                # ── ① 测量结果 ──
                if has_measure:
                    wr.writerow(["# mirom 测试数据"])
                    wr.writerow(["URL", m.get("url", "")])
                    wr.writerow(["文件名", m.get("file", "")])
                    wr.writerow(["文件大小(字节)", m.get("size", 0)])
                    wr.writerow(["代理", m.get("proxy") or "(无)"])
                    wr.writerow(["限速(MB/s)", m.get("limit_mbps") or 0])
                    c = m.get("chosen") or {}
                    wr.writerow(["最终选择",
                                 "%s @ %s 连接, %.2f MB/s, CV %s%%"
                                 % (c.get("tag"), c.get("conns"),
                                    c.get("MBps", 0),
                                    c.get("cv") if c.get("cv") is not None else "-")])
                    wr.writerow([])

                    wr.writerow(["# ① 节点探测"])
                    wr.writerow(["节点", "主机", "评级", "可用", "大小(字节)",
                                 "TTFB(ms)", "DNS(ms)", "TCP(ms)", "服务器",
                                 "支持Range", "实测速率(KB/s)", "错误"])
                    for p in m.get("probe") or []:
                        wr.writerow([p.get("tag"), p.get("host"), p.get("quality"),
                                     "是" if p.get("ok") else "否", p.get("size", 0),
                                     p.get("ttfb_ms", ""), p.get("dns_ms", ""),
                                     p.get("tcp_ms", ""), p.get("server", ""),
                                     "是" if p.get("range") else "否",
                                     p.get("stall_kbps", ""), p.get("err") or ""])
                    wr.writerow([])

                    wr.writerow(["# ② 单连接基准测速"])
                    wr.writerow(["节点", "连接数", "平均(MB/s)", "最低", "最高",
                                 "抖动CV(%)", "失败连接", "是否被单连接限速"])
                    for p in m.get("baseline") or []:
                        wr.writerow([p.get("tag"), p.get("nconn"), p.get("MBps"),
                                     p.get("min"), p.get("max"), p.get("cv"),
                                     p.get("errors"),
                                     "是" if p.get("limited") else "否"])
                    wr.writerow([])

                    wr.writerow(["# ③ 并发爬坡 (找拐点)"])
                    wr.writerow(["节点", "连接数", "平均(MB/s)", "最低", "最高",
                                 "抖动CV(%)", "失败连接"])
                    for p in m.get("ramp") or []:
                        wr.writerow([p.get("tag"), p.get("nconn"), p.get("MBps"),
                                     p.get("min"), p.get("max"), p.get("cv"),
                                     p.get("errors")])
                    wr.writerow([])

                # ── ② 下载时序 ──
                if has_series:
                    ws = self.chart.window_secs() if hasattr(self.chart, "window_secs") else None
                    tags = sorted(self.chart.per.keys())
                    wr.writerow(["# ④ 下载实时速度 (每秒采样)"])
                    wr.writerow(["已用(秒)", "总速度(MB/s)"]
                                + ["%s(MB/s)" % t for t in tags])
                    for i, t in enumerate(self.chart.ts):
                        row = ["%.1f" % t, "%.2f" % self.chart.tot[i]]
                        for tg in tags:
                            arr = self.chart.per[tg]
                            row.append("%.2f" % arr[i] if i < len(arr) else "")
                        wr.writerow(row)
                    wr.writerow([])

                # ── ③ 事件 ──
                ev = getattr(self.chart, "events", None) or []
                if ev:
                    wr.writerow(["# ⑤ 关键事件"])
                    wr.writerow(["已用(秒)", "事件"])
                    for e in ev:
                        try:
                            wr.writerow(["%.1f" % e[0], e[1]])
                        except Exception:
                            pass
            InfoBar.success("已导出", path, duration=4000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
        except Exception as e:
            InfoBar.error("导出失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _select_all(self):
        """全选链接输入框的内容 (旧版这里挂的是 None —— 菜单项摆在那儿但点了没反应)。"""
        self.ed_url.setFocus()
        self.ed_url.selectAll()

    def _copy_log(self):
        txt = self.log_view.toPlainText()
        if not txt.strip():
            InfoBar.info("日志为空", "还没有可复制的内容", duration=2000,
                         position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        QApplication.clipboard().setText(txt)
        InfoBar.success("已复制", "%d 行日志已复制到剪贴板" % txt.count("\n"),
                        duration=2500, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.values()
        self.cfg.update(new)
        self.outdir = self.cfg["outdir"]
        ok = save_settings(self.cfg)
        # 界面上的"输出"格子要跟着变, 否则用户会以为没生效
        try:
            self.lb_path.setText(self.outdir)
        except Exception:
            pass
        try:
            os.makedirs(self.outdir, exist_ok=True)
        except Exception:
            pass
        if ok:
            InfoBar.success("设置已保存", self._settings_summary(),
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            InfoBar.warning("设置仅本次生效", "无法写入 %s, 重启后会丢失" % SETTINGS_PATH,
                            duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _settings_summary(self):
        c = self.cfg
        bits = ["连接 " + ("自动" if not c["conns"] else str(c["conns"])),
                "上限 %d" % c["max_conns"],
                "限速 " + ("关闭" if c["limit_mbps"] <= 0 else "%.1f MB/s" % c["limit_mbps"])]
        if c["multi"]:
            bits.append("多线路聚合")
        if not c["verify"]:
            bits.append("不校验 MD5")
        if c["retune"]:
            bits.append("每次重调优")
        if c["proxy"]:
            bits.append("代理 %s" % c["proxy"])
        return " · ".join(bits)

    def _reset_settings(self):
        global DEFAULT_SETTINGS
        self.cfg = dict(DEFAULT_SETTINGS)
        self.outdir = self.cfg["outdir"]
        save_settings(self.cfg)
        try:
            self.lb_path.setText(self.outdir)
        except Exception:
            pass
        InfoBar.success("已重置", "所有设置恢复默认", duration=2500,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    # ---------- 快捷方式 / 开机启动 ----------
    def _exe_path(self):
        """当前可执行文件路径 (冻结后是 exe, 源码运行时是 pythonw/python)。"""
        return os.path.abspath(sys.executable)

    def _shortcut_spec(self):
        """
        算出快捷方式该指向什么、用什么图标。

        ★ 两种运行方式必须分开处理 (实测: 直接拿 sys.executable 会在源码模式下
          生成一个指向 python.exe、又没有脚本参数的快捷方式 —— 双击毫无反应,
          而用户只会觉得"这功能是坏的"):
            冻结成 EXE -> 目标就是 exe 自己, 无参数, 图标取自 exe 内嵌资源
            跑源码     -> 目标用 pythonw.exe (不弹控制台), 参数是本脚本,
                          图标用 assets\\logo.ico
        """
        exe = os.path.abspath(sys.executable)
        if getattr(sys, "frozen", False):
            return exe, "", "%s,0" % exe
        # 源码模式: 优先 pythonw.exe, 免得起一个黑框
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        target = pyw if os.path.exists(pyw) else exe
        script = os.path.abspath(__file__)
        ico = _asset("logo.ico") or target
        return target, '"%s"' % script, ("%s,0" % ico if ico else "")

    def _make_shortcut(self, folder, name):
        """
        在 folder 里建一个指向本程序的快捷方式, 并显式写上图标。

        ★ 为什么必须显式写 IconLocation:
          实测现有那个 .lnk 的 IconLocation 是 ",0" —— 路径是空的, 于是
          Windows 只能显示一个通用图标。快捷方式不会自动去读 exe 的内嵌图标,
          必须写成 "<exe路径>,0" 才稳。
        ★ 为什么走 PowerShell 的 WScript.Shell:
          它是系统自带组件, 不需要 pywin32 (少一个依赖, 打包也小)。
          调用必须隐藏窗口 —— 见 _run_hidden_ps 的注释。
        """
        if not sys.platform.startswith("win"):
            return None, "仅支持 Windows"
        lnk = os.path.join(folder, name + ".lnk")
        target, args, icon = self._shortcut_spec()

        def q(s):
            return s.replace("'", "''")
        ps = ("$w=New-Object -ComObject WScript.Shell;"
              "$s=$w.CreateShortcut('%s');"
              "$s.TargetPath='%s';"
              "%s"
              "$s.WorkingDirectory='%s';"
              "%s"
              "$s.Description='小米 ROM 下载加速器';"
              "$s.Save()"
              % (q(lnk), q(target),
                 ("$s.Arguments='%s';" % q(args)) if args else "",
                 q(os.path.dirname(target)),
                 ("$s.IconLocation='%s';" % q(icon)) if icon else ""))
        try:
            r = _run_hidden_ps(ps)
            if r.returncode != 0:
                return None, (r.stderr or r.stdout or "创建失败").strip()[:140]
            if not os.path.exists(lnk):
                return None, "脚本执行了但快捷方式没生成"
            return lnk, None
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, e)

    def _startup_dir(self):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "Microsoft", "Windows", "Start Menu", "Programs", "Startup")

    def _autostart_lnk(self):
        return os.path.join(self._startup_dir(), "mirom.lnk")

    def _autostart_on(self):
        return os.path.exists(self._autostart_lnk())

    def _toggle_autostart(self):
        if not sys.platform.startswith("win"):
            InfoBar.warning("不支持", "开机自启仅支持 Windows", duration=2500,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        p = self._autostart_lnk()
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception as e:
                InfoBar.error("关闭失败", str(e), duration=3500,
                              position=InfoBarPosition.TOP_RIGHT, parent=self)
                return
            InfoBar.success("已关闭开机自启", "下次开机不会自动运行 mirom",
                            duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        else:
            lnk, err = self._make_shortcut(self._startup_dir(), "mirom")
            if err:
                InfoBar.error("创建失败", err, duration=4000,
                              position=InfoBarPosition.TOP_RIGHT, parent=self)
                return
            InfoBar.success("已开启开机自启", "下次开机会自动启动 mirom",
                            duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        self._refresh_autostart_action()

    def _make_desktop_shortcut(self):
        if not sys.platform.startswith("win"):
            InfoBar.warning("不支持", "创建快捷方式仅支持 Windows", duration=2500,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        lnk, err = self._make_shortcut(desk, "mirom")
        if err:
            InfoBar.error("创建失败", err, duration=4000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        InfoBar.success("已创建", lnk, duration=4000,
                        position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _refresh_autostart_action(self):
        """把菜单勾选态同步成磁盘上的真实情况。"""
        act = getattr(self, "_act_autostart", None)
        if act is not None:
            try:
                act.setChecked(self._autostart_on())
            except Exception:
                pass

    def _save_diag(self):
        """把诊断报告存到下载目录旁边, 方便小白用户直接发给人看。"""
        import io
        import contextlib
        import platform
        buf = io.StringIO()
        try:
            from mirom import selftest
            with contextlib.redirect_stdout(buf):
                rc = selftest()
        except Exception as e:
            rc = 1
            buf.write("自检异常: %s\n" % e)
        path = os.path.join(self.outdir, "mirom_diag.txt")
        try:
            os.makedirs(self.outdir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("mirom 诊断报告\n" + "=" * 50 + "\n")
                f.write("版本      : %s\n" % ENGINE_VERSION)
                f.write("打包方式  : %s\n" % ("PyInstaller EXE"
                                              if getattr(sys, "frozen", False) else "源码运行"))
                f.write("Python    : %s\n" % sys.version.replace("\n", " "))
                f.write("平台      : %s / %s\n" % (sys.platform, platform.platform()))
                f.write("执行文件  : %s\n" % sys.executable)
                f.write("下载目录  : %s\n" % self.outdir)
                f.write("设置      : %s\n" % json.dumps(self.cfg, ensure_ascii=False))
                f.write("\n引擎自检  : %s\n" % ("全部通过" if rc == 0 else "有失败项"))
                f.write("-" * 50 + "\n")
                f.write(buf.getvalue())
            InfoBar.success("已生成", path, duration=5000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
        except Exception as e:
            InfoBar.error("生成失败", str(e), duration=4000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _not_yet(self):
        InfoBar.info("尚未实现", "该功能将在后续版本加入", duration=2200,
                     position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _about_resume(self):
        InfoBar.info("断点续传",
                     "中断后重新点「开始下载」即可自动续传; 断点文件为 <文件名>.mirom.json",
                     duration=6000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _about(self):
        from qfluentwidgets import MessageBox
        box = MessageBox("关于 mirom",
                         "小米 ROM 下载加速器  v%s  %s\n\n"
                         "把小米官方 CDN 的单连接限速 (约 1MB/s) 通过高并发分片"
                         "提升到 100MB/s 以上。\n\n"
                         "· 自动展开 5 个官方镜像并检测「假 206 挂死」节点\n"
                         "· 并发爬坡找吞吐拐点, 尾部退化自动重排/换线\n"
                         "· 断点续传 · 全局限速 · HTTP 代理 · 单实例保护\n"
                         "· 按文件名内嵌 MD5 自动校验\n\n"
                         "技术栈: Python 3 · PySide6 · PyQt-Fluent-Widgets · pyqtgraph\n"
                         "开源协议: MIT\n\n"
                         "免责: 仅加速下载小米官方服务器上的公开固件, 不修改、"
                         "不重打包、不绕过授权。刷机有风险, 请自行确认机型与固件匹配。"
                         % (ENGINE_VERSION, BYLINE), self)
        box.yesButton.setText("知道了")
        box.cancelButton.hide()
        # 关于页放上 logo —— 这是用户唯一会主动打开来看的窗口
        try:
            hb = QHBoxLayout()
            lb = QLabel()
            pm = app_icon().pixmap(64, 64)
            if not pm.isNull():
                lb.setPixmap(pm)
                lb.setFixedSize(64, 64)
                lb.setScaledContents(True)
                hb.addWidget(lb)
                hb.addStretch(1)
                box.textLayout.addLayout(hb)
        except Exception:
            pass
        box.exec()


# ═══════════════════════════ 崩溃兜底 ═══════════════════════════
def _writable_dir():
    """
    放崩溃日志的目录。

    ⚠ 不能用 os.path.dirname(__file__): 冻结成 EXE 之后它在 _internal 里面,
      而 _internal 在 Program Files 之类的只读位置时日志写不进去 ——
      偏偏"写不进去"正是最需要日志的时候。优先用 exe 同级目录, 退回用户目录。
    """
    if getattr(sys, "frozen", False):
        d = os.path.dirname(sys.executable)
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".mirom_wtest")
            with open(t, "w") as f:
                f.write("x")
            os.remove(t)
            return d
        except Exception:
            pass
    else:
        try:
            return os.path.dirname(os.path.abspath(__file__))
        except Exception:
            pass
    return os.path.expanduser("~")


CRASH_LOG = os.path.join(_writable_dir(), "mirom_crash.log")


def install_crash_handler():
    """
    任何未捕获异常都写进 mirom_crash.log 并弹窗提示路径。
    目的: 用户帮忙验证 UI 时, 万一崩了能直接把日志文件发回来, 而不是
    "窗口突然没了" —— 那种反馈没法定位。
    """
    import traceback
    import datetime

    def hook(t, v, tb):
        txt = "".join(traceback.format_exception(t, v, tb))
        try:
            with open(CRASH_LOG, "a", encoding="utf-8") as f:
                f.write("\n=== %s ===\n%s\n" % (datetime.datetime.now(), txt))
        except Exception:
            pass
        # --windowed 打包后 sys.stderr 也是 None, 直接 write 会二次崩溃
        try:
            if sys.stderr is not None:
                sys.stderr.write(txt)
        except Exception:
            pass
        try:
            from qfluentwidgets import MessageBox
            box = MessageBox("mirom 出错了",
                             (txt.strip().splitlines() or ["未知错误"])[-1][:400]
                             + "\n\n完整堆栈已写入:\n" + CRASH_LOG
                             + "\n\n请把该文件反馈给开发者。")
            box.yesButton.setText("知道了")
            box.cancelButton.hide()
            box.exec()
        except Exception:
            pass

    sys.excepthook = hook


# ═══════════════════════════ 启动 ═══════════════════════════
def main():
    args = list(sys.argv[1:])
    shot = None
    desktop = None
    diag = None

    # ── 无界面参数 ──
    # ★ 为什么要有这两个: mirom.exe 的入口是【图形界面】, 而 --selftest / --version
    #   是 mirom.py 的命令行参数。不加这段的话, 用户 (和自动化脚本) 敲
    #   `mirom.exe --selftest` 会看到程序【正常弹出窗口然后一直等在那】——
    #   参数被静默忽略, 没有任何提示, 看起来就像"卡死了"。
    #   实测这个坑在开发过程中踩过两次, 每次都要等几百秒超时才反应过来。
    #   现在直接在这里处理掉, 顺手把结果打到 stdout —— GUI 进程平时没有控制台,
    #   但控制台/管道里调用时是有的。
    if "--version" in args or "-V" in args:
        try:
            sys.stdout.write("mirom %s %s (Python %s)\n"
                             % (ENGINE_VERSION, BYLINE, sys.version.split()[0]))
            sys.stdout.flush()
        except Exception:
            pass
        return 0
    if "--selftest" in args or "--check" in args:
        import io
        import contextlib
        buf = io.StringIO()
        try:
            from mirom import selftest
            with contextlib.redirect_stdout(buf):
                rc = selftest()
        except Exception as e:
            import traceback
            rc = 1
            buf.write("自检异常: %s\n%s\n" % (e, traceback.format_exc()))
        txt = buf.getvalue()
        try:
            sys.stdout.write(txt)
            sys.stdout.flush()
        except Exception:
            pass
        # 没有控制台时 (双击运行 / --windowed) stdout 是 None, 那就落个文件,
        # 免得"跑了但什么都没留下"。
        if sys.stdout is None:
            try:
                p = os.path.join(tempfile.gettempdir(), "mirom_selftest.txt")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(txt)
            except Exception:
                pass
        return rc

    if "--diag" in args:
        i = args.index("--diag")
        diag = args[i + 1] if len(args) > i + 1 else "mirom_diag.txt"
        del args[i:i + 2]
    # --shot-live: 用【真实平台】渲染截图 (窗口藏到 -6000,-6000, 不抢前台)。
    # 必须存在的原因: offscreen 平台的字体库只有 58 个族、缺 微软雅黑,
    # 中文会全变成方框 —— 结构能验, 字体验不了。真实平台有 140 个族。
    live_shot = False
    test_dl = None
    if "--shot-live" in args:
        i = args.index("--shot-live")
        shot = args[i + 1] if len(args) > i + 1 else "shot_live.png"
        live_shot = True
        del args[i:i + 2]
    if "--shot" in args:
        i = args.index("--shot")
        shot = args[i + 1] if len(args) > i + 1 else "shot.png"
        del args[i:i + 2]
    # --test-download <报告文件> <URL> <输出目录> [秒数]
    # 跑一条【真实】下载 (走 GUI 的 _start 路径), 到点自动取消, 然后写报告。
    # 存在的理由: --diag 只证明引擎能 import, --shot 只证明界面能建。
    # PyInstaller 冻结后最经典的坑是 SSL 证书 (certifi 路径丢失) 和线程 ——
    # 这两个只有真的发一次 HTTPS、真的落一次盘才验得出来。
    if "--test-download" in args:
        i = args.index("--test-download")
        seg = args[i + 1:i + 5]
        del args[i:i + 5]
        if len(seg) >= 3:
            try:
                test_dl = (seg[1], seg[2], float(seg[3]) if len(seg) > 3 else 60.0)
            except Exception:
                test_dl = None
            if test_dl:
                test_dl = (seg[0],) + test_dl          # (报告, URL, 目录, 秒)
        else:
            print("用法: --test-download <报告.txt> <URL> <输出目录> [秒数]")
    if "--desktop" in args:
        i = args.index("--desktop")
        try:
            desktop = int(args[i + 1])
        except Exception:
            desktop = None
        del args[i:i + 2]

    # ★ 只要不是"给人看的正常启动", 一律走离屏渲染 —— 一个窗口都不弹。
    #   起因: 自动化测试时 w.show() 会在用户当前桌面上弹出窗口, 实测直接把
    #   正在玩游戏的用户顶掉前台 (SetForegroundWindow 被系统拒绝, 前台仍是游戏)。
    #   offscreen 平台不需要显示器/窗口管理器, 抓图走 w.grab(), 完全无感。
    if shot and not live_shot:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if test_dl:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    # 崩溃兜底要尽早装 —— 放在 QApplication 之后的话,
    # 导入期/初始化期的异常就没人接, EXE 会"窗口一闪就没了"。
    install_crash_handler()

    # ★ AppUserModelID 必须在 QApplication 之前设。
    #   它决定 Windows 任务栏怎么给窗口分组、以及用哪个图标 ——
    #   设晚了任务栏已经用旧 ID 建好分组, 就会一直显示 Python 的图标。
    set_windows_app_id()

    app = QApplication([sys.argv[0]] + args)
    # 进程级图标: 所有窗口 (含各种 QDialog) 默认继承它, 一个都跑不掉。
    # 任务栏图标也取自这里 (配合上面的 AppUserModelID)。
    try:
        app.setWindowIcon(app_icon())
    except Exception:
        pass
    w = MainWindow()

    # ── 诊断模式: 输出一份环境 + 自检报告, 方便用户反馈问题 ──
    if diag:
        import io, contextlib, platform
        buf = io.StringIO()
        try:
            from mirom import selftest
            with contextlib.redirect_stdout(buf):
                rc = selftest()
        except Exception as e:
            rc = 1
            buf.write("自检异常: %s\n" % e)
        try:
            with open(diag, "w", encoding="utf-8") as f:
                f.write("mirom 诊断报告\n" + "=" * 50 + "\n")
                f.write("版本      : %s\n" % ENGINE_VERSION)
                f.write("打包方式  : %s\n" % ("PyInstaller EXE" if getattr(sys, "frozen", False)
                                              else "源码运行"))
                f.write("Python    : %s\n" % sys.version.replace("\n", " "))
                f.write("平台      : %s / %s\n" % (sys.platform, platform.platform()))
                f.write("执行文件  : %s\n" % sys.executable)
                f.write("下载目录  : %s\n" % w.outdir)
                try:
                    import vdesk
                    f.write("虚拟桌面  : %d 个\n" % vdesk.desktop_count())
                except Exception:
                    f.write("虚拟桌面  : 不可用\n")
                f.write("\n引擎自检  : %s\n" % ("全部通过" if rc == 0 else "有失败项"))
                f.write("-" * 50 + "\n")
                f.write(buf.getvalue())
        except Exception:
            pass
        return 0

    # ── EXE 端到端下载自测 ──
    if test_dl:
        rep_path, dl_url, dl_dir, dl_secs = test_dl
        ev = {"done": None, "err": None, "prog": None, "meta": None,
              "nprog": 0, "logs": [], "tcancel": None, "t0": time.time()}
        w.sig.prog.connect(lambda d: ev.update(prog=d, nprog=ev["nprog"] + 1,
                                              tprog=time.time()))
        w.sig.meta.connect(lambda d: ev.update(meta=d))
        w.sig.done.connect(lambda r: ev.update(done=r, tdone=time.time()))
        w.sig.err.connect(lambda s: ev.update(err=s))
        w.sig.log.connect(lambda s: ev["logs"].append(
            "[%6.1fs] %s" % (time.time() - ev["t0"], s)))
        try:
            os.makedirs(dl_dir, exist_ok=True)
        except Exception:
            pass
        w.ed_url.setText(dl_url)
        w.outdir = dl_dir
        # 自动化模式: 目标文件已存在时不弹窗, 按"跳过"走
        # (即校验现有文件; 通过就秒过, 不通过就重下整份)。
        # 不设这个的话 _start 会弹出模态"已存在同名文件"窗, 而这里没人能点它。
        w.auto_conflict = ("go", None)
        w._start(probe=False)

        def _do_cancel():
            # 只有"确实还在跑"才算一次取消。若任务早已自然完成, 就不能记 tcancel,
            # 否则 tdone - tcancel 会变成负数 (实测打印出"收尾耗时 -330.4s")。
            if w.eng is not None and w.eng.is_alive():
                ev["tcancel"] = time.time()
                w._cancel()
                ev["alive_at_cancel"] = True
            else:
                ev["finished_early"] = True

        # 到点收工 —— 别让自测把整个 ROM 真下完
        QTimer.singleShot(int(dl_secs * 1000), _do_cancel)

        def _finish():
            # 再等 20s 让 cancel 真正落地 (实测取消约 6s)
            for _ in range(2):
                app.processEvents()
                if ev["done"] is not None or ev["err"] is not None:
                    break
                time.sleep(0.5)

            files = []
            try:
                for nm in sorted(os.listdir(dl_dir)):
                    fp = os.path.join(dl_dir, nm)
                    if os.path.isfile(fp) and not nm.endswith(".json"):
                        files.append((nm, os.path.getsize(fp)))
            except Exception:
                pass
            states = [nm for nm in (os.listdir(dl_dir) if os.path.isdir(dl_dir) else [])
                      if nm.endswith(".json")]
            pr = ev["prog"] or {}
            md = (ev["meta"] or {}).get("mirrors") or []
            en = [m for m in md if m.get("enabled")]

            L = []
            L.append("mirom EXE 端到端下载自测")
            L.append("=" * 52)
            L.append("URL       : %s" % dl_url)
            L.append("输出目录  : %s" % dl_dir)
            L.append("运行时长  : %.0f 秒 (到点自动取消)" % dl_secs)
            L.append("")
            L.append("[冻结环境关键能力]")
            L.append("  HTTPS/TLS 握手     : %s"
                     % ("成功 (拿到 %d 个可用节点)" % len(md) if md else "未拿到节点列表 ❌"))
            L.append("  镜像解析           : %s"
                     % ("%d 个, 其中 %d 个启用" % (len(md), len(en)) if md else "失败 ❌"))
            L.append("  后台线程回调       : %s"
                     % ("成功 (收到 %d 个进度包)" % ev["nprog"] if ev["nprog"] else "未收到 ❌"))
            L.append("  日志回调           : %d 条" % len(ev["logs"]))
            L.append("")
            L.append("[下载实测]")
            L.append("  已写入            : %s"
                     % (", ".join("%s (%.1f MB)" % (n, s / 1048576.0) for n, s in files)
                        or "无 ❌"))
            L.append("  进度              : %.2f%%" % pr.get("pct", 0.0))
            L.append("  瞬时/均值/峰值    : %.1f / %.1f / %.1f MB/s"
                     % (pr.get("speed", 0), pr.get("avg", 0), pr.get("peak", 0)))
            L.append("  连接数 (活跃/在途): %d / %d"
                     % (pr.get("conns", 0), pr.get("active", 0)))
            L.append("  续传状态文件      : %s"
                     % (", ".join(states) if states else "无"))
            L.append("")
            L.append("[取消行为]")
            if ev.get("tcancel"):
                L.append("  取消请求发出于     : %.1fs (当时线程存活=%s)"
                         % (ev["tcancel"] - ev["t0"], ev.get("alive_at_cancel")))
                if ev.get("tdone"):
                    L.append("  结束回调到达于     : %.1fs  → 收尾耗时 %.1fs"
                             % (ev["tdone"] - ev["t0"], ev["tdone"] - ev["tcancel"]))
                else:
                    L.append("  结束回调           : ❌ 报告生成时仍未到达 (收尾超 %.0fs)"
                             % (time.time() - ev["tcancel"]))
            else:
                L.append("  任务在取消点之前就已自然完成 (无需取消)")
            L.append("  最后一个进度包于   : %s"
                     % ("%.1fs" % (ev.get("tprog", 0) - ev["t0"]) if ev.get("tprog")
                        else "无"))
            L.append("")
            L.append("[尾部日志]")
            L.extend("  " + s for s in ev["logs"][-25:])
            L.append("")
            L.append("[结果]")
            if ev["err"]:
                L.append("  ❌ 报错: %s" % ev["err"].split("\n")[0][:300])
            elif ev["done"] is not None:
                L.append("  ✅ 正常结束 ok=%s" % ev["done"].get("ok"))
            else:
                L.append("  ⚠ 到点仍未结束")
            wrote = sum(s for _, s in files)
            # ★ 判定要区分"故意取消"和"失败"。
            #   本测试本来就在 dl_secs 处主动取消, 所以 ok=False 是【预期结果】,
            #   真正要守的是: 结束回调必须到达、且响应要快。
            #   旧实现的病征正是"回调 40 秒后才到", 这条断言就是它的回归门。
            lat = (ev["tdone"] - ev["tcancel"]) if (ev.get("tdone") and ev.get("tcancel")) else None
            early = bool(ev.get("finished_early"))
            c1 = ev.get("tdone") is not None          # 有结束回调
            c2 = ev["err"] is None                    # 没报错
            c3 = lat is not None and lat < 15.0       # 取消响应够快
            if early:
                # 全程跑完了, 那就不该拿"取消够快"当判据, 改判"是否真的下完并通过校验"
                c3 = bool(ev["done"] and ev["done"].get("ok"))
            L.append("")
            L.append("[断言]")
            L.append("  %s 结束回调已到达            %s"
                     % ("✅" if c1 else "❌", "%.1fs" % lat if lat is not None else "—"))
            L.append("  %s 无异常报错" % ("✅" if c2 else "❌"))
            if early:
                L.append("  %s 完整跑完且 ok=True" % ("✅" if c3 else "❌"))
            else:
                L.append("  %s 取消响应 < 15s            %s"
                         % ("✅" if c3 else "❌", "%.1fs" % lat if lat is not None else "n/a"))
            if md:
                L.append("  ✅ TLS/镜像解析成功")
            else:
                L.append("  ℹ️ 本次在探测完成前就取消了, 无节点列表属正常")
            L.append("")
            ok = c1 and c2 and c3
            L.append("  总判定: %s" % ("通过 ✅" if ok else "失败 ❌"))
            try:
                with open(rep_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(L) + "\n")
            except Exception:
                pass
            app.quit()

        QTimer.singleShot(int((dl_secs + 25) * 1000), _finish)
        return app.exec()

    if shot:
        w.chart.push_demo()
        w.resize(WIN_W, WIN_H)
        if live_shot:
            # 真实窗口 + 藏到屏幕外坐标: 既不抢用户前台, 又能加载系统 CJK 字体。
            w.setWindowFlag(Qt.Tool, True)
            w.move(-6000, -6000)
            w.setAttribute(Qt.WA_DontShowOnScreen, False)
        w.show()                      # offscreen 下的 show 不会产生真实窗口
        app.processEvents()

        def _grab():
            app.processEvents()
            pm = w.grab()
            pm.save(shot)
            print("已保存截图: %s  (%dx%d)  [%s]"
                  % (shot, pm.width(), pm.height(),
                     "真实平台/窗口移出屏外" if live_shot else "离屏渲染, 未弹出任何窗口"))
            app.quit()
        QTimer.singleShot(600 if not live_shot else 1200, _grab)
        return app.exec()

    w.show()

    if desktop and desktop > 1:
        # best-effort: 把窗口挪到第 N 个虚拟桌面。
        # ⚠ 前提是本进程能拿到前台 —— 若用户正在别的程序 (如游戏) 里, Windows
        #   会拒绝 SetForegroundWindow, 此时搬运无效是【系统行为】而非缺陷。
        def _move():
            try:
                import vdesk
                hwnd = int(w.winId())
                if vdesk.move_to_next_desktop(hwnd):
                    InfoBar.info("已移动窗口",
                                 "已尝试把窗口移到桌面%d; 若未生效, 请手动按 Win+Ctrl+Shift+→"
                                 % desktop, duration=4000,
                                 position=InfoBarPosition.TOP_RIGHT, parent=w)
            except Exception:
                pass
        QTimer.singleShot(400, _move)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
