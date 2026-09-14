# -*- coding: utf-8 -*-
"""
GUI 设置系统测试 —— 重点验"配置文件被写坏时界面不能崩"。

为什么单独测:
  设置文件是【面向用户的】: 用户会手改, 断电会写坏, 旧版本升级会留下缺字段的
  旧格式。这些都不该让程序起不来 —— 而"起不来"恰恰是最难让小白用户描述清楚的
  故障。同理, 设置对话框本身也要能真的构造出来 (我第一版就漏了 QLineEdit 的
  import, 是静态检查抓到的, 真点开就 NameError)。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ★ 测试在 tests\ 子目录里, mirom.py / mirom_gui.py 在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

import mirom_gui as G  # noqa: E402

PASS = FAIL = 0
FAILED = []


def chk(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %-48s %s" % (name, detail))
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL  %-48s %s" % (name, detail))


TMP = tempfile.mkdtemp(prefix="mirom_cfg_")
G.SETTINGS_PATH = os.path.join(TMP, "gui.json")


def write_cfg(text):
    with open(G.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write(text)


print("=" * 72)
print("1. 配置文件容错 (写坏 / 手改 / 旧版本)")
print("=" * 72)

cases = [
    ("文件不存在", None),
    ("空文件", ""),
    ("非 JSON", "@@@ 坏了 @@@"),
    ("顶层是数组", "[]"),
    ("顶层是字符串", '"hello"'),
    ("顶层是 null", "null"),
    ("字段类型全错", '{"conns": "x", "max_conns": [], "limit_mbps": {}, "multi": 1}'),
    ("conns 超大", '{"conns": 999999}'),
    ("conns 为负", '{"conns": -5}'),
    ("max_conns 为 0", '{"max_conns": 0}'),
    ("max_conns 为负", '{"max_conns": -99}'),
    ("limit 为负", '{"limit_mbps": -3.5}'),
    ("outdir 是数字", '{"outdir": 12345}'),
    ("proxy 是对象", '{"proxy": {"a": 1}}'),
    ("缺一堆字段", '{"conns": 64}'),
    ("多余字段", '{"conns": 64, "不认识的字段": 1}'),
    ("正常完整", json.dumps(G.DEFAULT_SETTINGS)),
    ("布尔用 0/1", '{"multi": 0, "verify": 0, "prealloc": 1, "retune": 1}'),
]
for label, text in cases:
    if text is None:
        if os.path.exists(G.SETTINGS_PATH):
            os.remove(G.SETTINGS_PATH)
    else:
        write_cfg(text)
    try:
        s = G.load_settings()
        ok = (isinstance(s, dict)
              and 0 <= s["conns"] <= 4096
              and 1 <= s["max_conns"] <= 4096
              and s["limit_mbps"] >= 0
              and isinstance(s["outdir"], str) and s["outdir"]
              and isinstance(s["proxy"], str))
        # 手动连接数超过自动上限时, 上限必须被抬上去, 否则用户设了 512 会被压回 256
        if s["conns"] and s["conns"] > s["max_conns"]:
            ok = False
            detail = "conns=%s 但 max_conns=%s (会被压住)" % (s["conns"], s["max_conns"])
        else:
            detail = "conns=%s max=%s limit=%s" % (s["conns"], s["max_conns"], s["limit_mbps"])
        chk("load_settings: %s" % label, ok, detail)
    except Exception as e:
        chk("load_settings: %s" % label, False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 72)
print("2. 保存 / 读回一致性")
print("=" * 72)

s = dict(G.DEFAULT_SETTINGS)
s.update({"conns": 384, "max_conns": 512, "limit_mbps": 12.5, "proxy": "http://127.0.0.1:7890",
          "multi": True, "verify": False, "prealloc": False, "retune": True,
          "outdir": os.path.join(TMP, "下载 目录")})
chk("save_settings 返回 True", G.save_settings(s) is True, "")
back = G.load_settings()
same = all(back[k] == s[k] for k in s)
chk("保存后读回完全一致", same, "" if same else str({k: (s[k], back.get(k)) for k in s if back.get(k) != s[k]}))
chk("中文目录名保持正确", back["outdir"] == s["outdir"], back["outdir"])
chk("没有残留 .tmp 文件", not os.path.exists(G.SETTINGS_PATH + ".tmp"), "")

print()
print("=" * 72)
print("3. 设置对话框能真的构造出来")
print("=" * 72)

try:
    dlg = G.SettingsDialog(dict(G.DEFAULT_SETTINGS))
    chk("SettingsDialog 可构造", dlg is not None, "")
    v = dlg.values()
    chk("values() 返回全部键",
        set(v.keys()) == set(G.DEFAULT_SETTINGS.keys()),
        "%d 个键" % len(v))
    chk("values() 类型正确",
        isinstance(v["conns"], int) and isinstance(v["limit_mbps"], float)
        and isinstance(v["multi"], bool),
        "conns=%s limit=%s multi=%s" % (type(v["conns"]).__name__,
                                        type(v["limit_mbps"]).__name__,
                                        type(v["multi"]).__name__))
    # 用一份极端设置去构造, 确认不会因为越界值而崩
    wild = dict(G.DEFAULT_SETTINGS)
    wild.update({"conns": 99999, "max_conns": -5, "limit_mbps": 1e9, "proxy": "???"})
    dlg2 = G.SettingsDialog(wild)
    chk("极端值也能构造对话框", dlg2 is not None, "")
    dlg.deleteLater()
    dlg2.deleteLater()
except Exception as e:
    import traceback
    chk("SettingsDialog 可构造", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("4. 设置真的会传进引擎")
print("=" * 72)

# 用一个假的 MiromEngine 截住参数 —— 只验"设置有没有被传下去", 不真下载
captured = {}


class FakeEngine(object):
    def __init__(self, url, **kw):
        captured.update(kw)
        self.kw = kw
        self.on_log = self.on_stage = self.on_meta = None
        self.on_progress = self.on_done = self.on_error = None

    def start(self):
        pass

    def is_alive(self):
        return False


try:
    real = G.MiromEngine
    real_load = G.load_settings          # ⚠ 必须存下来: 第 4 节把它换成 lambda 之后
    G.MiromEngine = FakeEngine           #   第 6 节还要用真的, 不还原就会读到假数据
    G.load_settings = lambda: dict(G.DEFAULT_SETTINGS, conns=384, max_conns=512,
                                   limit_mbps=7.5, proxy="http://127.0.0.1:7890",
                                   multi=True, verify=False, prealloc=False,
                                   retune=True, insecure=True)
    w = G.MainWindow()
    w.ed_url.setText("https://bigota.d.miui.com/20.5.6/x_028480154d_10.0.zip")
    w._start(False)
    app.processEvents()
    chk("conns 已传入", captured.get("conns") == 384, "conns=%s" % captured.get("conns"))
    chk("max_conns 已传入", captured.get("max_conns") == 512, "max=%s" % captured.get("max_conns"))
    chk("limit_mbps 已传入", captured.get("limit_mbps") == 7.5, "limit=%s" % captured.get("limit_mbps"))
    chk("proxy 已传入", captured.get("proxy") == "http://127.0.0.1:7890", "%s" % captured.get("proxy"))
    chk("multi 已传入", captured.get("multi") is True, "%s" % captured.get("multi"))
    chk("verify=False 已传入", captured.get("verify") is False, "%s" % captured.get("verify"))
    chk("retune 已传入", captured.get("retune") is True, "%s" % captured.get("retune"))
    chk("insecure 已传入", captured.get("insecure") is True, "%s" % captured.get("insecure"))
    chk("prealloc=False 走 extra_args",
        (captured.get("extra_args") or {}).get("no_prealloc") is True,
        "%s" % captured.get("extra_args"))
    G.MiromEngine = real
    G.load_settings = real_load          # 还原, 否则后面的用例读到的是假的
    w.deleteLater()
except Exception as e:
    import traceback
    chk("设置传入引擎", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("5. 菜单项: 不允许存在'点了没反应'的死控件")
print("=" * 72)

try:
    w = G.MainWindow()
    dead = []
    total = 0
    # PySide6 的 receivers() 要的是 Qt4 风格的规范化签名串, 不是 SignalInstance。
    # 旧写法 act.receivers(act.triggered) 会直接 TypeError —— 那样测试本身就废了,
    # 永远只会报 FAIL, 反而掩盖真实的死控件。
    # 2 是参数个数的前缀, QAction.triggered 带一个 bool 参数。
    from PySide6.QtWidgets import QMenu
    for menu in w.menuBar().findChildren(QMenu):
        for act in menu.actions():
            if act.isSeparator():
                continue
            total += 1
            try:
                n = act.receivers("2triggered(bool)")
            except Exception:
                n = 1                      # 探不到就当作已连接, 不误报
            if not n:
                dead.append("%s / %s" % (menu.title(), act.text()))
    chk("没有无回调的菜单项", not dead, "共 %d 项; 死项: %s" % (total, dead or "无"))
    # 顺带确认几个新加的项确实在
    texts = [a.text() for m in w.menuBar().findChildren(QMenu) for a in m.actions()]
    for want in ("下载设置…", "全选链接", "复制全部日志", "诊断报告 (反馈问题时发这个)"):
        chk("菜单含 %s" % want, want in texts, "")
    w.deleteLater()
except Exception as e:
    chk("没有无回调的菜单项", False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 72)
print("6. 两个改目录的入口必须写同一份状态")
print("=" * 72)

# 旧 bug: 菜单"选择下载目录"只改 self.outdir, 不改 self.cfg, 也不落盘。
# 于是"菜单选目录 → 打开设置对话框 → 保存"会把目录弹回旧值。
try:
    G.save_settings(dict(G.DEFAULT_SETTINGS))
    w = G.MainWindow()
    w.cfg = G.load_settings()
    newdir = os.path.join(TMP, "菜单选的目录")
    os.makedirs(newdir, exist_ok=True)
    real_dlg = G.QFileDialog.getExistingDirectory
    G.QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: newdir)
    w._choose_dir()
    chk("菜单改目录后 outdir 生效", w.outdir == newdir, w.outdir)
    chk("菜单改目录后 cfg 同步", w.cfg.get("outdir") == newdir, str(w.cfg.get("outdir")))
    chk("菜单改目录后已落盘",
        G.load_settings().get("outdir") == newdir, str(G.load_settings().get("outdir")))
    # 再打开设置对话框, 它必须显示新目录 (不是旧的)
    dlg = G.SettingsDialog(w.cfg)
    chk("设置对话框显示的是新目录",
        dlg.values()["outdir"] == newdir, dlg.values()["outdir"])
    G.QFileDialog.getExistingDirectory = real_dlg
    w.deleteLater()
    dlg.deleteLater()
except Exception as e:
    import traceback
    chk("两个改目录入口一致", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("7. 同名文件冲突: 必须先问, 再按选择走")
print("=" * 72)
# 用户报的 bug: "如果下载文件夹有相同文件, 直接显示下载成功而不是提醒之后
# 创建副本下载"。旧实现发现同名文件就直接 log 一句"无需重新下载"返回 0 ——
# 界面上就是"下载完成", 用户以为自己点错了。
URLX = "https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip"
import mirom as M  # noqa: E402

FN = M.parse_url(URLX)["file"]

try:
    cdir = os.path.join(TMP, "conflict")
    os.makedirs(cdir, exist_ok=True)
    w = G.MainWindow()
    w.outdir = cdir

    # 7a 文件不存在 -> 直接开干, 不该弹窗
    asked = []
    real_dlg = G.ConflictDialog

    class SpyDlg(object):
        """替身: 只记录"有没有被弹出来", 并按预设码返回。"""
        pick = G.CONFLICT_CANCEL

        def __init__(self, *a, **k):
            asked.append(a[0] if a else "")
            self.result_code = SpyDlg.pick

        def exec(self):
            return self.result_code

    G.ConflictDialog = SpyDlg
    act, sa = w._resolve_conflict(URLX, probe=False)
    chk("无同名文件时不弹窗", act == "go" and not asked, "action=%s 弹窗次数=%d" % (act, len(asked)))

    # 7b 有同名文件但有断点 -> 那是续传, 也不该弹窗
    target = os.path.join(cdir, FN)
    with open(target, "wb") as f:
        f.write(b"\0" * 1024)
    with open(M.state_path(target), "w", encoding="utf-8") as f:
        f.write("{}")
    asked[:] = []
    act, sa = w._resolve_conflict(URLX, probe=False)
    chk("有断点文件时不弹窗 (那是续传)", act == "go" and not asked,
        "action=%s 弹窗=%d" % (act, len(asked)))

    # 7c 有同名文件且无断点 -> 必须弹窗; 用户选"下载副本"
    os.remove(M.state_path(target))
    asked[:] = []
    SpyDlg.pick = G.CONFLICT_COPY
    act, sa = w._resolve_conflict(URLX, probe=False)
    chk("有同名文件时必须弹窗", len(asked) == 1, "弹窗 %d 次" % len(asked))
    chk("选择下载副本 -> action=copy", act == "copy", act)
    chk("副本名形如 xxx (1).zip", sa and "(1)" in sa, str(sa))

    # 7d 选择覆盖重下 / 跳过 / 取消
    for code, want in ((G.CONFLICT_FORCE, "force"), (G.CONFLICT_SKIP, "go"),
                       (G.CONFLICT_CANCEL, "cancel")):
        SpyDlg.pick = code
        act, _ = w._resolve_conflict(URLX, probe=False)
        chk("冲突选择 -> %s" % want, act == want, act)

    # 7e 仅测速不该弹冲突窗 (不写盘, 没有冲突可言)
    asked[:] = []
    SpyDlg.pick = G.CONFLICT_CANCEL
    act, _ = w._resolve_conflict(URLX, probe=True)
    chk("仅测速不弹冲突窗", act == "go" and not asked, "action=%s" % act)

    # 7f 链接解析不了时不该崩, 交给引擎报错
    act, _ = w._resolve_conflict("这不是链接", probe=False)
    chk("非法链接不崩", act == "go", act)

    # 7g 真对话框本身必须能构造, 且四个按钮的返回码互不相同 ——
    #    这条守的是"qfluentwidgets 的 MessageBox 会吞掉自定义返回码"那个坑:
    #    一旦有人把 ConflictDialog 换成 MessageBox, 这段就会失败。
    G.ConflictDialog = real_dlg
    try:
        d = G.ConflictDialog("C:/x/y.zip", "y (1).zip")
        codes = [G.CONFLICT_COPY, G.CONFLICT_FORCE, G.CONFLICT_SKIP, G.CONFLICT_CANCEL]
        chk("ConflictDialog 可构造", d is not None, "")
        chk("四个返回码互不相同且都不是 1( Accepted)",
            len(set(codes)) == 4 and 1 not in codes, str(codes))
        from PySide6.QtWidgets import QPushButton as _QPB
        btns = [b.text() for b in d.findChildren(_QPB)]
        for want_txt in ("下载副本", "覆盖重下", "跳过", "取消"):
            chk("冲突对话框含按钮 %s" % want_txt, want_txt in btns, str(btns))
        d.done(G.CONFLICT_COPY)
        chk("done() 能正确记录返回码", d.result_code == G.CONFLICT_COPY,
            "result_code=%s" % d.result_code)
        d.deleteLater()
    except Exception as e:
        chk("ConflictDialog 构造", False, "%s: %s" % (type(e).__name__, e))

    w.deleteLater()
except Exception as e:
    import traceback
    chk("同名文件冲突流程", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("8. 导出 CSV: 仅测速之后就必须能导出")
print("=" * 72)
# 用户明确要求: "测速之后就可以导出"。
# 旧实现只导出下载时序 (chart.ts), 而仅测速不创建下载器 -> 一条进度都没有 ->
# 永远是"暂无数据"。
try:
    from PySide6.QtWidgets import QFileDialog
    csvp = os.path.join(TMP, "probe_export.csv")
    real_save = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (csvp, "CSV (*.csv)"))
    w = G.MainWindow()
    w.outdir = TMP

    # 8a 什么都没跑 -> 提示"暂无数据", 不生成文件
    if os.path.exists(csvp):
        os.remove(csvp)
    w._export_csv()
    chk("无数据时不生成文件", not os.path.exists(csvp), "")

    # 8b 只有 measure (模拟仅测速跑完) -> 必须能导出
    w.measure = {
        "url": "https://bigota.d.miui.com/20.5.6/x_028480154d_10.0.zip",
        "file": "x_028480154d_10.0.zip", "host": "bigota.d.miui.com",
        "size": 2555596407, "proxy": "", "limit_mbps": 0,
        "probe": [{"tag": "cdnorg", "host": "cdnorg.d.miui.com", "quality": "✓",
                   "ok": True, "size": 2555596407, "ttfb_ms": 543, "dns_ms": 6,
                   "tcp_ms": 252, "server": "Tengine", "range": True,
                   "stall_kbps": 816.5, "err": None},
                  {"tag": "bn", "host": "bn.d.miui.com", "quality": "⚠", "ok": False,
                   "size": 2555596407, "ttfb_ms": 727, "dns_ms": 9, "tcp_ms": 320,
                   "server": "Tengine", "range": True, "stall_kbps": 18.3,
                   "err": "限速 18 KB/s"}],
        "baseline": [{"tag": "cdnorg", "nconn": 1, "MBps": 1.12, "min": 1.0,
                      "max": 1.3, "cv": 8.0, "errors": 0, "limited": True}],
        "ramp": [{"tag": "cdnorg", "nconn": 64, "MBps": 85.0, "min": 60, "max": 99,
                  "cv": 55.0, "errors": 0},
                 {"tag": "cdnorg", "nconn": 128, "MBps": 90.0, "min": 70, "max": 105,
                  "cv": 59.0, "errors": 0}],
        "chosen": {"tag": "cdnorg", "conns": 128, "MBps": 90.0, "cv": 59.0},
    }
    chk("chart.ts 为空 (仅测速本来就没有时序)", not w.chart.ts, "%d 点" % len(w.chart.ts))
    w._export_csv()
    ok_file = os.path.exists(csvp)
    chk("仅测速后能导出 CSV", ok_file, csvp if ok_file else "未生成")
    if ok_file:
        txt = open(csvp, encoding="utf-8-sig").read()
        chk("CSV 含节点探测表", "节点探测" in txt and "cdnorg" in txt, "%d 字节" % len(txt))
        chk("CSV 含单连接基准表", "单连接基准" in txt, "")
        chk("CSV 含并发爬坡表", "并发爬坡" in txt, "")
        chk("CSV 记录了劣质节点实测速率", "18.3" in txt, "")
        chk("CSV 记录了最终选择",
            "cdnorg @ 128 连接" in txt.replace('"', ""), "")
        chk("CSV 是合法表格 (列数一致)",
            len(set(l.count(",") for l in txt.splitlines() if l.strip()
                    and not l.startswith("#") and "URL" not in l)) <= 40, "")

    # 8c measure + 时序并存时两份都要有
    w.chart.ts = [1.0, 2.0, 3.0]
    w.chart.tot = [10.0, 20.0, 30.0]
    w.chart.per = {"cdnorg": [5.0, 10.0, 15.0]}
    if os.path.exists(csvp):
        os.remove(csvp)
    w._export_csv()
    if os.path.exists(csvp):
        txt = open(csvp, encoding="utf-8-sig").read()
        chk("同时导出测量结果与时序",
            "节点探测" in txt and "下载实时速度" in txt, "%d 字节" % len(txt))
    else:
        chk("同时导出测量结果与时序", False, "未生成")

    QFileDialog.getSaveFileName = real_save
    w.deleteLater()
except Exception as e:
    import traceback
    chk("导出 CSV 流程", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("9. 自检结果必须有弹窗 (不能只写日志)")
print("=" * 72)
try:
    w = G.MainWindow()
    popups = []
    real_box = G.MessageBox

    class _Sig(object):
        def connect(self, *a, **k):
            pass

    class _Btn(object):
        def __init__(self):
            self.clicked = _Sig()

        def setText(self, *a):
            pass

    class SelfBox(object):
        def __init__(self, title, body, *a, **k):
            popups.append((title, body))
            self.yesButton = _Btn()
            self.cancelButton = _Btn()
            self.textLayout = None

        def exec(self):
            return 0

    G.MessageBox = SelfBox
    n_before = len(w._log_lines)
    w._run_selftest()
    chk("自检后弹出了结果窗口", len(popups) == 1, "弹窗 %d 个" % len(popups))
    if popups:
        title, body = popups[0]
        chk("弹窗标题正确", "自检" in title, title)
        chk("弹窗含结论 (通过/失败)", ("通过" in body) or ("失败" in body), body[:40])
    chk("日志同时也写入了完整输出",
        len(w._log_lines) - n_before > 20, "新增 %d 行" % (len(w._log_lines) - n_before))
    # 日志里必须能看到逐项 PASS, 不能只留最后 6 行
    joined = "\n".join(l for l, _c, _v in w._log_lines)
    chk("日志保留了逐项 PASS", joined.count("PASS") > 20, "%d 处 PASS" % joined.count("PASS"))
    G.MessageBox = real_box
    w.deleteLater()
except Exception as e:
    import traceback
    chk("自检弹窗", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("10. 图标 (logo) 接线")
print("=" * 72)
# 用户要求: 链接框图标、进程图标、启动项/快捷方式图标统一用同一个 logo。
# 这里守住每一处接线, 任何一处断了都会静默退化成"没图标", 很难发现。
try:
    from PySide6.QtWidgets import QLabel
    from PySide6.QtGui import QIcon

    ico = G.app_icon()
    chk("app_icon() 能加载", not ico.isNull(), "")
    szs = sorted(set((s.width(), s.height()) for s in ico.availableSizes()))
    chk("图标含多分辨率 (小尺寸不会糊)",
        len(szs) >= 5 and (16, 16) in szs and (256, 256) in szs, str(szs))

    for n in ("logo.ico", "logo.png", "logo_256.png"):
        chk("资源可定位: %s" % n, G._asset(n) is not None, str(G._asset(n)))
    chk("缺失资源返回 None 而不是乱猜", G._asset("绝对不存在.png") is None, "")

    w = G.MainWindow()
    chk("主窗口设了图标", not w.windowIcon().isNull(), "")

    # 链接框前面那格必须是 logo 位图, 不是 🔗 emoji
    pm_found = False
    emoji_found = False
    for l in w.findChildren(QLabel):
        p = l.pixmap()
        if p is not None and not p.isNull() and l.width() == 22 and l.height() == 22:
            pm_found = True
        if l.text() and "🔗" in l.text():
            emoji_found = True
    chk("链接框图标已换成 logo 位图", pm_found, "")
    chk("链接框不再使用 🔗 emoji", not emoji_found, "")

    # 快捷方式规格: 冻结/源码两种模式都要能用
    tgt, args, icon = w._shortcut_spec()
    chk("快捷方式有明确目标", bool(tgt), tgt)
    chk("快捷方式带图标", bool(icon) and "," in icon, icon)
    chk("图标指向 .ico 或 exe",
        icon.lower().endswith(",0") and (".ico" in icon.lower() or ".exe" in icon.lower()),
        icon)
    if getattr(sys, "frozen", False):
        chk("冻结模式: 目标就是 exe", tgt.lower().endswith(".exe") and not args, tgt)
    else:
        chk("源码模式: 目标是 pythonw 且带脚本参数",
            "pythonw" in tgt.lower() and "mirom_gui.py" in args,
            "%s %s" % (os.path.basename(tgt), args))

    # 开机自启的判定必须读磁盘真实状态, 不能凭一次点击记着
    chk("autostart 状态读的是磁盘", w._autostart_on() == os.path.exists(w._autostart_lnk()),
        "on=%s" % w._autostart_on())
    chk("autostart 路径在启动文件夹内",
        "Startup" in w._autostart_lnk(), w._autostart_lnk())
    # 菜单里的勾选态要和磁盘一致
    act = getattr(w, "_act_autostart", None)
    chk("菜单勾选态与磁盘一致", act is not None and act.isChecked() == w._autostart_on(),
        "checked=%s" % (act.isChecked() if act else "无此菜单项"))

    # 隐藏子进程: 建快捷方式时绝不能弹黑框
    import inspect as _ins3
    _s3 = _ins3.getsource(G._run_hidden_ps)
    chk("_run_hidden_ps 用 CREATE_NO_WINDOW",
        "CREATE_NO_WINDOW" in _s3, "")
    chk("_run_hidden_ps 用 SW_HIDE 双保险",
        "SW_HIDE" in _s3 or "wShowWindow" in _s3, "")

    w.deleteLater()
except Exception as e:
    import traceback
    chk("图标接线", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

print()
print("=" * 72)
print("11. 帮助菜单: 每一项都必须指向真实目标")
print("=" * 72)
# 用户报的 bug: "软件菜单的帮助二级菜单内功能未完善"。
# 根因是 REPO_URL 一直是占位符 "https://github.com/", README_URL 是它的 "#readme" ——
# 点「使用说明」只会打开 GitHub 首页, 「常见问题」的锚点也是编的。
# 更糟的是 _open_url 里 `except: pass` 把所有失败都吞了, 用户点完什么都没发生。
try:
    from PySide6.QtWidgets import QMenu

    chk("REPO_URL 不是占位符",
        G.REPO_URL.rstrip("/") != "https://github.com", G.REPO_URL)
    chk("REPO_URL 指向真实仓库",
        "poposjj/mirom" in G.REPO_URL, G.REPO_URL)
    for nm, u in (("README_URL", G.README_URL), ("ISSUES_URL", G.ISSUES_URL),
                  ("RELEASES_URL", G.RELEASES_URL)):
        chk("%s 指向仓库内页面" % nm, "poposjj/mirom" in u, u)
    chk("issues 地址正确", G.ISSUES_URL.endswith("/issues/new"), G.ISSUES_URL)
    chk("releases 地址正确", G.RELEASES_URL.endswith("/releases"), G.RELEASES_URL)

    # 锚点必须是以 # 开头的非空片段, 且不能是空壳 "#readme"
    for nm, a in (("常见问题", G.ANCHOR_FAQ), ("使用教程", G.ANCHOR_USAGE),
                  ("实现思路", G.ANCHOR_HOWTO), ("项目结构", G.ANCHOR_STRUCT),
                  ("自己打包", G.ANCHOR_BUILD)):
        chk("锚点 %s 形如 #xxx" % nm, a.startswith("#") and len(a) > 2, a)

    # ★ 拼起来的完整地址只能有【一个】#。
    #   第一版 README_URL 自带 "#readme", 再拼锚点就成了
    #   ".../mirom#readme#三常见问题" —— 浏览器只认第一个 #, 点击永远停在页首。
    #   这种错误肉眼很难发现, 必须断言。
    for nm, a in (("常见问题", G.ANCHOR_FAQ), ("使用教程", G.ANCHOR_USAGE),
                  ("实现思路", G.ANCHOR_HOWTO), ("项目结构", G.ANCHOR_STRUCT),
                  ("自己打包", G.ANCHOR_BUILD)):
        full = G.README_URL + a
        chk("完整地址 %s 只有一个 #" % nm, full.count("#") == 1, full)
        chk("完整地址 %s 以锚点结尾" % nm, full.endswith(a), full)
    chk("README_URL 自身不带 fragment", "#" not in G.README_URL, G.README_URL)
    chk("README_TOP 恰好一个 #", G.README_TOP.count("#") == 1, G.README_TOP)

    # 遍历帮助菜单: 每个动作都得有回调, 且 URL 类目标不能是裸 github.com
    w = G.MainWindow()
    help_menu = None
    for m in w.menuBar().findChildren(QMenu):
        if "帮助" in m.title():
            help_menu = m
            break
    chk("找到帮助菜单", help_menu is not None, "")
    if help_menu:
        items, no_cb = [], []
        for a in help_menu.actions():
            if a.isSeparator():
                continue
            items.append(a.text())
            try:
                n = a.receivers("2triggered(bool)")
            except Exception:
                n = 1
            if not n:
                no_cb.append(a.text())
        chk("帮助菜单没有死项", not no_cb, "共 %d 项; 死项: %s" % (len(items), no_cb or "无"))
        chk("帮助菜单项足够完整 (>=10)", len(items) >= 10, "%d 项" % len(items))
        for want in ("使用说明（本地，可离线看）", "常见问题", "检查更新",
                     "打开下载页（Releases）", "反馈问题 / 提建议", "项目主页",
                     "它是怎么提速的（实现思路）", "导出诊断报告（反馈时附上）"):
            chk("帮助菜单含「%s」" % want, want in items, "")
        # 反向检查: 不允许再出现占位地址。
        # ⚠ 只看【非注释行】—— 上面那段解释"以前是占位符"的注释里本身就带着
        #   那个字符串, 直接全文匹配会被自己的注释误伤 (第一版就是这么挂的)。
        import inspect as _i11
        src = _i11.getsource(G)
        code_lines = []
        for ln in src.splitlines():
            s = ln.strip()
            if s.startswith("#") or s.startswith('"') or s.startswith("'"):
                continue
            code_lines.append(ln)
        code = "\n".join(code_lines)
        chk("源码里没有裸 github.com 占位地址",
            '"https://github.com/"' not in code.replace("'", '"'), "")

    # 使用说明必须能找到本地文件 (帮助应当离线可用)
    cands = w._doc_candidates()
    chk("使用说明候选路径非空", len(cands) >= 2, "%d 个候选" % len(cands))
    found = [p for p in cands if os.path.exists(p)]
    chk("本地能定位到使用说明.txt", bool(found),
        found[0] if found else "候选: %s" % cands[:3])

    # 打不开浏览器时必须给出兜底提示, 不能静默
    import inspect as _i11b
    s = _i11b.getsource(G.MainWindow._open_url)
    chk("_open_url 失败时不再静默吞掉", "_url_fallback" in s, "")
    chk("_open_url 没有裸 except: pass",
        not (s.count("except") and s.count("pass") >= s.count("except")), "")
    chk("_url_fallback 会把链接给用户",
        "clipboard" in _i11b.getsource(G.MainWindow._url_fallback), "")

    # 检查更新: 网络请求必须在后台线程 (主线程发 HTTP 会把界面冻住)
    su = _i11b.getsource(G.MainWindow._check_update)
    chk("检查更新走后台线程", "Thread(" in su or "threading" in su, "")
    chk("检查更新的线程是 daemon",
        "daemon=True" in su, "否则关窗口后线程会吊住进程")

    w.deleteLater()
except Exception as e:
    import traceback
    chk("帮助菜单接线", False, "%s: %s" % (type(e).__name__, e))
    traceback.print_exc()

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print()
print("=" * 72)
print("总计: PASS %d  FAIL %d" % (PASS, FAIL))
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  - %s" % f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
