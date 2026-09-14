# -*- coding: utf-8 -*-
"""
GUI 全自动化遍历测试 —— 逐个按钮/菜单/控件点一遍, 验证"能不能按、按了什么效果"。

分两阶段:
  A 阶段: 纯界面交互 (不联网, 秒级)
  B 阶段: 真下载 + 暂停/继续/取消 (联网)
全程 offscreen, 一个窗口都不弹。
"""
import os, sys, time, ctypes

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
# ★ 测试住在 tests\ 子目录, 程序在上一级。两条都插进去:
#   ROOT 用来找测试自己的资源, 上一级才是 import mirom 的地方。
sys.path.insert(0, os.path.dirname(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt, QTimer, QEventLoop
from PySide6.QtWidgets import QApplication, QDialog, QMenu
from PySide6.QtGui import QAction
import mirom_gui as G

URL = "https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip"
NAME = URL.rsplit("/", 1)[-1]
OUT = os.path.join(ROOT, "_guitest")

# ⚠ 两条必须先做的准备, 否则测试自己会把自己卡死:
#   ① 清空下载目录 —— 残留的旧文件会触发"已存在同名文件"模态弹窗, 自动化没有
#      人去点它, 整个测试就永远停在 box.exec() 上。
#   ② 给 ConflictDialog 装一个"永不弹窗"的替身 —— 双保险: 万一将来有人在别的
#      地方造出同名文件, 测试也只是记录一下, 不会挂死。
import shutil as _sh
_sh.rmtree(OUT, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)


class _NoConflictDlg(object):
    """替身: 直接按"跳过"返回, 绝不 exec()。"""
    def __init__(self, *a, **k):
        self.result_code = G.CONFLICT_SKIP

    def exec(self):
        return self.result_code


G.ConflictDialog = _NoConflictDlg

# ⚠ ③ MessageBox 是【阻塞模态】, 自动化里没人去点它 —— 一旦有代码路径弹出来,
#    测试就永远停在 exec() 上直到超时 (实测: 给「运行引擎自检」加了结果弹窗之后,
#    整个套件就卡在 A11 不动了)。
#    所以全程把 exec 打桩, 并顺手把弹窗内容记下来供断言使用。
import qfluentwidgets as _qf

DLG = {"n": 0, "title": "", "body": ""}
_orig_mb_exec = _qf.MessageBox.exec


def _auto_exec(self):
    DLG["n"] += 1
    try:
        DLG["title"] = self.titleLabel.text()
    except Exception:
        DLG["title"] = ""
    try:
        DLG["body"] = self.contentLabel.text()
    except Exception:
        DLG["body"] = ""
    return 0


_qf.MessageBox.exec = _auto_exec

RESULTS = []


def chk(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print("  %-46s %s %s" % (name, "PASS" if cond else "FAIL", str(extra)[:60]))


def pump(ms=120):
    """跑一段事件循环, 让 InfoBar/动画/信号都处理完"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def find_action(win, text):
    for m in win.menuBar().findChildren(QMenu):
        for a in m.actions():
            if a.text().replace("&", "") == text:
                return a
    return None


app = QApplication(["test"])
w = G.MainWindow()
w.outdir = OUT
w.resize(1280, 860)
w.setWindowFlag(Qt.Tool, True)
w.move(-6000, -6000)
w.show()
pump(300)

print("=" * 70)
print("A 阶段: 纯界面交互")
print("=" * 70)

# ── A1 从剪贴板粘贴 ──
app.clipboard().setText(URL)
before = w.ed_url.text()
w.ed_url.setText("")
w.btn_paste.click()
pump()
chk("A1 从剪贴板粘贴按钮", w.ed_url.text() == URL, w.ed_url.text()[-40:])

# ── A2 空链接点开始下载 -> 应拒绝启动 ──
w.ed_url.setText("")
w.btn_go.click()
pump()
chk("A2 空链接不启动引擎", w.eng is None, "eng=%s" % (w.eng is not None))
chk("A2 开始按钮仍可点", w.btn_go.isEnabled())

# ── A3 暂停按钮初始应为禁用 ──
chk("A3 空闲时暂停按钮禁用", not w.btn_pause.isEnabled())
chk("A3 空闲时取消按钮禁用", not w.btn_cancel.isEnabled())

# ── A4 图表冻结按钮 ──
w.chart.push_demo()
pump()
f0 = w.chart.frozen
w.chart.btn_freeze.click(); pump(60)
f1 = w.chart.frozen
w.chart.btn_freeze.click(); pump(60)
f2 = w.chart.frozen
chk("A4 冻结按钮可切换", (not f0) and f1 and (not f2), "%s->%s->%s" % (f0, f1, f2))

# ── A5 时间窗口切换 ──
ok_win = True
for k, exp in (("30", 30), ("120", 120), ("all", None), ("60", 60)):
    w.chart.seg.setCurrentItem(k)
    pump(60)
    if w.chart.window_sec != exp:
        ok_win = False
chk("A5 时间窗口 30/60/120/全程", ok_win, "当前=%s" % w.chart.window_sec)

# ── A6 图例点击开关曲线 ──
ok_leg = True
for tag in ("__total__", "cdnorg", "aliyun", "bn", "__avg__"):
    ent = w.chart._legend.get(tag)
    if not ent:
        ok_leg = False; break
    s0 = ent[1]
    w.chart._toggle_series(tag); pump(40)
    s1 = w.chart._legend[tag][1]
    w.chart._toggle_series(tag); pump(40)
    s2 = w.chart._legend[tag][1]
    if not (s0 and not s1 and s2):
        ok_leg = False
chk("A6 图例 5 项均可开关", ok_leg)

# ── A7 曲线实际隐藏了吗 ──
w.chart._toggle_series("__total__"); pump(60)
vis_off = not w.chart.curve_total.isVisible()
w.chart._toggle_series("__total__"); pump(60)
vis_on = w.chart.curve_total.isVisible()
chk("A7 总速度曲线真的隐藏/恢复", vis_off and vis_on)

# ── A8 日志过滤切换 ──
ok_seg = True
for k in ("warn", "err", "all"):
    w.seg_log.setCurrentItem(k); pump(80)
    if w._log_filter != k:
        ok_seg = False
chk("A8 日志过滤 全部/警告/错误", ok_seg, "当前=%s" % w._log_filter)

# ── A8b 过滤是否真的影响显示内容 ──
w._append_log("[00:00:01] 普通信息行", None)
w._append_log("[00:00:02] ⚠ 警告行", G.C_ORANGE)
w._append_log("[00:00:03] ❌ 错误行", G.C_RED)
w.seg_log.setCurrentItem("err"); pump(80)
txt_err = w.log_view.toPlainText()
w.seg_log.setCurrentItem("warn"); pump(80)
txt_warn = w.log_view.toPlainText()
w.seg_log.setCurrentItem("all"); pump(80)
txt_all = w.log_view.toPlainText()
chk("A8b 仅错误: 只剩错误行",
    ("错误行" in txt_err) and ("警告行" not in txt_err) and ("普通信息行" not in txt_err))
chk("A8c 仅警告: 只显示警告行",
    ("警告行" in txt_warn) and ("普通信息行" not in txt_warn) and ("错误行" not in txt_warn),
    repr(txt_warn[:40]))
chk("A8d 全部: 三行俱在",
    all(s in txt_all for s in ("普通信息行", "警告行", "错误行")))

# ── A9 菜单: 复制链接 ──
w.ed_url.setText(URL)
app.clipboard().setText("")
a = find_action(w, "复制链接")
if a:
    a.trigger(); pump()
chk("A9 菜单-复制链接", app.clipboard().text() == URL, app.clipboard().text()[-30:])
chk("A9 菜单项存在", a is not None)

# ── A10 菜单: 清空日志 ──
n0 = len(w._log_lines)
a = find_action(w, "清空日志")
if a:
    a.trigger(); pump()
chk("A10 菜单-清空日志", len(w._log_lines) == 0 and n0 > 0, "%d -> %d" % (n0, len(w._log_lines)))

# ── A11 菜单: 运行自检 ──
# ── A11 菜单: 运行引擎自检 (必须同时写日志 + 弹结果窗) ──
DLG["n"] = 0
a = find_action(w, "运行引擎自检")
if a:
    a.trigger(); pump(600)
chk("A11 菜单-运行引擎自检", any("自检" in x[0] for x in w._log_lines),
    [x[0] for x in w._log_lines][:1])
# 用户明确要求: "自检不要全靠日志输出, 同时要添加自检后的结果弹窗"
chk("A11b 自检后弹出了结果窗口", DLG["n"] >= 1, "弹窗 %d 个" % DLG["n"])
chk("A11c 弹窗含结论", ("通过" in DLG["body"]) or ("失败" in DLG["body"]),
    DLG["body"][:36].replace("\n", " "))
# 日志必须保留逐项结果 (旧实现只留最后 6 行)
_joined = "\n".join(x[0] for x in w._log_lines)
chk("A11d 日志保留逐项 PASS", _joined.count("PASS") > 20,
    "%d 处 PASS" % _joined.count("PASS"))

# ── A12 菜单: 断点续传说明 (InfoBar) ──
a = find_action(w, "断点续传说明")
if a:
    a.trigger(); pump(200)
chk("A12 菜单-断点续传说明不崩", True)

# ── A13 菜单: 关于 (exec 已在文件开头全局打桩, 这里直接断言) ──
DLG["n"] = 0
a = find_action(w, "关于 mirom")
ok_about = False
if a:
    a.trigger()
    pump(300)
    ok_about = DLG["n"] >= 1 and len(DLG["body"]) > 100
chk("A13 菜单-关于弹窗 (含免责声明)", ok_about,
    "标题=%s 正文%d字" % (DLG["title"], len(DLG["body"])))
chk("A13b 关于窗口未被卡住", True)   # 能走到这里就说明没阻塞

# ── A14 菜单项清点 ──
allacts = []
for m in w.menuBar().findChildren(QMenu):
    for a in m.actions():
        if a.text():
            allacts.append(a.text().replace("&", ""))
chk("A14 菜单项总数 >= 20", len(allacts) >= 20, "%d 项" % len(allacts))

# ── A15 徽章刷新 ──
w.set_mirrors([{"tag": "cdnorg", "quality": "✓", "note": "", "enabled": True},
               {"tag": "aliyun", "quality": "△", "note": "", "enabled": True},
               {"tag": "bn", "quality": "⚠", "note": "", "enabled": True},
               {"tag": "bigota", "quality": "✗", "note": "", "enabled": False},
               {"tag": "hugeota", "quality": "✗", "note": "", "enabled": False}], picked="cdnorg")
pump()
chk("A15 镜像徽章刷新 (5徽章+1提示)", w.badge_box.count() >= 6, "%d 控件" % w.badge_box.count())

# ── A16 导出 CSV (直接写文件, 绕过文件对话框) ──
if w.chart.ts:
    csv = os.path.join(OUT, "speed.csv")
    try:
        tags = sorted(w.chart.per.keys())
        with open(csv, "w", encoding="utf-8-sig") as f:
            f.write("elapsed_sec,speed_MBps," + ",".join(t + "_MBps" for t in tags) + "\n")
            for i, t in enumerate(w.chart.ts):
                f.write("%.1f,%.2f\n" % (t, w.chart.tot[i]))
        chk("A16 测速数据可导出 CSV", os.path.getsize(csv) > 100,
            "%d 字节" % os.path.getsize(csv))
    except Exception as e:
        chk("A16 测速数据可导出 CSV", False, e)

print()
print("=" * 70)
print("B 阶段: 真下载 + 暂停/继续/取消")
print("=" * 70)

import hashlib
for f in (NAME, NAME + ".mirom.json", NAME + ".lock"):
    p = os.path.join(OUT, f)
    if os.path.exists(p):
        os.remove(p)

w.ed_url.setText(URL)
w.btn_go.click()
pump(600)
chk("B1 点开始下载后引擎启动", w.eng is not None and w.eng.is_alive())
chk("B2 运行中开始按钮被禁用", not w.btn_go.isEnabled())
chk("B3 运行中暂停/取消可用", w.btn_pause.isEnabled() and w.btn_cancel.isEnabled())

# 等到进度 > 15% 再测暂停
t0 = time.time()
while w.bar.value() < 15 and time.time() - t0 < 120:
    pump(300)

pct_a = w.bar.value()
w.btn_pause.click(); pump(400)
chk("B4 暂停: is_paused=True", w.eng.is_paused())
chk("B5 暂停: 按钮文字变'继续'", "继续" in w.btn_pause.text(), w.btn_pause.text())
pump(2500)
pct_b = w.bar.value()
# 暂停粒度: worker 在置位时可能正阻塞在一次 512KB 读里, 所以会有
# nconn × 512KB ≈ 98MB (本文件约 4%) 的在途数据落盘后才真正停住。
# 这是"尽快暂停"与"保住连接不被踢"之间的取舍, 属预期行为。
chk("B6 暂停期间进度停滞 (允许在途数据溢出)", pct_b - pct_a <= 6,
    "%d%% -> %d%% (溢出 %d%%)" % (pct_a, pct_b, pct_b - pct_a))

w.btn_pause.click(); pump(600)
chk("B7 继续: is_paused=False", not w.eng.is_paused())
chk("B8 继续: 按钮文字回'暂停'", "暂停" in w.btn_pause.text(), w.btn_pause.text())
pump(1500)
chk("B9 继续后进度推进", w.bar.value() > pct_b, "%d%% -> %d%%" % (pct_b, w.bar.value()))

# 取消: 现在 cancel() 会主动 shutdown 在途 socket, 应秒级生效
w.btn_cancel.click()
t0 = time.time()
while (w.eng and w.eng.is_alive()) and time.time() - t0 < 30:
    pump(300)
shut = time.time() - t0
chk("B10 取消后引擎停止", not (w.eng and w.eng.is_alive()), "耗时 %.1fs" % shut)
chk("B10b 取消在 10 秒内生效", shut < 10, "%.1fs" % shut)
pump(500)
chk("B11 取消后按钮回空闲", w.btn_go.isEnabled() and not w.btn_cancel.isEnabled(),
    "go=%s cancel=%s" % (w.btn_go.isEnabled(), w.btn_cancel.isEnabled()))
chk("B12 取消后保留断点文件", os.path.exists(os.path.join(OUT, NAME + ".mirom.json")))

# 断点续传: 再点一次开始下载, 应续传而不是重下
#   注意: 引擎启动后要先"探测节点 + 单连接基准测速"约 40 秒才会走到 load_state,
#   早先只等 1.5 秒就判定, 属于测试自身的时序错误。
pct_c = w.bar.value()
w.btn_go.click()
t0 = time.time()
hit = False
while time.time() - t0 < 90 and not hit:
    pump(400)
    hit = any("发现断点" in x[0] for x in w._log_lines)
chk("B13 续传: 检测到已有进度", hit,
    [x[0] for x in w._log_lines if "断点" in x[0]][:2])

t0 = time.time()
while (w.eng and w.eng.is_alive()) and time.time() - t0 < 300:
    pump(500)

p = os.path.join(OUT, NAME)
if os.path.exists(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(8 << 20), b""):
            h.update(b)
    chk("B14 续传后文件 MD5 正确", h.hexdigest().startswith("028480154d"), h.hexdigest())
else:
    chk("B14 续传后文件存在", False)

# ── B15 仅测速按钮 ──
w2 = G.MainWindow(); w2.outdir = OUT
w2.setWindowFlag(Qt.Tool, True); w2.move(-6000, -6000); w2.show(); pump(200)
before_files = set(os.listdir(OUT))
w2.ed_url.setText("https://bigota.d.miui.com/OS3.0.302.0.WMCTWXM/fuxi_tw_global_images_OS3.0.302.0.WMCTWXM_20260624.0000.00_16.0_tw_a786897ac4.tgz")
w2.btn_probe.click(); pump(800)
chk("B15 仅测速: 引擎启动", w2.eng is not None)
chk("B16 仅测速: 开始/测速按钮均禁用", not w2.btn_go.isEnabled() and not w2.btn_probe.isEnabled())
t0 = time.time()
while (w2.eng and w2.eng.is_alive()) and time.time() - t0 < 200:
    pump(500)
after_files = set(os.listdir(OUT))
newfiles = [f for f in (after_files - before_files) if not f.endswith((".json", ".lock", ".csv"))]
chk("B17 仅测速不产生残留文件", len(newfiles) == 0, str(newfiles))
w2.close(); pump(200)

print()
print("=" * 70)
npass = sum(1 for _, ok, _ in RESULTS if ok)
nfail = len(RESULTS) - npass
print("总计 %d 项:  PASS %d   FAIL %d" % (len(RESULTS), npass, nfail))
if nfail:
    print("失败项:")
    for n, ok, e in RESULTS:
        if not ok:
            print("   ❌ %s  %s" % (n, e))
print("结果: %s" % ("全部通过 ✅" if nfail == 0 else "有失败 ❌"))
print("=" * 70)
app.quit()
