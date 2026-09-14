#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mirom.py — 小米 ROM 官方下载加速器 (单文件 / 零依赖 / 纯标准库)

════════════════════════════════════════════════════════════════════════
 为什么小米官网下载慢? —— 实测结论
════════════════════════════════════════════════════════════════════════
  1. 慢的根因不是总带宽限制, 而是【单连接限速】
     cdnorg / bn.d 每连接仅 ~1.0 MB/s, 单线程下 6.9GB 要下 2 小时。
     实测: 1 连接 1.11 MB/s  →  192 连接 109 MB/s   (98 倍)

  2. bigota.d.miui.com 的 403 不是封禁, 是【Referer 防盗链】
     不带 Referer → 403 ; 带任意 Referer → 206 正常
     (注意: 伪造 UA / X-Forwarded-For 都无效, 只有 Referer 有效)

  3. 各节点质量差异巨大, 必须实测选线, 不能凭直觉
     阿里云新加坡节点单连接最快(6.5MB/s)但【抖动 CV 48-61%】,
     速度在 10↔189 MB/s 之间反复崩塌, 实际体验极差。

  4. 并发有拐点, 超过就只增抖动不提速
     192 连接 = 109 MB/s / CV 8%   ← 拐点
     256 连接 =  96 MB/s / CV 31%  ← 白费力气还更抖

  5. 文件名内嵌 MD5 前 10 位, 可自动校验完整性
     marble_images_..._cn_20c17bd222.tgz  →  真实 MD5 = 20c17bd222a023e5...
════════════════════════════════════════════════════════════════════════

用法:
  python mirom.py <任意一个CDN链接>
  python mirom.py <URL> -o D:\\roms            # 指定输出目录
  python mirom.py <URL> --probe               # 只测速, 不下载
  python mirom.py <URL> --conns 192           # 手动指定并发
  python mirom.py <URL> --single              # 只走最快单线(最稳)
  python mirom.py <URL> --multi               # 多线聚合(最快)
"""
import argparse, atexit, hashlib, http.client, json, os, re, socket, ssl
import statistics, subprocess, sys, threading, time
from collections import deque

# ─────────────────────────── 配置 ───────────────────────────
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 小米 ROM 已知 CDN 节点。referer=None 表示无需防盗链头。
# quality 来自 24 文件 × 9 区域 的抽样实测 (见 xfu_analyze.py):
#   ✓  可用稳定   △ 快但抖   ⚠ 差   ✗ 挂死
CDN_HOSTS = [
    ("cdnorg",  "cdnorg.d.miui.com",                                            None,
     "✓",  "零连接错误, 唯一干净稳定的节点 (192并发可达 109 MB/s, CV 8%)"),
    ("aliyun",  "bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com", None,
     "△",  "单点吞吐最高但爆发式抖动 (CV 100%), 需高并发抹平"),
    ("bn",      "bn.d.miui.com",                                                None,
     "⚠",  "连接大面积失败 (12并发中9~11条断), 实测 0.4~8.5 MB/s"),
    ("bigota",  "bigota.d.miui.com",                                            "https://www.miui.com/",
     "✗",  "假206挂死: 返回206+完整Content-Length, 但每连接仅传~256KB即永久停滞"),
    ("hugeota", "hugeota.d.miui.com",                                           "https://www.miui.com/",
     "✗",  "同 bigota 挂死特征"),
]
# 说明: bigota 另有一层 Referer 防盗链 (无 Referer → 403);
#       但补上 Referer 后依然挂死, 所以 Referer 不是解药。


MB = 1024 * 1024
UNIT = 1 * MB          # 最小调度单元 / 断点续传粒度
SEED = 16 * MB         # 初始分片大小
READ = 512 * 1024      # socket 单次读取
TUNE_CACHE = os.path.join(os.path.expanduser("~"), ".mirom_tune.json")
VERSION = "1.0.0"
AUTHOR = "poposjj"
BYLINE = "by %s" % AUTHOR
PROXY = None           # (host, port) 或 None; 由 --proxy / 环境变量设置
CURRENT_LOCK = None    # 当前持有的单实例锁路径, 供引擎结束时主动释放

CTX = ssl.create_default_context()
# ⚠ 这里原来写死了 check_hostname=False + CERT_NONE (完全关闭证书校验),
#   属于自己给自己开的中间人后门。实测 4 个官方 CDN 的证书都是有效的
#   (CN=*.d.miui.com / ap-southeast-1.oss.aliyuncs.com), 严格校验完全可用,
#   当初关掉纯属多余。现在默认严格校验, 需要时显式加 --insecure 才降级。
#   注意: ssl 模块不允许对 check_hostname=True 的 context 直接设 CERT_NONE,
#   所以 --insecure 时必须先关 hostname 再关 verify。
def set_insecure(flag=True):
    global CTX
    CTX = ssl.create_default_context()
    if flag:
        CTX.check_hostname = False
        CTX.verify_mode = ssl.CERT_NONE

# ══════════════════════════ 跨平台适配层 ══════════════════════════
# 目标: Windows / Linux / macOS 均可直接运行, 且不区分盘型 (SSD/HDD/网络盘/可移动)
IS_WIN = (os.name == "nt")
# 记账体检开关: 设 MIROM_DEBUG_ACCT=1 时, 下载循环每次采样都会打印
# "written 字节数 vs 位图覆盖字节数"的差值, 用来判断进度是否虚高。
_DBG_ACCT = bool(os.environ.get("MIROM_DEBUG_ACCT"))
IS_MAC = (sys.platform == "darwin")

_UNICODE_OK = True


def _stdout_ok():
    """
    判断当前有没有可写的标准输出。

    ⚠ PyInstaller 的 --windowed 构建【没有控制台】, 此时 sys.stdout 就是 None。
      旧代码直接调 sys.stdout.isatty() → AttributeError, EXE 一启动就崩:
        File "mirom.py", line 106, in _init_console
        AttributeError: 'NoneType' object has no attribute 'isatty'
      注意: Python 内建的 print() 在 sys.stdout is None 时是【静默 no-op】
      (CPython 里对 Py_None 有特判), 所以满地的 print() 反而没事 ——
      真正要防的是显式调用 .isatty() / .write() / .flush() 的地方。
    """
    so = sys.stdout
    return so is not None and hasattr(so, "write") and hasattr(so, "isatty")


# 改代码页之前记下原值, 供 _init_console 判断"这是不是一台老终端"。
_ORIG_CP = None


def _force_utf8():
    """
    把标准输出/错误强制切成 UTF-8 —— 必须在【任何一次输出之前】调用。

    ★ CI 抓到的真 bug (GitHub Actions 的 windows-latest, 控制台代码页是 1252):
        UnicodeEncodeError: 'charmap' codec can't encode characters in position 12-13
          File "mirom.py", line 2004, in selftest
            print("mirom %s 自检" % VERSION)
      英文版 Windows 的默认代码页是 1252, 根本编不出中文 ——
      于是【任何一条命令都直接崩】, 而 README 上白纸黑字写着支持 Windows。
      中文版 Windows 是 936, 编得出中文, 所以本地怎么测都测不出来。
      这正是"只有换台机器才暴露"的那类问题 —— 幸好 CI 用的是英文镜像。

    两件事一起做:
      ① Windows: 把控制台输出代码页设成 65001, 终端才会真的按 UTF-8 渲染。
         不设的话即使我们输出 UTF-8 字节, 终端仍按 1252 解释 → 全是乱码。
      ② 把 sys.stdout/stderr 重新配置成 UTF-8。
         加 errors="replace" 是兜底: 万一某台机器还是编不出来, 最多显示成 ?,
         绝不再抛 UnicodeEncodeError 把整个程序带崩 —— 崩了连提示都没有。

    注意模块级就已经执行了 (见下面 _force_utf8() 的调用), 因为 COLOR/TTY 这些
    常量在 import 期就求值了, 后面满地的 print 更早不了。
    """
    global _ORIG_CP
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            _ORIG_CP = k.GetConsoleOutputCP()
            k.SetConsoleOutputCP(65001)          # CP_UTF8
        except Exception:
            pass
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue                              # --windowed 下就是 None
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Python 3.6 没有 reconfigure; 或者 stdout 已被替换成别的流对象。
            # 都无所谓 —— 只是没有这层保险, 不能因此崩掉。
            pass


# ⚠ 必须在这里就调, 不能等 main(): 模块导入期就有常量求值, 之后满地的
#   print/log 更早不了。放在这一行之前是刻意的。
_force_utf8()


def _init_console():
    """
    初始化终端:
      * 关闭 Windows 老式控制台的 ANSI 支持问题 (开启 VT 处理)
      * 输出被重定向到文件 / 管道时不发 ANSI 转义码
      * 遵守 NO_COLOR 规范 (https://no-color.org) 与 TERM=dumb
      * 终端不支持中文/box 字符时自动降级为纯 ASCII 进度条
      * 【无控制台环境】(PyInstaller --windowed) 下直接返回 False, 绝不允许抛异常
    """
    global _UNICODE_OK
    if os.environ.get("NO_COLOR") is not None or os.environ.get("TERM") == "dumb":
        return False
    if not _stdout_ok():
        _UNICODE_OK = False
        return False
    if not sys.stdout.isatty():
        return False
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            # 开启 ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
            # 老 conhost 对全角/制表符支持差。
            # ⚠ 这里要看【改之前】的代码页 (_ORIG_CP): _force_utf8() 已经把当前
            #   代码页设成 65001 了, 直接查 GetConsoleOutputCP() 会永远得到 65001,
            #   这个判断就废了。
            cp = _ORIG_CP if _ORIG_CP is not None else k.GetConsoleOutputCP()
            if cp not in (65001, 936, 54936):
                _UNICODE_OK = False
        except Exception:
            return False
    else:
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        if "utf" not in enc:
            _UNICODE_OK = False
    return True


COLOR = _init_console()
TTY = _stdout_ok() and sys.stdout.isatty()   # 无控制台时必须短路, 否则 None.isatty() 崩
C_OK = "\033[92m" if COLOR else ""
C_BAD = "\033[91m" if COLOR else ""
C_WARN = "\033[93m" if COLOR else ""
C_DIM = "\033[90m" if COLOR else ""
C_END = "\033[0m" if COLOR else ""

BAR_FULL = "█" if _UNICODE_OK else "#"
BAR_EMPTY = "░" if _UNICODE_OK else "-"


def c(txt, col):
    return "%s%s%s" % (col, txt, C_END) if col else txt


def _safe_write(s):
    """
    写 stdout 的容错版本。

    ⚠ 这不是杞人忧天: 只要把输出接进 `| head`、日志轮转、或 SSH 断开,
       stdout 就会被关闭, 此时 sys.stdout.write 抛 BrokenPipeError。
       如果不管, 异常会一路冒泡把【整个下载线程组带崩】, 留下一个损坏的半成品。
       实测踩过: `mirom.py ... | head -28` 直接把 2.38GB 的下载打断, MD5 全错。
       这里捕获后静默降级为"里程碑打行"模式并丢弃后续输出, 让下载继续跑完。
    """
    global TTY
    if not _stdout_ok():
        TTY = False
        return False
    try:
        sys.stdout.write(s)
        sys.stdout.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        TTY = False
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
        return False


# ══════════════════════ GUI 挂钩 (A 方案的接口层) ══════════════════════
# CLI 直接跑时这些全是 None, 行为与原来完全一致;
# GUI 用 MiromEngine 时会填上回调, 于是日志/进度/阶段都走回调而不是 stdout。
# 这样【不需要】把两千行核心逻辑重写一遍, 也不会出现 CLI / GUI 两套实现漂移。
HOOKS = {
    "log": None,        # fn(msg:str)
    "stage": None,      # fn(key:str, label:str, index:int, total:int)
    "meta": None,       # fn(dict)  文件信息 / 镜像清单 / 磁盘信息
    "progress": None,   # fn(dict)  每 0.5s 一次 —— 折线图的数据源
    "attach": None,     # fn(Downloader)  把下载器实例交给 Engine, 供暂停/取消
    "cancelled": None,  # fn() -> bool  线程级取消查询 (CLI 下为 None)
    # fn(dict) 测速/探测的结构化结果。
    # ★ 为什么单独要有这条通道 (用户报的 bug: "工具中保存测速数据, 这个功能无效"):
    #   进度包 (progress) 只在【下载阶段】的传输循环里发。而"仅测速"模式压根
    #   不创建 Downloader, 于是一条 progress 都没有 —— 界面上的"导出本次测速数据"
    #   跑完仅测速去点, 只会得到"暂无数据"。
    #   更关键的是: 真正的测量结果 (各节点 TTFB/服务器/stall 速率、单连接基准速率、
    #   并发爬坡每一档的吞吐与抖动) 从来就没被记录下来过, 只打印成了日志。
    "measure": None,
}


def cancelled():
    """
    线程级取消查询。CLI 下 HOOKS["cancelled"] 是 None → 恒 False, 零开销。

    ★ 为什么必须有它 (实测抓到的真 bug):
      ①② 探测节点 + 单连接基准测速 两个阶段合计跑 40 秒以上, 而它们都发生在
      Downloader 创建【之前】。旧代码的取消只有 Downloader.stop 一条路, 于是
      这段窗口里点"取消"完全无效 —— 界面打印"已请求取消…"骗用户, 引擎却一路
      把探测(21s)+基准测速(22s)+爬坡全跑完才停下。
    """
    fn = HOOKS.get("cancelled")
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def emit(key, *args):
    fn = HOOKS.get(key)
    if fn is not None:
        try:
            fn(*args)
        except Exception:
            pass


def log(m):
    if HOOKS["log"] is not None:
        emit("log", "[%s] %s" % (time.strftime("%H:%M:%S"), m))
        return
    _safe_write("[%s] %s\n" % (time.strftime("%H:%M:%S"), m))


def stage(key, label, index, total=4):
    """阶段推进: 供 UI 画 4 步指示器 (探测 → 调优 → 下载 → 校验)。CLI 下无副作用。"""
    emit("stage", key, label, index, total)


# ─────────────────────── 磁盘类型探测 (跨平台) ───────────────────────
# Windows 下"不弹黑框"需要的 creationflags。0x08000000 = CREATE_NO_WINDOW。
# 若常量缺失 (非 Windows) 给 0。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0
_disk_cache = {}
_disk_cache_lock = threading.Lock()


def _run_hidden(cmd, timeout=25):
    """
    跑一个外部命令 —— 在 Windows GUI/冻结环境下【绝不弹窗】。

    ★ 这是用户实际报上来的 bug:
      "点测速等等还有很多按钮之后, 会弹出 PowerShell 又瞬间闪退"。
      根因: PyInstaller --windowed 打包出来的进程【没有控制台】, 于是 Windows
      在创建 powershell.exe 子进程时会【新分配一个控制台窗口】——
      就是那个一闪而过的黑框。它不影响功能, 但很难看, 而且会让用户以为中毒了。
      修法: creationflags=CREATE_NO_WINDOW + STARTUPINFO 里 SW_HIDE 双保险
      (只用其中一个在部分 Windows 版本 / 某些父进程组合下仍会闪)。
    """
    kw = {"capture_output": True, "text": True, "timeout": timeout}
    if IS_WIN:
        kw["creationflags"] = _CREATE_NO_WINDOW
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0            # SW_HIDE
            kw["startupinfo"] = si
        except Exception:
            pass
    return subprocess.run(cmd, **kw)


def _disk_type_windows(path):
    """Windows: 用 Get-PhysicalDisk 查 MediaType / BusType。失败返回 None。"""
    try:
        import subprocess as _sp          # noqa: F401  (确保模块已导入)
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return None
        letter = drive.rstrip(":\\/")
        ps = ("$p=Get-Partition -DriveLetter %s -ErrorAction Stop;"
              "$d=Get-PhysicalDisk -DeviceNumber $p.DiskNumber -ErrorAction Stop;"
              "Write-Output ($d.MediaType.ToString()+'|'+$d.BusType.ToString())"
              % letter)
        out = _run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                           "-WindowStyle", "Hidden", "-Command", ps], timeout=25)
        s = (out.stdout or "").strip()
        if "|" not in s:
            return None
        media, bus = [x.strip() for x in s.split("|", 1)]
        bus_u = bus.upper()
        if "NETWORK" in bus_u or "ISCSI" in bus_u:
            return "network"
        if "USB" in bus_u:
            return "removable"
        return {"SSD": "ssd", "HDD": "hdd",
                "UNSPECIFIED": "unknown", "SCM": "ssd"}.get(media.upper(), "unknown")
    except Exception:
        return None


def _disk_type_posix(path):
    """
    Linux: /sys/block/<dev>/queue/rotational  (0=SSD, 1=HDD)
    macOS: diskutil info -plist (SolidState)
    """
    try:
        import subprocess
        rp = os.path.realpath(os.path.abspath(path))
        if sys.platform.startswith("linux"):
            out = _run_hidden(["df", "--output=source", rp], timeout=15)
            lines = (out.stdout or "").strip().split("\n")
            if len(lines) < 2:
                return None
            dev = lines[-1].strip()                       # 例: /dev/nvme0n1p2
            base = os.path.basename(dev)
            # nvme0n1p2 -> nvme0n1 ; sda1 -> sda ; mmcblk0p1 -> mmcblk0
            import re as _re
            m = _re.match(r"^(nvme\d+n\d+|mmcblk\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+)", base)
            if not m:
                return None
            node = m.group(1)
            if node.startswith(("nvme", "mmcblk", "vd")):
                rot = 0
            else:
                with open("/sys/block/%s/queue/rotational" % node) as f:
                    rot = int(f.read().strip())
            return "hdd" if rot else "ssd"
        if IS_MAC:
            out = _run_hidden(["diskutil", "info", "-plist", rp], timeout=15)
            txt = out.stdout or ""
            if "<key>SolidState</key>" in txt:
                seg = txt.split("<key>SolidState</key>", 1)[1][:80]
                return "ssd" if "<true/>" in seg else "hdd"
            if "<key>Internal</key>" in txt:
                seg = txt.split("<key>Internal</key>", 1)[1][:80]
                if "<false/>" in seg:
                    return "removable"
        return None
    except Exception:
        return None


def detect_disk(path):
    """
    探测目标路径所在磁盘类型。返回 (类型, 说明文字)。
    类型: ssd / hdd / network / removable / unknown
    读取失败一律返回 unknown —— 绝不能因为探测不了就拒绝下载。
    """
    ap = os.path.abspath(path)
    # UNC / 网络路径直接判定 (Windows: \\server\share, POSIX: //host/share)
    if ap.startswith("\\\\") or ap.startswith("//"):
        return "network", "网络/远程盘"
    # 目标目录可能还不存在 → 逐级向上找到第一个存在的目录再判断,
    # 否则 os.path.isdir 失败会回退到当前盘, 把网络盘误判成本地盘。
    d = os.path.dirname(ap) or "."
    probe_dir = None
    cur = d
    for _ in range(40):
        if os.path.isdir(cur):
            probe_dir = cur
            break
        parent = os.path.dirname(cur.rstrip("\\/")) or os.path.abspath(os.sep)
        if parent == cur:
            break
        cur = parent
    if probe_dir is None:
        return "unknown", "未能识别 (按通用配置运行)"
    # 缓存: 同一个盘符只问一次系统。
    #   detect_disk 在每次下载/测速/自检都会被调用, 每次都起一个 PowerShell 进程 ——
    #   即使加了 CREATE_NO_WINDOW 不闪窗了, 那也是白起进程 (实测一次约 0.3~1 秒)。
    #   盘型在一个会话里不会变, 缓存是安全的。
    _ck = os.path.splitdrive(probe_dir)[0].upper() or probe_dir
    with _disk_cache_lock:
        if _ck in _disk_cache:
            t = _disk_cache[_ck]
        else:
            t = None
    if t is None:
        t = _disk_type_windows(probe_dir) if IS_WIN else _disk_type_posix(probe_dir)
        if t is not None:
            with _disk_cache_lock:
                _disk_cache[_ck] = t
    if t is None:
        return "unknown", "未能识别 (按通用配置运行)"
    return t, {
        "ssd": "固态硬盘", "hdd": "机械硬盘",
        "network": "网络/远程盘", "removable": "可移动介质",
        "unknown": "未知类型",
    }.get(t, "未知类型")


def disk_hint(dtype):
    """
    按盘型给出 (连接数上限, 分片MB, 说明)。

    实测依据 (本机 WDC WD10EZEX 机械盘 / Samsung 980PRO 固态盘, 512KB 块):
        机械盘 顺序写 175.7 MB/s; 1/16/64/192 线程并发散块写 = 179/180/179/178 MB/s
        —— 大块并发散写在机械盘上【不会踩踏】, 所以不必为盘型牺牲并发。
        固态盘 顺序写 1554.9 MB/s; 16~64 线程最佳 (2390/2288), 192 线程反而回落到 1081。
    因此:
      * 机械/固态盘都放开并发, 让【网络】决定连接数 (真正的瓶颈在网络);
      * 只有【网络盘/可移动介质】必须收敛 —— 它们每次写都有往返延迟或带宽极低,
        连接开太多会让写队列长期打满、TCP 接收窗口堆积、重试率飙升。
    另外运行期会统计"写盘占用时间", 若磁盘成为瓶颈会明确提示 (见 Downloader.verify)。
    """
    if dtype == "network":
        return 8, 8, "网络盘: 每次写都有网络往返, 收敛到 8 连接 / 8MB 分片"
    if dtype == "removable":
        return 16, 8, "可移动介质: 带宽有限, 收敛到 16 连接 / 8MB 分片"
    if dtype == "hdd":
        return 192, 16, "机械盘: 实测大块并发散写不踩踏, 按网络调优 (上限 192 连接)"
    return 256, 16, "固态盘: 按网络调优 (上限 256 连接)"


def _long_paths_enabled():
    """Windows 是否启用了 >260 字符的长路径 (注册表 LongPathsEnabled)。非 Windows 恒 True。"""
    if not IS_WIN:
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            v, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
        return bool(v)
    except Exception:
        return False


def path_risk(path):
    """
    开工前预检输出路径。没问题返回 None, 否则返回一句可以直接展示给用户的话。

    ★ 为什么需要:
      Windows 默认 MAX_PATH=260。输出目录深一点 + ROM 文件名本身就 60 字符,
      很容易越界。旧行为是【下到 100% 才在写盘时抛 FileNotFoundError】——
      两分钟白等, 而且错误信息完全看不出是路径太长。这里提前拦下并给可操作建议。
      阈值取 240 而不是 260: 要给 "<文件名>.mirom.json" 和 ".tmp" 留出余量。
    """
    try:
        ap = os.path.abspath(path)
    except Exception:
        ap = path
    if IS_WIN and not _long_paths_enabled() and len(ap) >= 240:
        return ("输出路径过长 (%d 字符; Windows 未启用长路径支持, 上限 260)。\n"
                "请把下载目录换到更浅的位置, 例如  D:\\Roms\n"
                "(或启用长路径: 组策略 → 计算机配置 → 管理模板 → 系统 → 文件系统 → "
                "启用 Win32 长路径)" % len(ap))
    d = os.path.dirname(ap) or "."
    try:
        os.makedirs(d, exist_ok=True)
    except Exception as e:
        return "无法创建输出目录 %s\n%s" % (d, e)
    if not os.access(d, os.W_OK):
        return "输出目录不可写: %s" % d
    return None


def unique_copy_path(path):
    """
    给一个"没被占用"的副本文件名: a.zip -> a (1).zip -> a (2).zip ...

    ★ 用于用户选了"已存在同名文件时下载副本"的场景。
      注意 MD5 校验用的是【原文件名】里的内嵌前缀 (见 run_flow 里的 eh),
      所以改名不影响校验 —— 这一点很容易被写错成"从输出文件名提 MD5",
      那样副本文件会因为名字变了而静默跳过校验。
    """
    if not os.path.exists(path):
        return path
    d, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    for i in range(1, 1000):
        cand = os.path.join(d, "%s (%d)%s" % (stem, i, ext))
        if not os.path.exists(cand):
            return cand
    import time as _t
    return os.path.join(d, "%s (%d)%s" % (stem, int(_t.time()), ext))


def check_space(path, need):
    """
    开工前确认目标盘放得下。返回 (是否够, 剩余字节)。
    旧版本完全没有这一步 —— 7GB 的文件下到 90% 才发现盘满了, 前功尽弃。
    探测失败一律返回"够", 绝不能因为测不了就拒绝下载。
    """
    try:
        import shutil
        d = os.path.dirname(os.path.abspath(path)) or "."
        for _ in range(40):
            if os.path.isdir(d):
                break
            nd = os.path.dirname(d.rstrip("\\/")) or os.path.abspath(os.sep)
            if nd == d:
                break
            d = nd
        free = shutil.disk_usage(d).free
        # 除文件本身, 断点/位图/文件系统开销留 3% 余量
        return free >= need * 1.03, free
    except Exception:
        return True, 0


def _pid_alive(pid):
    """判断进程是否还活着 (跨平台, 失败一律当作'已死'以便接管陈旧锁)。"""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_lock(out):
    """
    抢占式单实例锁 —— 防止两个实例同时写同一个输出文件。
    这是给 GUI/EXE 准备的必需品: 用户双击两次图标就必然触发,
    两个进程各写各的分片, 最终文件必然损坏 (而且 MD5 才拦得住)。

    返回 (lock_path, err)。err 非空 → 应中止; lock_path 为 "" → 无法加锁但继续。
    用 O_CREAT|O_EXCL 原子创建, 内容是持有者 PID; PID 已死则视为陈旧锁并接管。
    """
    lp = out + ".lock"
    for _ in range(2):
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return lp, None
        except FileExistsError:
            holder = ""
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    holder = f.read().strip()
            except Exception:
                pass
            if holder and _pid_alive(holder):
                # ★ 锁是可重入的: 持有者就是本进程自己 -> 说明是同一个 GUI
                #   里连续启动第二次下载 (第一次已取消或已完成)。旧实现会把它
                #   误判成"另一个实例在跑"而直接放弃执行 —— 实测导致第二次点
                #   「开始下载」毫无反应, 续传功能在 GUI 下完全失效。
                if holder == str(os.getpid()):
                    return "", None
                return None, "另一个实例 (PID %s) 正在处理同一个文件" % holder
            log(c("发现陈旧锁文件 (持有进程 %s 已不存在), 接管" % (holder or "?"), C_WARN))
            try:
                os.remove(lp)
            except Exception:
                return "", None
        except Exception:
            return "", None      # 目录只读等情况下不阻塞下载
    return "", None


def release_lock(lp):
    if not lp:
        return
    try:
        os.remove(lp)
    except Exception:
        pass


class RateLimiter:
    """
    全局令牌桶限速 (--limit MB/s)。0 表示不限速。
    所有 worker 共享一个桶, 因此限的是【总带宽】而非单连接。
    注意: 睡眠必须在锁外进行, 否则会把所有线程串行化。
    """
    def __init__(self, bytes_per_sec):
        self.rate = float(bytes_per_sec or 0)
        self.lock = threading.Lock()
        # 初始桶为空, 不做"1 秒突发"。用户设 5MB/s 就该量到 5MB/s;
        # 留突发的话短任务会明显超速 (实测限 20MB/s 传 200MB 只用了 9.0s 而非 10s)。
        self.allowance = 0.0
        self.last = time.monotonic()
        self.slept = 0.0

    def consume(self, n):
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = time.monotonic()
                self.allowance = min(self.rate, self.allowance + (now - self.last) * self.rate)
                self.last = now
                if self.allowance >= n:
                    self.allowance -= n
                    return
                need = (n - self.allowance) / self.rate
            dt = min(need, 0.25)
            time.sleep(dt)
            self.slept += dt


def parse_proxy(spec):
    """把 http://host:port / host:port 解析成 (host, port); 空或不受支持的协议返回 None。"""
    if not spec:
        return None
    s = spec.strip()
    low = s.lower()
    # ★ SOCKS 返回 None, 而不是把 127.0.0.1:1080 当 HTTP 代理硬解析出来。
    #   旧行为: 先打印"代理: 127.0.0.1:1080", 紧接着又警告"不支持 SOCKS" ——
    #   自相矛盾, 而且真的拿着 SOCKS 端点去发 HTTP CONNECT, 用户最后看到的
    #   是一堆看不懂的连接超时, 而不是"代理协议不支持"。
    if low.startswith(("socks5://", "socks4://", "socks://")):
        return None
    for pre in ("http://", "https://"):
        if low.startswith(pre):
            s = s[len(pre):]
            break
    s = s.rstrip("/")
    if not s:
        return None
    if ":" in s:
        h, _, p = s.rpartition(":")
        try:
            return (h, int(p))
        except ValueError:
            return (s, 8080)
    return (s, 8080)


def write_prealloc(f, size):
    """
    预分配:
      Linux/macOS 的 ftruncate 是瞬时稀疏操作, 直接用。
      Windows 的 SetEndOfFile 会真实写零 (实测 5.8GB ≈ 11 秒), 但它换来的是
      【连续簇】—— 实测对比 (5.80GB / 192 连接):
          truncate: 建 11.4s + 传输  76s = 1:28   起步 6% 即满速
          seek    : 建  0.0s + 传输 122s = 2:02   起步爬 20s 才满速
      192 条连接同时往未分配区域写会让 NTFS 现场分配簇, 反而慢 46 秒。
      所以 Windows 上保留 truncate, 并由上层用后台线程与测速阶段重叠掉这 11 秒。
    """
    if IS_WIN:
        f.truncate(size)
    else:
        try:
            os.posix_fallocate(f.fileno(), 0, size)   # Linux: 真正预分配且不写零
        except (AttributeError, OSError):
            f.truncate(size)                          # macOS / 其它: 稀疏, 瞬时


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%.2f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024.0


def hms(s):
    if s <= 0 or s > 86400 * 3:
        return "--:--"
    return time.strftime("%H:%M:%S", time.gmtime(s))


# ─────────────────────── URL 解析与镜像展开 ───────────────────────
ROM_RE = re.compile(r"^/(?P<ver>[^/]+)/(?P<file>[^/]+)$")


def state_path(out):
    """
    断点文件名 = <数据文件>.mirom.json。

    抽成模块级函数是为了让 run_flow 也能拿到它 —— 那里需要在【创建 Downloader
    之前】丢弃失效断点 (见"校验不通过, 将重新下载"那段), 不能等实例化。
    """
    return out + ".mirom.json"


def safe_name(name):
    """
    把文件名里各平台都不接受的字符换掉。

    ★ 为什么必须有 (实测抓到的真 bug):
      用户从论坛/脚本复制来的链接常带查询串, 例如
        https://bigota.d.miui.com/20.5.6/miui_DAVINCI_..._10.0.zip?token=ABC
      旧 parse_url 直接把这串当文件名, 得到 'miui_..._10.0.zip?token=ABC':
        · Windows 下 '?' 是非法字符 → open() 抛 OSError(Invalid argument),
          下载跑到写盘那一刻才炸, 前面的探测/调优全白费;
        · 顺带把内嵌 MD5 也弄丢了 (正则要求 .zip 结尾) → 【静默跳过校验】。
      这两个后果都不该由一个多余的 ? 引起。
    """
    if not name:
        return ""
    bad = '<>:"/\\|?*'
    out = []
    for ch in name:
        if ch in bad or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    s = "".join(out)
    # Windows 不允许文件名以点或空格结尾 (会被静默截断, 导致后面找不回文件)
    s = s.rstrip(" .")
    return s


def parse_url(url):
    """拆出 version 目录 + 文件名, 并识别来源节点"""
    u = url.strip()
    if u.startswith("http://"):
        u = "https://" + u[7:]
    if not u.startswith("https://"):
        u = "https://" + u
    rest = u[8:]
    host, _, path = rest.partition("/")
    path = "/" + path
    # ★ 请求路径【保留】查询串 —— 有些链接带 token, 服务端要它才放行。
    #   但本地文件名必须去掉 ?query 和 #fragment (见 safe_name 的注释)。
    path_only = path.split("#", 1)[0].split("?", 1)[0]
    m = ROM_RE.match(path_only)
    if not m:
        raise ValueError("无法解析路径: %s\n期望形如 /V14.0.27.0.TMRCNXM/xxx_images_....tgz"
                         % path_only)
    src = next((t for t, h, _, _, _ in CDN_HOSTS if h == host.split(":")[0]), "unknown")
    fname = safe_name(m.group("file"))
    if not fname:
        raise ValueError("文件名非法, 无法作为本地文件保存: %r" % m.group("file"))
    return {"host": host.split(":")[0], "version": m.group("ver"),
            "file": fname, "path": path, "source": src}


def expand_mirrors(info, extra_hosts=(), include_dead=False):
    """由任一节点 URL 展开出全部镜像"""
    out = []
    for tag, host, ref, quality, note in CDN_HOSTS:
        if quality == "✗" and not include_dead:
            continue
        out.append({"tag": tag, "host": host, "path": info["path"], "referer": ref,
                    "quality": quality, "note": note})
    for h in extra_hosts:
        out.append({"tag": "custom", "host": h, "path": info["path"],
                    "referer": "https://www.miui.com/", "quality": "?", "note": "自定义"})
    return out


# ─────────────────────────── HTTP 层 ───────────────────────────
def _headers(mirror, rng=None):
    h = {"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "keep-alive"}
    if mirror.get("referer"):
        h["Referer"] = mirror["referer"]
    if rng:
        h["Range"] = "bytes=%d-%d" % rng
    return h


def http_connect(mirror, timeout=25, rcvbuf=8 * MB):
    # 代理支持: HTTPS 走 CONNECT 隧道。用 set_tunnel 是标准做法 ——
    # 必须先连到代理端口, 再让 http.client 自己发 CONNECT。
    if PROXY:
        conn = http.client.HTTPSConnection(PROXY[0], PROXY[1], timeout=timeout, context=CTX)
        conn.set_tunnel(mirror["host"], 443)
    else:
        conn = http.client.HTTPSConnection(mirror["host"], 443, timeout=timeout, context=CTX)
    conn.connect()
    try:
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    except Exception:
        pass
    return conn


def stall_check(mirror, want=4 * MB, deadline=8.0):
    """
    劣质节点检测 —— 这是本项目最重要的发现之一。

    事实澄清 (实测抓包, 原先把现象描述错了):
      bigota / hugeota 返回 206 + 完整的 Content-Length, 看起来完全正常, 但它们
      【不是"挂死"】, 而是被限速到约 16 KB/s —— 每秒精确吐一个 16KB 包, 一直吐:

          0.00s +16384 累计 16KB     4.00s +16384 累计 96KB
          1.00s +16384 累计 32KB     8.00s +16384 累计 176KB
          2.00s +16384 累计 48KB    12.00s +16384 累计 256KB

      这个区别很关键, 因为它决定了正确的读法:
        · 数据【一直在到达】→ socket 超时永远不会触发, 靠 settimeout 拦不住;
        · 旧的 resp.read(256*1024) 会一直阻塞到攒够 262144 字节 = 约 12 秒,
          于是 deadline=8.0 形同虚设 (只在两次 read 之间检查), 实测每次都超时到 12s。
      正确做法: 小块读 + 每次按【剩余预算】设超时, deadline 才真正说了算。

    返回 (实际收到字节, 是否劣质, 实际耗时秒)
    """
    got = 0
    t0 = time.time()
    try:
        conn = http_connect(mirror, timeout=10)
        conn.request("GET", mirror["path"], headers=_headers(mirror, (0, want - 1)))
        resp = conn.getresponse()
        if resp.status != 206:
            return got, False, 0.0
        while got < want:
            left = deadline - (time.time() - t0)
            if left <= 0:
                break
            # 取消/中断时立刻收手 (用户在探测阶段点取消)
            if cancelled():
                break
            # 每次读的超时 = min(2 秒, 剩余预算) —— 这样 deadline 是硬上界,
            # 不会像旧代码那样被一次巨长 read 顶到 12 秒去。
            try:
                conn.sock.settimeout(max(0.5, min(2.0, left)))
            except Exception:
                pass
            b = resp.read(min(32 * 1024, want - got))
            if not b:
                break
            got += len(b)
        conn.close()
    except Exception:
        pass
    return got, (got < 1 * MB), time.time() - t0


def probe(mirror):
    """单线可达性 / 大小 / Range 支持 / 限速特征 / 挂死检测"""
    r = {"tag": mirror["tag"], "host": mirror["host"], "ok": False}
    try:
        t0 = time.time()
        socket.gethostbyname(mirror["host"])
        r["dns_ms"] = int((time.time() - t0) * 1000)
        t0 = time.time()
        s = socket.create_connection((mirror["host"], 443), timeout=8)
        r["tcp_ms"] = int((time.time() - t0) * 1000)
        s.close()
    except Exception as e:
        r["err"] = "DNS/TCP 失败: %s" % e
        return r
    try:
        conn = http_connect(mirror, timeout=15)
        t0 = time.time()
        conn.request("GET", mirror["path"], headers=_headers(mirror, (0, 1)))
        resp = conn.getresponse()
        resp.read()
        r["ttfb_ms"] = int((time.time() - t0) * 1000)
        r["status"] = resp.status
        r["server"] = resp.getheader("Server") or ""
        cr = resp.getheader("Content-Range")
        if resp.status == 206 and cr:
            r["size"] = int(cr.split("/")[-1])
            r["range"] = True
            r["ok"] = True
        elif resp.status == 403:
            r["err"] = "403 防盗链" + ("" if mirror.get("referer") else " (缺 Referer)")
        else:
            r["err"] = "HTTP %d" % resp.status
        conn.close()
    except Exception as e:
        r["err"] = "%s: %s" % (type(e).__name__, e)
    # 二次确认: 真拉一段, 识别"假 206 涓流"节点
    if r["ok"]:
        got, dead, el = stall_check(mirror)
        r["stall_got"] = got
        r["stall_sec"] = el
        r["stall_rate"] = (got / el) if el > 0 else 0.0
        if dead:
            r["ok"] = False
            # 报【实测速率】而不是写死的"8秒" —— 旧文案说"挂死", 但真实情况是
            # 被限速到 ~16 KB/s (数据一直在来, 只是慢得没用), 说成挂死会误导排查。
            r["err"] = ("限速 %.0f KB/s (%.0f 秒只收到 %.0f KB, 无法使用)"
                        % (r["stall_rate"] / 1024, el, got / 1024))
            r["note"] = ("返回206+完整Content-Length, 但每连接仅约16KB/s —— "
                         "不是连接挂死, 是被限速到不可用")
    return r


# ─────────────────────────── 测速 ───────────────────────────
class Counter:
    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def add(self, k):
        with self.lock:
            self.n += k


class _Donate(Exception):
    """主控要求 worker 交出尚未下完的剩余分片 (重排尾部, 让新连接有活可干)"""
    pass


def _feeder(mirror, start, length, ctr, stop, errs):
    try:
        conn = http_connect(mirror)
        conn.request("GET", mirror["path"], headers=_headers(mirror, (start, start + length - 1)))
        resp = conn.getresponse()
        if resp.status != 206:
            with ctr.lock:
                errs.append(resp.status)
            return
        while not stop.is_set():
            b = resp.read(READ)
            if not b:
                break
            ctr.add(len(b))
        conn.close()
    except Exception as e:
        with ctr.lock:
            errs.append(str(e)[:40])


def speed_test(mirror, nconn, size, dur=14.0, warm=5.0):
    """固定并发跑 dur 秒, 返回稳态吞吐与抖动"""
    ctr, stop, errs = Counter(), threading.Event(), []
    seg = size // nconn
    ths = [threading.Thread(target=_feeder,
                            args=(mirror, i * seg, seg, ctr, stop, errs), daemon=True)
           for i in range(nconn)]
    t0 = time.monotonic()
    for t in ths:
        t.start()
    samples, ln, lt = [], 0, t0
    while time.monotonic() - t0 < dur:
        time.sleep(0.25)
        now = time.monotonic()
        if now - lt >= 0.5:
            samples.append((ctr.n - ln) / (now - lt))
            ln, lt = ctr.n, now
    stop.set()
    for t in ths:
        t.join(timeout=3)
    k = int(warm / 0.5)
    st = samples[k:] if len(samples) > k + 2 else samples
    if not st:
        return None
    m = statistics.mean(st)
    return {"MBps": m / MB, "min": min(st) / MB, "max": max(st) / MB,
            "cv": (statistics.pstdev(st) / m * 100) if m > 0 else 999,
            "errors": len(errs),
            "samples": [round(s / MB, 1) for s in samples]}


def autotune(mirror, size, levels, budget):
    """并发爬坡找拐点: 吞吐不再显著提升即停止"""
    best, out = None, []
    t0 = time.monotonic()
    for lv in levels:
        # 单个节点的爬坡最多 4 档 × ~17 秒 ≈ 68 秒, 只在节点之间查一次太粗。
        if cancelled():
            break
        # ⚠ 必须用【真实墙钟时间】记账。早先用 "spent += 14.0" 记名义时长,
        #   但建 64~192 条连接 + TLS 握手本身就要 3~6 秒, 实际每档 16~20 秒,
        #   于是 120 秒的预算跑出了 194 秒。
        spent = time.monotonic() - t0
        if spent + 8 > budget:
            break
        r = speed_test(mirror, lv, size, dur=max(5.0, min(14.0, budget - spent)))
        if not r:
            continue
        r["nconn"] = lv
        out.append(r)
        log("    %3d 连接 → %7.2f MB/s | 抖动 CV %5.1f%% | 失败 %d"
            % (lv, r["MBps"], r["cv"], r["errors"]))
        # 熔断: 连接失败率超过 20% 说明该节点本身不可靠, 再往上爬纯属浪费时间。
        #   实测 bn.d.miui.com 在 192 并发下 164 条连接失败, 吞吐塌到 11 MB/s,
        #   但旧逻辑仍会把 64/128/192 三档全部跑完 (~1 分钟白等)。
        if r["errors"] > max(3, lv * 0.2):
            log(c("    ✗ 失败 %d/%d 条连接 (>20%%), 判定该节点不可靠, 提前终止测试"
                  % (r["errors"], lv), C_WARN))
            if best is None:
                best = r
            break
        if best is None or r["MBps"] > best["MBps"] * 1.06:
            best = r
        else:
            log("    %s" % c("↑ 吞吐已饱和(增益<6%%), 拐点锁定 %d 连接" % (best["nconn"],), C_WARN))
            break
    return best, out


def load_tune():
    """
    读取调优缓存 (host -> {conns, MBps, cv, ts})。

    ★ 必须【逐字段校验】, 不能直接 return json.load(f)。
      实测抓到的崩溃:
          TypeError: unsupported operand type(s) for -: 'float' and 'str'
          at  cc and time.time() - cc.get("ts", 0) < 24*3600
      缓存里只要有一个 ts 是字符串, 整次下载就在"探测完、调优刚开始"那一刻崩掉
      —— 用户已经白等了 25 秒。而这份缓存是【面向用户的文件】: GUI 菜单里就有
      "打开调优缓存"直接拿编辑器打开, 用户手改坏了、或者旧版本留下的格式,
      都不该让程序崩。坏掉的条目丢掉即可, 剩下的照样能用。
    """
    try:
        with open(TUNE_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for host, cc in raw.items():
        if not isinstance(cc, dict):
            continue
        try:
            conns = int(cc.get("conns"))
            mbps = float(cc.get("MBps", 0) or 0)
            cv = float(cc.get("cv", 999) or 999)
            ts = float(cc.get("ts", 0) or 0)
        except (TypeError, ValueError):
            continue                      # 这一条不可用, 丢掉就好
        if conns <= 0 or ts <= 0:
            continue
        out[str(host)] = {"conns": conns, "MBps": mbps, "cv": cv, "ts": ts}
    return out


def save_tune(d):
    try:
        with open(TUNE_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


# ─────────────────────────── 下载器 ───────────────────────────
class Downloader:
    """
    共享分片队列 + 工作窃取 + 动态分裂
      * 所有连接从同一队列领活, 快线路自然多干活 → 天然负载均衡
      * 队尾按需二分剩余分片 → 消除收尾阶段的速度塌陷
    """

    def __init__(self, mirrors, path, out, size, chunk=SEED, alt=None, prealloc=None,
                 limiter=None):
        self.mirrors, self.path, self.out, self.size = mirrors, path, out, size
        self.prealloc = prealloc     # 后台预分配线程 (在测速阶段就已开始)
        self.limiter = limiter       # 全局限速器 (None = 不限速)
        self.alt = list(alt or [])   # 备用线路: 主干线路中途退化时自动顶上
        self.failover_log = []
        self.donate = {}             # {worker_idx: True} 主控要求该 worker 让出剩余分片
        self.holding = {}            # {worker_idx: (s,e)} 当前持有的分片, 供精准选大块让位
        self.hlock = threading.Lock()
        self.reshards = 0            # 尾部重排次数
        self.last_reshard = 0.0
        self._per_prev = {}          # 上次采样时各线路的累计字节 (算瞬时速度用)
        self._tick_prev = time.monotonic()
        self.nconn0 = len(mirrors)   # 初始连接数, 用于限制增线上限
        self._next_log = 0           # 非交互模式下的下一个进度打点百分比
        self.chunk = chunk
        self.nunit = (size + UNIT - 1) // UNIT
        self.done = bytearray(self.nunit)
        # 已完成单元数 (O(1) 维护)。bm_bytes() 靠它, 免得每 0.5 秒 sum() 一遍整个位图。
        # ⚠ 任何整体替换 self.done 的地方都必须同步重算它 (见 load_state)。
        self.done_n = 0
        self.q = deque()
        self.qlock = threading.Lock()
        self.dlock = threading.Lock()
        self.ctr = Counter()
        self.per = {}
        # 累计写入字节数。⚠ 【不要拿它算进度】—— 它只表示"写盘动作发生了多少次",
        #   含重下/让位重排的重复计数, 且不保证覆盖的单元是完整的。
        #   进度/ETA/平均值一律用 bm_bytes()(位图)。这里只服务于
        #   MIROM_DEBUG_ACCT 记账体检 —— 它是验证位图账目对不对的对照量。
        self.written = 0
        self.disk_t = 0.0         # 所有线程阻塞在 write() 上的累计时间
        self.plock = threading.Lock()
        self.inflight = 0         # 已被领走、正在传输中的分片数
        self.ilock = threading.Lock()
        self.stop = threading.Event()
        self.paused = threading.Event()   # GUI 暂停用; CLI 下永远不置位
        # 记录所有活着的连接, 取消时主动 shutdown 把阻塞在 read() 里的 worker 踢醒。
        # ⚠ 实测: 只置 stop 标志, worker 会卡在 r.read(512KB) 里等 socket 超时 (最长 30 秒),
        #   导致 "取消" 迟迟不生效 —— 自动化测试里表现为 2.5 秒后引擎仍 is_alive()。
        self.live_conns = set()
        self.clock = threading.Lock()
        self.workers = []
        self.retries = 0
        self.fatal = []
        self.nconn = len(mirrors)
        self._seed()
        self.t0 = time.monotonic()
        self._ln, self._lt = 0, self.t0

    # ---- 队列 ----
    def _eff_chunk(self, remaining):
        """
        分片尺寸自适应 —— 这是收尾拖尾的关键。
        固定 16MB 分片在两种情况下会出问题:
          * 续传时剩余量少: 例 1023MB / 16MB = 只有 64 个分片, 却有 192 条连接,
            112 条连接一启动就无活可干, 尾部还会退化成 1~2 条连接慢慢啃。
        策略: 让分片数至少是连接数的 2 倍; 但不小于 2MB, 以免请求数过多
              触发 CDN 的请求频率限制 (实测过度细分会让吞吐从 96 MB/s 崩到 3 MB/s)。
        """
        if self.nconn <= 0:
            return self.chunk
        want = remaining // max(self.nconn * 2, 1)
        # ⚠ 必须向下取整到 UNIT 的整数倍。否则分片末尾落在单元中间,
        #   _mark 会把它算作"整块完成", 取消续传时就留下查不出来的空洞。
        want = max(2 * UNIT, (want // UNIT) * UNIT)
        return max(2 * UNIT, min(self.chunk, want))

    def _seed(self):
        eff = self._eff_chunk(self.size)
        self.eff_chunk = eff
        pos = 0
        while pos < self.size:
            end = min(pos + eff, self.size) - 1
            self.q.append((pos, end))
            pos = end + 1

    def _pend_units(self, s, e):
        a, b = s // UNIT, e // UNIT
        return sum(1 for u in range(a, b + 1) if not self.done[u])

    def take(self, active):
        """
        领活。队列见底时二分剩余分片, 避免尾部塌陷。

        ⚠ 修过的两个坑:
          (1) 分片被切成正好 2*UNIT 时 mid 会算成 s, 旧实现先 pop 再判断,
              条件不成立就【既不切分也不放回】→ 分片凭空蒸发 → 文件尾部留零块。
              修法: 先确认可切分, 任何失败路径都要放回队列。
          (2) 队尾最后一个大分片会被某个 worker 独吞, 队列随即清空,
              其余 worker 全部退出 → 收尾阶段只剩 1 条连接, 实测最后 0.7% 拖 32 秒。
              修法: 若取走后队列已空且分片还很大, 就地再切一刀, 把一半还回队列,
              让仍在等待的 worker 有活可干。
        """
        with self.qlock:
            if not self.q:
                return None
            # 只有 >= 2*UNIT 才可能切出两段各 >= UNIT 的合法分片
            if self.q[-1][1] - self.q[-1][0] + 1 >= 2 * UNIT:
                unclaimed = sum((e - s) // UNIT + 1 for s, e in self.q)
                if unclaimed < active * 2:
                    s, e = self.q.pop()
                    full = e - s + 1
                    half = (full // 2 // UNIT) * UNIT
                    mid = s + half
                    if half >= UNIT and s < mid <= e:
                        self.q.append((s, mid - 1))
                        self.q.append((mid, e))
                        self.q.rotate(-1)
                    else:
                        self.q.append((s, e))      # ★ 关键: 放回, 绝不丢弃
            rng = self.q.popleft()
            # ⚠ 曾试过"取走后队列若空、且分片还大就再切一刀还回一半"。
            #   实测适得其反: 分片被过度细分 → HTTP 请求数暴增 → CDN 对请求频率
            #   有限制, 吞吐反而从 96 MB/s 崩到 3 MB/s。已回退, 保持大分片。
            return rng

    def remaining(self, active=1):
        with self.qlock:
            n = sum((e - s) // UNIT + 1 for s, e in self.q)
            if n > 0:
                # 队列只能粗估, 用已完成单元精确计算
                return self.size - self.done_bytes()
        return self.size - self.done_bytes()

    def done_bytes(self):
        with self.dlock:
            return min(self.done_n * UNIT, self.size)

    def bm_bytes(self):
        """
        位图覆盖的字节数 —— 进度 / ETA / 平均速度的【唯一权威来源】。O(1)。

        ★ 为什么不能用 self.written:
          written 是"每向磁盘写一个字节就 +1"; 位图必须表示"这一单元整块都已落盘"。
          两者在传输途中必然有差, 差值 = 所有在途连接手里那半个单元之和。
          实测 (MIROM_DEBUG_ACCT=1, 2.38GB / 128~192 连接):
            · 若位图只在【整块分片下完】时标记: 虚高最大 659.5 MB = 文件的 27%,
              且 written 会超过文件大小 → min() 一夹, 进度条在文件还差 22MB 时
              就显示 100.00% 然后长时间不动, 用户会以为程序死了;
              点暂停后进度条还会再爬 8%~31% (位图在补记暂停前就读完的数据)。
            · 改为【按单元增量标记】(见读取循环里的 _mark 调用) 之后:
              虚高降到 42 MB = 1.7%, 正好等于每条在途连接手里那半个单元。
          所以进度永远不超过实际完成度, 且暂停即刻停住。
        """
        with self.dlock:
            return min(self.done_n * UNIT, self.size)

    def _mark(self, s, e):
        """
        把 [s, e] 覆盖的单元标记为已完成。

        ⚠ 这里必须保证"标记 = 该单元整块都已落盘", 否则取消后续传会跳过它,
          文件留下空洞 (而且完整性闸门查不出来, 只有 MD5 能兜住)。
        实测踩过: _eff_chunk 算出 6.2MB 这类【非单元整数倍】的分片大小,
          于是每个分片结尾都会越界标记下一个单元的一半, 取消时就是空洞。

        因此约定: 分片一律按 UNIT 对齐 (见 _eff_chunk / _Donate 的处理),
        唯一例外是文件最后一个单元 —— 它本来就是残缺的。
        """
        a = s // UNIT
        if e >= self.size - 1:
            b = self.nunit - 1          # 写到文件末尾 => 最后一个残缺单元也算完成
        else:
            b = e // UNIT
        with self.dlock:
            for u in range(a, min(b + 1, self.nunit)):
                if not self.done[u]:
                    self.done[u] = 1
                    self.done_n += 1

    # ---- 断点 ----
    @property
    def statefile(self):
        return state_path(self.out)

    def load_state(self):
        """
        读取断点。⚠ 任何失败路径都必须把状态【真正重置干净】, 见下面的 except。

        历史坑 (实测踩过): 用户删掉 tgz 但留下 .mirom.json, 工具信任"已完成 98.9%",
        只补下剩余 83MB 就报"下载完成" —— 产出尺寸正确但 99% 是零的文件。
        只在结尾被 MD5 拦住; 加了 --no-verify 就是静默损坏。
        原先这个检查写在 run() 里, 于是正确性取决于"调用方记得先查一遍"。
        现在下沉到这里: 谁调用 load_state 都不会踩中, 自洽。
        """
        if not os.path.exists(self.statefile):
            return
        # 断点只有和数据文件同时存在才有意义
        if not os.path.exists(self.out) or os.path.getsize(self.out) != self.size:
            log(c("数据文件缺失/尺寸不符, 但断点文件仍在 —— 断点已失效, 丢弃后重新开始",
                  C_WARN))
            self._discard_state()
            return
        try:
            with open(self.statefile, "r", encoding="utf-8") as f:
                st = json.load(f)
            if st.get("size") != self.size or st.get("unit") != UNIT:
                log("断点文件不匹配, 忽略")
                return
            bm = bytes.fromhex(st["bitmap"]) if st.get("bitmap") else b""
            if len(bm) == self.nunit:
                self.done = bytearray(bm)
                # 位图被整体替换 → 计数器必须同步重算, 否则 bm_bytes() 会读到旧值
                # (漏掉这一步的表现: 续传时进度从 0% 开始, 看着像断点丢了)
                self.done_n = sum(self.done)
            # 按未完成单元重建队列 (分片尺寸按剩余量自适应)
            self.q.clear()
            eff = self._eff_chunk(max(self.size - self.done_bytes(), UNIT))
            self.eff_chunk = eff
            run_start = None
            for u in range(self.nunit + 1):
                need = (u < self.nunit and not self.done[u])
                if need and run_start is None:
                    run_start = u
                elif not need and run_start is not None:
                    s = run_start * UNIT
                    e = min((u + 1) * UNIT, self.size) - 1
                    # 切成自适应大小的分片
                    p = s
                    while p <= e:
                        q = min(p + eff, e + 1) - 1
                        self.q.append((p, q))
                        p = q + 1
                    run_start = None
            got = self.done_bytes()
            if got:
                log("发现断点: 已完成 %s / %s (%.1f%%)"
                    % (human(got), human(self.size), got / self.size * 100))
        except Exception as e:
            # ★ 旧实现只打一行"从头开始", 却什么都不重置 —— 此时 self.done 可能
            #   已经被上面赋过值了 (位图读成功、后面某步才炸)。那句话是假的, 而且
            #   正好落进它自己注释警告的那个坑: 信任一份没校验完的位图 → 静默损坏。
            #   这里必须真的清干净: 位图归零 + 队列重建为整份文件。
            log("断点读取失败(%s), 从头开始" % e)
            self._discard_state()

    def _discard_state(self):
        """
        把断点状态彻底清成"全新下载": 位图归零 + 队列重建为整份文件。

        ⚠ 队列重建走 _seed() —— 别在这里手写一遍, 那样会漏掉 _eff_chunk 的自适应
          逻辑, 而且以后改分片策略时要记得改两处。
        ⚠ 重建失败也不能让队列空着: 空队列的表现是"启动后瞬间结束、0 字节下载",
          比报错更难排查。所以失败时回退成固定分片自己兜一遍。
        """
        self.done = bytearray(self.nunit)
        self.done_n = 0
        try:
            self.q.clear()
        except Exception:
            pass
        try:
            self._seed()
        except Exception:
            # 兜底: 不依赖 nconn/chunk 这些可能还没初始化的属性
            try:
                step = max(2 * UNIT, UNIT * 8)
                p = 0
                while p < self.size:
                    q = min(p + step, self.size) - 1
                    self.q.append((p, q))
                    p = q + 1
            except Exception:
                pass
        try:
            if os.path.exists(self.statefile):
                os.remove(self.statefile)
        except Exception:
            pass

    def save_state(self):
        with self.dlock:
            bm = bytes(self.done).hex()
        tmp = self.statefile + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"size": self.size, "unit": UNIT, "bitmap": bm,
                           "ts": time.time()}, f)
            os.replace(tmp, self.statefile)
        except Exception:
            pass

    # ---- 工作线程 ----
    def worker(self, idx):
        m = self.mirrors[idx]
        fh = open(self.out, "r+b", buffering=0)
        conn, backoff = None, 0.4
        idle = 0
        disk_local = 0.0        # 本线程累计的"阻塞在写盘"的时间
        while not self.stop.is_set():
            rng = self.take(len(self.workers))
            if rng is None:
                # 短暂等待 (最多 1 秒): 让"刚被追加的备用线路连接"有机会接手
                # 正在重排中的尾部分片。等不到就直接退出, 不浪费线程。
                #
                # ⚠ 这里必须问位图, 不能问 written —— written 会因为
                #   让位重排/重试而超过文件大小 (实测超 9.5MB), 于是 fin 提前为真,
                #   尾部那一秒的宽限期被跳过, 队列里刚被让位回来的分片就没人接了。
                #   (在 plock 之外取, 避免与 dlock 形成锁序倒置)
                fin = self.bm_bytes() >= self.size
                if not fin and not self.fatal:
                    for _ in range(20):
                        time.sleep(0.05)
                        rng = self.take(len(self.workers))
                        if rng is not None:
                            break
                if rng is None:
                    break
            with self.ilock:
                self.inflight += 1
            s, e = rng
            with self.hlock:
                self.holding[idx] = (s, e)
            pos, good, att = s, False, 0
            # 增量标记的水位线 (见读取循环里的 _mark 调用)。必须按 UNIT 对齐起步。
            marked_upto = (s // UNIT) * UNIT
            donated = False
            gave_up = False
            try:
                while not self.stop.is_set() and att < 15:
                    att += 1
                    try:
                        if conn is None:
                            conn = http_connect(m)
                            self._register(conn)
                        conn.request("GET", m["path"], headers=_headers(m, (pos, e)))
                        r = conn.getresponse()
                        if r.status != 206:
                            raise IOError("HTTP %d" % r.status)
                        while pos <= e and not self.stop.is_set():
                            # ── 暂停检查 ──
                            #   放在读取循环最内层, 暂停延迟 ≈ 一次 512KB 读 (<1ms)。
                            #   不关闭连接 —— socket 保持, 恢复后无缝继续;
                            #   万一被服务端超时踢掉, 现有重试逻辑会自动重连。
                            while self.paused.is_set() and not self.stop.is_set():
                                time.sleep(0.1)
                            if self.stop.is_set():
                                break
                            # ── 让位检查 ──
                            #   尾部退化时, 所有剩余工作都攥在在途连接手里, 新加的
                            #   备用线路连接抢不到活只能空转退出。主控于是打标记要求
                            #   让位: 本 worker 立刻放弃剩余部分并还回队列, 由主控重新
                            #   分片, 新连接才有活可干。
                            if self.donate.pop(idx, False):
                                raise _Donate()
                            b = r.read(min(READ, e - pos + 1))
                            if not b:
                                raise IOError("连接提前关闭")
                            if self.limiter is not None:
                                self.limiter.consume(len(b))
                            tw = time.monotonic()
                            fh.seek(pos)
                            fh.write(b)
                            disk_local += time.monotonic() - tw
                            pos += len(b)
                            self.ctr.add(len(b))
                            # ★ 增量标记: 每写完一个【完整单元】就立刻记进位图。
                            #   旧行为是等整块分片 (6MB) 下完才 _mark 一次, 后果:
                            #     · 进度/ETA 最多虚低 660MB (2.38GB 文件的 27%);
                            #     · 点暂停后进度条还要再爬 8%~31% 才停 —— 因为位图
                            #       在补记"暂停前就读完、只是还没标记"的数据。
                            #   对齐到 UNIT 是必须的: _mark 会标记 e 所在的整个单元,
                            #   传 pos-1 会把只写了一半的单元标成完成 → 取消续传时
                            #   那里就是永久空洞, 而且完整性闸门查不出来。
                            upto = (pos // UNIT) * UNIT
                            if upto > marked_upto:
                                self._mark(marked_upto, upto - 1)
                                marked_upto = upto
                            with self.plock:
                                self.written += len(b)
                                self.disk_t += disk_local
                                disk_local = 0.0
                                self.per[m["tag"]] = self.per.get(m["tag"], 0) + len(b)
                        # ⚠⚠ 绝对不能写成无条件 good = True!
                        #   内层循环的退出条件有两个: "pos > e (下完了)" 和
                        #   "self.stop 被置位 (用户点了取消)"。旧实现不区分两者,
                        #   取消时也会把整个分片标记成"已完成" -> 位图谎报 ->
                        #   续传时跳过这一段 -> 文件留空洞。而且【完整性闸门也
                        #   查不出来】(位图说它完成了), 只有最后的 MD5 能兜住。
                        #   实测: 取消后续传完成的文件 MD5 全错。
                        if pos > e:
                            good = True
                            backoff = 0.4
                        break
                    except _Donate:
                        # 已下的部分保留, 只把 (pos, e) 这段剩余还回队列。
                        # 必须关连接重开: 原响应体是旧 Range 的, 不能继续读。
                        self._unregister(conn)
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                        # ★ 关键: 已经下完的 [s, pos-1] 必须记账。
                        #   漏掉这步 → 位图把这些单元记成"未写入",
                        #   完整性闸门会在结尾误报几百个"空洞" (实测 872 个)。
                        # ⚠ 且 pos 必须先向下对齐到 UNIT: 否则 _mark 会越界标记
                        #   pos 所在的半个单元, 取消续传时那里就是永久空洞。
                        #   多下一小段是幂等的 (覆盖写), 少记一次却是不可恢复的。
                        pos = (pos // UNIT) * UNIT
                        if pos > s:
                            self._mark(s, pos - 1)
                        if pos <= e:
                            # ⚠ 这里【绝对不能】把剩余切碎再放回。
                            #   我试过切成 3 段, 想让新追加的连接有活干, 结果:
                            #     3 次重排 × ~70 个分片 × 3 段 = 633 个新 HTTP 请求,
                            #     直接触发 CDN 的请求频率限流, 全程 1:39 → 3:19 (慢一倍)。
                            #   本项目早先已实测过同一现象 (过度细分让吞吐 96→3 MB/s)。
                            #   整块放回, 让少数连接把它啃完, 反而更快。
                            with self.qlock:
                                self.q.appendleft((pos, e))
                        donated = True
                        break
                    except Exception as ex:
                        self.retries += 1
                        self._unregister(conn)
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                        if att >= 15:
                            self.fatal.append("%s@%d: %s" % (m["tag"], s, str(ex)[:50]))
                            gave_up = True
                            break
                        # 可中断退避: 取消时不该还傻等 6 秒才退出 (实测取消耗时 5.9s)
                        _end = time.time() + backoff
                        while time.time() < _end and not self.stop.is_set():
                            time.sleep(min(0.2, max(0.0, _end - time.time())))
                        backoff = min(backoff * 1.8, 6.0)
            finally:
                with self.ilock:
                    self.inflight -= 1
                with self.hlock:
                    self.holding.pop(idx, None)
                if self.stop.is_set() and conn is not None:
                    self._unregister(conn)
            if good:
                self._mark(s, e)
            elif not donated and not gave_up:
                with self.qlock:
                    self.q.appendleft((s, e))
            # 放弃的分片不再回队, 否则会被反复领取→反复失败, 陷入死循环。
            # 交给结尾的完整性闸门报出来, 重新运行即可续传补齐。
        # ⚠ 这里绝对不能调 os.fsync(): 192 个 worker 各刷一次整个 GB 级文件的脏页,
        #   收尾阶段会直接引发 I/O 风暴, 把最后几个分片拖成龟速。
        #   统一由 run() 在所有线程 join 之后刷一次即可。
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass
        if conn is not None:
            self._unregister(conn)

    def tick(self):
        now = time.monotonic()
        dt = now - self._lt
        if dt < 0.5:
            return None
        sp = (self.ctr.n - self._ln) / dt
        self._ln, self._lt = self.ctr.n, now
        return sp

    # ---- GUI 控制接口 (CLI 下用不到, 但无副作用) ----
    def pause(self):
        if not self.stop.is_set():
            self.paused.set()
            log(c("已暂停 —— 连接保持不断, 恢复后无缝继续", C_WARN))

    def resume(self):
        if self.paused.is_set():
            self.paused.clear()
            log(c("已恢复下载", C_OK))

    def cancel(self):
        self.paused.clear()
        self.stop.set()
        self._kick_conns()
        log(c("已取消 —— 断点已保留, 重跑同一条命令即可续传", C_WARN))

    def _register(self, conn):
        try:
            with self.clock:
                self.live_conns.add(conn)
        except Exception:
            pass

    def _unregister(self, conn):
        try:
            with self.clock:
                self.live_conns.discard(conn)
        except Exception:
            pass

    def _kick_conns(self):
        """把所有在途连接 shutdown 掉, 让阻塞中的 read() 立刻返回 —— 取消/暂停秒生效"""
        try:
            with self.clock:
                conns = list(self.live_conns)
        except Exception:
            return
        for c in conns:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    def is_paused(self):
        return self.paused.is_set()

    def _line_speeds(self, dt):
        """把各线路的累计字节数换算成【瞬时速度】—— 折线图的分线数据源。"""
        out = {}
        with self.plock:
            cur = dict(self.per)
        for tag, v in cur.items():
            prev = self._per_prev.get(tag, v)
            out[tag] = max(0.0, (v - prev) / dt / MB)
        self._per_prev = cur
        return out

    def run(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.out)) or ".", exist_ok=True)
        # 后台预分配若还在跑, 等它结束。正常情况下它在测速阶段就跑完了, 这里不阻塞。
        if self.prealloc is not None and self.prealloc.is_alive():
            log("等待后台预分配完成...")
            self.prealloc.join()
        # ★ 断点必须与数据文件同时存在才有意义。
        #   实测踩过: 用户删掉 tgz 但留下 .mirom.json, 工具会信任"已完成 98.9%",
        #   只补下剩余 83MB, 然后报"下载完成" —— 产出一个尺寸正确但 99% 是零的文件。
        #   只是恰好被结尾的 MD5 拦住了; 若用户加了 --no-verify 就是静默损坏。
        #   数据文件缺失或尺寸不符 → 断点已失效, 直接丢弃重新开始。
        file_ok = os.path.exists(self.out) and os.path.getsize(self.out) == self.size
        if not file_ok and os.path.exists(self.statefile):
            log(c("数据文件缺失/尺寸不符, 但断点文件仍在 —— 断点已失效, 丢弃后重新开始", C_WARN))
            try:
                os.remove(self.statefile)
            except Exception:
                pass
        self.load_state()
        if not os.path.exists(self.out) or os.path.getsize(self.out) != self.size:
            log("创建文件 %s (%s)" % (self.out, human(self.size)))
            mode = os.environ.get("MIROM_PREALLOC", "auto")
            t_pa = time.time()
            with open(self.out, "wb") as f:
                if mode == "seek":
                    # 瞬时撑出逻辑大小, 但 Windows 上会导致 192 连接现场分配簇,
                    # 实测传输反而慢 46 秒。仅作为兜底选项保留。
                    f.seek(self.size - 1)
                    f.write(b"\x00")
                else:
                    write_prealloc(f, self.size)
            dt = time.time() - t_pa
            log("   预分配方式=%s, 耗时 %.2f 秒%s"
                % (mode, dt, "" if dt < 0.5 else " (纯磁盘操作)"))
        # 计时从"真正开始传输"算起, 否则会把建文件/探测的时间也算进耗时
        self.t0 = time.monotonic()
        self._lt = self.t0
        self._ln = 0

        left = self.size - self.done_bytes()
        if left <= 0:
            log("分片已全部完成, 直接校验")
            return self.verify()
        log("任务 %s | 剩余 %s | 共 %d 连接 | 分片 %d 个 x %.1f MB"
            % (human(self.size), human(left), self.nconn, len(self.q),
               getattr(self, "eff_chunk", self.chunk) / MB))
        seen = {}
        for m in self.mirrors:
            seen[m["tag"]] = seen.get(m["tag"], 0) + 1
        for t, n in sorted(seen.items(), key=lambda x: -x[1]):
            host = next(m["host"] for m in self.mirrors if m["tag"] == t)
            log("   %-7s %-56s %3d 连接" % (t, host[:56], n))

        for i in range(self.nconn):
            t = threading.Thread(target=self.worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)

        log("开始传输 (Ctrl+C 可安全中断, 支持续传)")
        win, last, saves = [], time.monotonic(), time.monotonic()
        samples = []
        peak = 0.0
        self.peak = 0.0
        try:
            while any(t.is_alive() for t in self.workers):
                time.sleep(0.5)
                sp = self.tick()
                if sp is not None:
                    win.append(sp)
                    samples.append(sp)

                # ── 每 0.5 秒给 GUI 一个采样点 ──
                #   折线图要的是"实时波动", 3 秒粒度太粗 (实测 118 秒只出 21 个点,
                #   曲线全是折角)。这里与 CLI 的 3 秒显示节奏解耦, 各走各的。
                if HOOKS["progress"] is not None:
                    now_t = time.monotonic()
                    dtl = max(0.001, now_t - self._tick_prev)
                    self._tick_prev = now_t
                    # ★ 用位图, 不用 written —— 见 bm_bytes() 的注释。
                    #   位图现在按单元增量维护, 虚高只有 ~1.7% (每条在途连接那半个单元),
                    #   所以这个数既是真话, 又足够跟手。
                    w2 = self.bm_bytes()
                    el2 = now_t - self.t0
                    with self.ilock:
                        infl2 = self.inflight
                    emit("progress", {
                        "pct": w2 / self.size * 100,
                        "speed": (sp or 0.0) / MB,
                        "avg": (w2 / el2 / MB) if el2 > 0 else 0.0,
                        "peak": peak / MB,
                        "done": w2, "total": self.size,
                        "elapsed": el2,
                        "eta": ((self.size - w2) / sp) if sp and sp > 0 else 0.0,
                        "conns": self.nconn,
                        "active": sum(1 for t in self.workers if t.is_alive()),
                        "queue": len(self.q), "inflight": infl2,
                        "retries": self.retries, "paused": self.paused.is_set(),
                        "lines": self._line_speeds(dtl),
                    })

                if time.monotonic() - last >= 3.0:
                    cur = statistics.mean(win) if win else 0
                    win = []
                    last = time.monotonic()
                    if cur > peak:
                        peak = cur
                        self.peak = cur
                    # ★ db 用位图 (见 bm_bytes 注释)。written 在本函数里只服务
                    #   于下面的记账体检, 不再参与任何对用户可见的数字。
                    db = self.bm_bytes()
                    # ── 记账体检 (MIROM_DEBUG_ACCT=1) ──
                    #   进度用位图(权威), written 是对照量。正常差值 ≈ 在途连接数 ×
                    #   半个单元 (实测 42MB / 128 连接); 若它涨到几百 MB, 说明
                    #   位图又退化成"整块分片才标记"了 —— 那正是进度虚高/暂停后
                    #   进度条继续爬的根因。这个开关就是它的看门狗。
                    if _DBG_ACCT:
                        _gap = self.written - db
                        self._gap_max = max(getattr(self, "_gap_max", 0), _gap)
                        self._won = max(getattr(self, "_won", 0), self.written - self.size)
                        log("   [记账] written %.1f MB | 位图(权威) %.1f MB | 虚高 %.1f MB "
                            "| 历史最大 %.1f MB | 超出文件大小 %.1f MB"
                            % (self.written / MB, db / MB, _gap / MB,
                               self._gap_max / MB, self._won / MB))
                    # ── 尾部退化处理 ──
                    #   实测 cdnorg 对某些文件会在中后段突然退化。此时队列往往已空,
                    #   剩余工作全被在途连接攥着, 光补连接没用 (新连接抢不到活就退出)。
                    #   做法: ①把【最大的若干个】在途分片切成剩余部分还回队列;
                    #        ②再补备用线路去认领。
                    #
                    #   ⚠ 踩过的坑: 最初是"让所有在途连接全部让位", 一次甩出 230+ 个
                    #     碎片, 4 次重排等于近千个新 HTTP 请求 → CDN 直接限流,
                    #     0.00 MB/s 卡死 + 重试飙到 144 次。所以必须:
                    #       · 只让位最大的 K 个, 不做全量重排
                    #       · 加冷却时间与次数上限
                    #       · 增线总量设上限, 避免把 CDN 打死
                    now_m = time.monotonic()
                    alive_n = sum(1 for t in self.workers if t.is_alive())
                    # 判定"并行度已塌": 在途分片数远低于初始连接数。
                    #   ⚠ 不要用"空闲连接数"来判 — 连接一旦领不到活就退出,
                    #     空闲数永远是 0, 条件永远不成立 (实测尾部因此无人补位,
                    #     总速度随连接数一路从 83 掉到 0.17 MB/s)。
                    #   实测每连接吞吐基本恒定 (~0.15-0.22 MB/s), 所以总速度
                    #   完全由"还有多少条连接在干活"决定 → 缺的是并行度, 不是带宽。
                    lost = self.inflight < self.nconn0 * 0.7
                    degraded = (peak > 20 * MB and cur < peak * 0.5
                                and (self.size - db) > 32 * MB
                                and now_m - self.t0 > 20)
                    if (degraded and lost and not self.q
                            and self.reshards < 1 and now_m - self.last_reshard > 15):
                        with self.hlock:
                            cands = sorted(self.holding.items(),
                                           key=lambda kv: -(kv[1][1] - kv[1][0]))
                        if len(cands) > 8:
                            # 让位个数 = 离满并行还差多少条连接, 多切一点给新连接留活
                            k = min(len(cands), max(24, self.nconn0 - self.inflight))
                            for i2, _ in cands[:k]:
                                self.donate[i2] = True
                            self.reshards += 1
                            self.last_reshard = now_m
                            log(c("   ⚠ 并行度塌陷: 队列空 / 仅 %d 个分片在途 (初始 %d 条连接) "
                                  "→ 让位最大的 %d 个分片 (第 %d 次)"
                                  % (self.inflight, self.nconn0, k, self.reshards), C_WARN))
                    if degraded and lost and self.alt and self.nconn < self.nconn0 * 1.6:
                        m = self.alt.pop(0)
                        add = min(64, max(32, self.nconn0 // 3))
                        add = min(add, int(self.nconn0 * 1.6) - self.nconn)
                        if add > 0:
                            for _ in range(add):
                                self.mirrors.append(m)
                                t = threading.Thread(target=self.worker,
                                                     args=(len(self.mirrors) - 1,), daemon=True)
                                t.start()
                                self.workers.append(t)
                            self.nconn += add
                            self.failover_log.append((m["tag"], add, cur / MB, peak / MB))
                            log(c("   ⚠ 吞吐 %.1f MB/s (峰值 %.1f), 追加线路 %s x%d 连接"
                                  % (cur / MB, peak / MB, m["tag"], add), C_WARN))
                    pct = db / self.size * 100
                    eta = (self.size - db) / cur if cur > 0 else 0
                    el = time.monotonic() - self.t0
                    alive = sum(1 for t in self.workers if t.is_alive())

                    # 旧的 3 秒进度包已移除 —— 采样已提前到每 0.5 秒一次
                    if TTY:
                        bar_n = int(pct / 2.5)
                        bar = BAR_FULL * bar_n + BAR_EMPTY * (40 - bar_n)
                        _safe_write("\r  [%s] %5.1f%% %7.2f MB/s  已用 %s  剩余 %s  "
                                    "连接 %3d 队列 %3d 在途 %3d 重试 %d   "
                                    % (bar, pct, cur / MB, hms(el), hms(eta),
                                       alive, len(self.q), self.inflight, self.retries))
                    elif pct >= self._next_log:
                        # 输出被重定向到文件 / 跑在 CI 里: 不要用 \r 刷屏,
                        # 改成每 10% 打一行, 日志可读且可 grep
                        self._next_log = (int(pct / 10) + 1) * 10
                        log("  %5.1f%% | %7.2f MB/s | 已用 %s | 剩余 %s | 连接 %d | 重试 %d"
                            % (pct, cur / MB, hms(el), hms(eta), alive, self.retries))
                    if time.monotonic() - saves > 15:
                        self.save_state()
                        saves = time.monotonic()
        except KeyboardInterrupt:
            if TTY:
                _safe_write("\n")
            log(c("收到 Ctrl+C, 正在保存断点...", C_WARN))
            self.stop.set()
        finally:
            if TTY:
                _safe_write("\n")
            for t in self.workers:
                t.join(timeout=6)
            # ★ 全部线程退出后统一刷一次盘 (而不是每个 worker 各刷一次)
            try:
                with open(self.out, "r+b") as f:
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                pass
            if not self.fatal:
                self.save_state()

        if self.fatal:
            log(c("存在失败分片: %s" % self.fatal[:3], C_BAD))
            log("可重新运行本命令续传")
            return False
        # ★ 完整性闸门: 尺寸校验挡不住"预分配留下的空洞", 必须逐单元确认都写过了
        with self.dlock:
            missing = [i for i, v in enumerate(self.done) if not v]
        if missing:
            log(c("❌ 有 %d 个分片未写入 (文件存在空洞)! 首缺位置: %s"
                  % (len(missing), ["%dMB" % (i * UNIT // MB) for i in missing[:8]]), C_BAD))
            log("   已保留断点, 重新运行本命令即可自动补下缺失分片")
            return False
        self.last_samples = samples
        return self.verify()

    def verify(self):
        if os.path.getsize(self.out) != self.size:
            log(c("尺寸校验失败", C_BAD))
            return False
        el = time.monotonic() - self.t0
        if TTY:
            _safe_write("\n")
        log(c("下载完成", C_OK))
        log("  大小   : %s (%d 字节)" % (human(self.size), self.size))
        log("  耗时   : %s" % hms(el))
        if self.ctr.n > 0 and el > 0:
            log("  平均   : %.2f MB/s" % (self.ctr.n / el / MB))
        s = getattr(self, "last_samples", [])
        core = s[2:-1] if len(s) > 8 else s
        if len(core) > 2 and statistics.mean(core) > 0:
            m = statistics.mean(core)
            log("  稳态   : 均值 %.2f MB/s | 区间 %.2f~%.2f | 抖动 CV %.1f%%"
                % (m / MB, min(core) / MB, max(core) / MB,
                   statistics.pstdev(core) / m * 100))
        if self.per:
            tot = sum(self.per.values()) or 1
            log("  各线路贡献:")
            for k in sorted(self.per, key=lambda x: -self.per[x]):
                log("     %-7s %8s (%.1f%%)" % (k, human(self.per[k]), self.per[k] / tot * 100))
        log("  重试   : %d 次" % self.retries)
        # 磁盘滞后判定: 多线程累计写盘时间 / (线程数 × 墙钟时间) ≈ 写盘占用率。
        # 接近 1 说明磁盘长期饱和 —— 此时真正的瓶颈是磁盘而不是网络。
        if el > 1 and self.nconn > 0:
            occ = self.disk_t / (el * self.nconn)
            if occ > 0.6:
                log(c("  磁盘   : 写盘占用率约 %.0f%% —— 瓶颈在磁盘, 换到更快的盘会更快"
                      % (occ * 100), C_WARN))
            else:
                log("  磁盘   : 写盘占用率约 %.0f%% (未成为瓶颈)" % (occ * 100))
        if self.failover_log:
            log("  线路切换:")
            for tag, n, cur, pk in self.failover_log:
                log("     %-7s +%d 连接  (切换时 %.1f MB/s, 峰值 %.1f MB/s)" % (tag, n, cur, pk))
        log("  文件   : %s" % self.out)
        return True


# ─────────────────────────── 校验 ───────────────────────────
def start_prealloc(out, size):
    """
    后台预分配磁盘空间。

    预分配(truncate)是纯磁盘操作, Windows 会真实写入一遍零: 5.8GB 约 11 秒。
    如果等到下载开始前才做, 这 11 秒就是纯等待。
    但紧接着的"探测各节点 + 单连接基准测速"有约 40 秒是纯网络等待,
    正好把磁盘操作藏进去 —— 下载开始时文件已经准备好了。
    """
    def _work():
        try:
            d = os.path.dirname(os.path.abspath(out))
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(out) and os.path.getsize(out) == size:
                return
            t0 = time.time()
            with open(out, "wb") as f:
                write_prealloc(f, size)
            log("   后台预分配完成 (%s / %.1f 秒, 已与测速阶段重叠)"
                % (human(size), time.time() - t0))
        except Exception as e:
            log("   后台预分配失败: %s (稍后会就地补做)" % e)
    t = threading.Thread(target=_work, daemon=True, name="prealloc")
    t.start()
    return t


HASH_IN_NAME = re.compile(r"_([0-9a-f]{8,32})(?:_[0-9][0-9.]*)?\.(?:tgz|tar\.gz|zip)$", re.I)
#   ⚠ 这里踩过坑: 卡刷包(Recovery ZIP)的命名是
#        miui_<机型>_<版本>_<hash>_<安卓版本>.zip
#     哈希后面还跟着 "_10.0", 不在扩展名正前方。
#     旧正则写死 "_<hash>.zip", 于是 miui_DAVINCI_20.5.6_028480154d_10.0.zip
#     匹配不到 → MD5 校验被静默跳过 → 等于没校验。已加可选后缀组。


def embedded_hash(filename):
    m = HASH_IN_NAME.search(filename)
    return m.group(1) if m else None


def verify_md5(path, expect_prefix):
    if not expect_prefix:
        return None, None
    h = hashlib.md5()
    total = os.path.getsize(path)
    done = 0
    step = 0
    t0 = time.time()
    with open(path, "rb") as f:
        while True:
            b = f.read(8 * MB)
            if not b:
                break
            h.update(b)
            done += len(b)
            pct = done / total * 100
            if pct >= step + 20:          # 每 20% 报一次, 避免日志刷屏
                step = pct
                log("    校验中 %d%%" % int(pct))
    sys.stdout.write("\r" + " " * 30 + "\r") if _stdout_ok() else None
    dig = h.hexdigest()
    ok = dig[:len(expect_prefix)].lower() == expect_prefix.lower()
    log("  MD5    : %s   (文件名内嵌: %s)  耗时 %.0fs" % (dig, expect_prefix, time.time() - t0))
    log("  校验   : %s" % (c("✅ 一致, 文件完整", C_OK) if ok else c("❌ 不一致! 文件可能损坏", C_BAD)))
    return dig, ok


# ─────────────────────────── 主流程 ───────────────────────────
def _undefined_names(path):
    """
    用标准库 symtable 扫出"引用了但根本没定义"的名字。

    ★ 为什么自检里要塞一个静态检查:
      本项目已经【两次】因为改名漏掉一个调用点而引入 NameError ——
      第一次是 seg_log 死控件, 第二次是 aborted→cancelled 改名时漏了两处。
      compile() 抓不到这种错 (它只查语法), 要到真正跑到那一行才炸,
      而且往往正好炸在"取消/出错"这种本来就少走的分支上, 测试很容易漏。
      symtable 是标准库, 不需要装 pyflakes, 却正好能查这一件事。
    """
    import builtins
    import symtable
    try:
        src = open(path, "r", encoding="utf-8").read()
    except Exception:
        return None                      # 读不到就跳过, 不误报
    try:
        st = symtable.symtable(src, path, "exec")
    except Exception as e:
        return ["<无法解析: %s>" % e]

    top = set()
    for s in st.get_symbols():
        if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_parameter():
            top.add(s.get_name())
    # 解释器注入的模块全局 —— 它们不在 builtins 里, 但永远存在, 属于误报源。
    top |= {"__file__", "__name__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__dict__", "__path__", "__debug__"}

    bad = []

    def walk(tbl):
        for s in tbl.get_symbols():
            nm = s.get_name()
            # is_global() = 这个名字不是本层的局部/自由变量 → 只能在模块层或内置里找
            if (s.is_referenced() and s.is_global()
                    and nm not in top and not hasattr(builtins, nm)):
                bad.append("%s() 内引用未定义的名字 '%s'"
                           % (tbl.get_name() or "<module>", nm))
        for c in tbl.get_children():
            walk(c)

    walk(st)
    # 去重但保持顺序
    seen, out = set(), []
    for b in bad:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def selftest():
    """
    内置自检 —— 不联网, 只验证纯逻辑部分。
    提交前跑一遍, 可以挡住绝大多数"改一处崩一处"的低级错误
    (例如本轮就靠它之外的手工测试抓到过 autotune 里一个元组解包错误)。
    """
    import tempfile
    fails = []

    def chk(name, cond, extra=""):
        print("  %-46s %s %s" % (name, "PASS" if cond else "FAIL", extra))
        if not cond:
            fails.append(name)

    print("mirom %s 自检" % VERSION)
    print("-" * 62)
    print("[URL 解析]")
    for url, ver, fn in [
        ("https://cdnorg.d.miui.com/V14.0.27.0.TMRCNXM/marble_images_V14.0.27.0.TMRCNXM_20231220.0000.00_13.0_cn_20c17bd222.tgz",
         "V14.0.27.0.TMRCNXM", "marble_images_V14.0.27.0.TMRCNXM_20231220.0000.00_13.0_cn_20c17bd222.tgz"),
        ("http://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip",
         "20.5.6", "miui_DAVINCI_20.5.6_028480154d_10.0.zip"),
    ]:
        i = parse_url(url)
        chk("parse_url -> %s" % fn[:32], i["version"] == ver and i["file"] == fn)

    # 带查询串/锚点的链接 —— 用户从论坛、脚本里复制粘贴出来的常见形态。
    # 旧实现把 "?token=X" 当成文件名的一部分, 后果有两个且都很隐蔽:
    #   ① Windows 下 '?' 非法 → 下到写盘那一刻才 OSError, 前面全白费;
    #   ② 内嵌 MD5 正则要求 .zip 结尾, 于是提取失败 → 【静默跳过校验】。
    for u, want_file, want_has_hash in [
        ("https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip?foo=bar",
         "miui_DAVINCI_20.5.6_028480154d_10.0.zip", True),
        ("https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip#frag",
         "miui_DAVINCI_20.5.6_028480154d_10.0.zip", True),
        ("https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip?a=1&b=2",
         "miui_DAVINCI_20.5.6_028480154d_10.0.zip", True),
    ]:
        j = parse_url(u)
        chk("带查询串/锚点仍取到正确文件名",
            j["file"] == want_file, "-> %s" % j["file"])
        chk("带查询串/锚点仍能提取内嵌 MD5",
            bool(embedded_hash(j["file"])) == want_has_hash,
            "-> %s" % embedded_hash(j["file"]))

    print("[文件名清洗]")
    for raw, want in [("a?b.zip", "a_b.zip"), ("a/b.zip", "a_b.zip"),
                      ("a:b*.zip", "a_b_.zip"), ("trail... ", "trail"),
                      ("ok_name.zip", "ok_name.zip"), ("a<b>c|d.zip", "a_b_c_d.zip")]:
        got = safe_name(raw)
        chk("safe_name(%r)" % raw[:16], got == want, "-> %r" % got)
    chk("safe_name 不产生非法字符",
        not any(ch in safe_name('a?*:<>|"b\\c/d.zip') for ch in '<>:"/\\|?*'), "")

    print("[路径预检]")
    try:
        chk("path_risk 正常路径返回 None",
            path_risk(os.path.join(tempfile.gettempdir(), "mirom_ok.bin")) is None, "")
    except Exception as e:
        chk("path_risk 正常路径返回 None", False, "%s: %s" % (type(e).__name__, e))
    _deep = os.path.join(tempfile.gettempdir(), "D" * 130, "E" * 130, "f.bin")
    _r = path_risk(_deep)
    if IS_WIN and not _long_paths_enabled():
        chk("path_risk 拦下过长路径", bool(_r) and "过长" in str(_r),
            "长度 %d" % len(os.path.abspath(_deep)))
    else:
        chk("path_risk 长路径 (本机已启用长路径, 允许通过)", True, "跳过")

    print("[文件名内嵌 MD5 提取]")
    for fn, exp in [
        ("x_images_V13.0.1.0.SJQINXM_20221216.0000.00_12.0_in_cd47902d89.tgz", "cd47902d89"),
        ("miui_DAVINCI_20.5.6_028480154d_10.0.zip", "028480154d"),
        ("miui_AGATEGlobal_OS1.0.17.0.UKWMIXM_2deb69168e_14.0.zip", "2deb69168e"),
        ("no_hash_here.tgz", None),
    ]:
        chk("embedded_hash -> %s" % exp, embedded_hash(fn) == exp, repr(embedded_hash(fn)))

    print("[镜像展开]")
    i = parse_url("https://bigota.d.miui.com/V1/x_images_V1_20200101.0000.00_10.0_cn_abcdef1234.tgz")
    alive = expand_mirrors(i)
    chk("默认跳过挂死节点", all(m["tag"] not in ("bigota", "hugeota") for m in alive),
        "共 %d 个" % len(alive))
    chk("展开后仍能取到全部 5 个", len(expand_mirrors(i, (), True)) == 5)
    chk("路径在所有镜像中一致", len({m["path"] for m in alive}) == 1)

    print("[磁盘探测]")
    d = tempfile.mkdtemp(prefix="mirom_selftest_")
    t, desc = detect_disk(os.path.join(d, "x.tgz"))
    chk("本地目录探测不抛异常", t in ("ssd", "hdd", "network", "removable", "unknown"), t)
    for dt in ("ssd", "hdd", "network", "removable", "unknown"):
        cc, ch, _ = disk_hint(dt)
        chk("disk_hint(%s) 参数合法" % dt, cc > 0 and ch >= 2, "%d 连接 / %dMB" % (cc, ch))

    print("[写入路径]")
    p = os.path.join(d, "prealloc.bin")
    try:
        with open(p, "wb") as f:
            write_prealloc(f, 4 * MB)
        chk("write_prealloc 得到正确大小", os.path.getsize(p) == 4 * MB,
            "%d 字节" % os.path.getsize(p))
    except Exception as e:
        chk("write_prealloc", False, str(e))

    print("[分片算法]")
    try:
        dl = Downloader([dict(tag="t", host="h", path="/p", referer=None)] * 8,
                        "/p", os.path.join(d, "q.bin"), 100 * MB, chunk=16 * MB)
        chk("初始化成功", True)
        chk("队列非空", len(dl.q) > 0, "%d 个分片" % len(dl.q))
        r1 = dl.take(4)
        chk("take 返回合法区间", r1 is not None and r1[0] == 0 and r1[1] < 100 * MB, str(r1))
        # 反复取空队列, 确认不会返回 None 以外的异常, 且总量守恒
        got, guard = [], 0
        while guard < 100000:
            r = dl.take(4)
            if r is None:
                break
            got.append(r)
            guard += 1
        covered = sum(e - s + 1 for s, e in got) + (r1[1] - r1[0] + 1)
        chk("分片总量守恒 (无丢失/无重复)", covered == 100 * MB,
            "覆盖 %d / %d" % (covered, 100 * MB))
        eff = dl._eff_chunk(10 * MB)
        chk("自适应分片落在 [2MB, 16MB]", 2 * MB <= eff <= 16 * MB, "%.1f MB" % (eff / MB))
    except Exception as e:
        chk("分片算法", False, "%s: %s" % (type(e).__name__, e))

    try:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass

    print("-" * 62)
    print("[无控制台环境 (PyInstaller --windowed)]")
    _so, _se = sys.stdout, sys.stderr
    _r = []
    try:
        sys.stdout = None
        sys.stderr = None
        # 注意: 此时连 chk() 里的 print 也会被静默丢弃, 所以先把结果攒起来,
        # 恢复 stdout 之后再统一打印 (否则这几项"通过"是看不见的)。
        _r.append(("stdout=None: _safe_write 不抛异常", _safe_write("x") is False, ""))
        _r.append(("stdout=None: _init_console 不抛异常", _init_console() is False, ""))
        _r.append(("stdout=None: _stdout_ok 返回 False", _stdout_ok() is False, ""))
        log("静默丢弃")           # log 走 _safe_write, 不能炸
        _r.append(("stdout=None: log() 不抛异常", True, ""))
    except Exception as e:
        _r.append(("stdout=None 兼容性", False, "%s: %s" % (type(e).__name__, e)))
    finally:
        sys.stdout, sys.stderr = _so, _se
    for _n, _c, _x in _r:
        chk(_n, _c, _x)

    print("-" * 62)
    print("[取消通道 (探测/调优阶段)]")
    # 回归用例: ① 探测 + ② 基准测速 期间 Downloader 还没创建, 此时点取消
    # 必须也能中断 —— 旧实现只认 Downloader.stop, 这段 40 秒窗口里取消无效,
    # 而 GUI 照样打印"已请求取消…"。下面三条守住这个行为。
    _orig = HOOKS.get("cancelled")
    try:
        chk("CLI 下 cancelled() 恒为 False (零开销)",
            cancelled() is False, "HOOKS['cancelled'] is None")
        HOOKS["cancelled"] = lambda: True
        chk("注入后 cancelled() 读到 True", cancelled() is True, "")
        HOOKS["cancelled"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        chk("回调抛异常时安全退化为 False", cancelled() is False, "不向上传播")
    finally:
        HOOKS["cancelled"] = _orig

    # Engine 侧: 没有 Downloader 时 cancel() 必须立标志并返回 True
    try:
        e = MiromEngine.__new__(MiromEngine)
        e._abort = threading.Event()
        e._lock = threading.Lock()
        e._dl = None
        r1 = MiromEngine.cancel(e)
        chk("无 Downloader 时 cancel() 返回 True (不再静默 False)",
            r1 is True, "旧实现返回 False")
        chk("无 Downloader 时 cancel() 已立下取消标志",
            MiromEngine.is_cancelled(e) is True, "")
        e2 = MiromEngine.__new__(MiromEngine)
        e2._abort = threading.Event()
        chk("全新 Engine 未被误标为已取消",
            MiromEngine.is_cancelled(e2) is False, "")
    except Exception as ex:
        chk("Engine 取消通道", False, "%s: %s" % (type(ex).__name__, ex))

    print("-" * 62)
    print("[静态体检: 未定义名]")
    try:
        _self_dir = os.path.dirname(os.path.abspath(__file__))
        for _fn in ("mirom.py", "mirom_gui.py", "vdesk.py"):
            _p = os.path.join(_self_dir, _fn)
            if not os.path.exists(_p):
                continue
            _u = _undefined_names(_p)
            if _u is None:
                continue
            chk("%s 无未定义名" % _fn, not _u, ("" if not _u else "; ".join(_u[:4])))
    except Exception as _e:
        chk("静态体检", False, "%s: %s" % (type(_e).__name__, _e))

    print("-" * 62)
    print("[记账: 位图 vs written]")
    # 守住"进度不虚高"这条不变式。
    # 背景: 位图曾经只在整块分片下完时标记, 导致进度虚高 27%, 且 written 能超过
    # 文件大小 → 进度条在还差 22MB 时就显示 100%。改成按单元增量标记后虚高 ~1.7%。
    # 这里用假对象直接验 _mark / done_n / bm_bytes 三者账目一致。
    try:
        import types
        f = types.SimpleNamespace()
        f.nunit, f.size = 16, 16 * UNIT
        f.done, f.done_n = bytearray(16), 0
        f.dlock = threading.Lock()
        f._mark = types.MethodType(Downloader._mark, f)
        f.bm_bytes = types.MethodType(Downloader.bm_bytes, f)
        f.done_bytes = types.MethodType(Downloader.done_bytes, f)

        chk("初始位图计数为 0", f.done_n == 0 and f.bm_bytes() == 0)
        f._mark(0, UNIT - 1)                       # 整块第 0 单元
        chk("标记 1 个单元 -> done_n=1", f.done_n == 1 and f.bm_bytes() == UNIT,
            "done_n=%d" % f.done_n)
        f._mark(0, UNIT - 1)                       # 重复标记
        chk("重复标记不重复计数", f.done_n == 1, "done_n=%d" % f.done_n)
        f._mark(UNIT, 3 * UNIT - 1)                # 单元 1..2
        chk("标记 [1,3) 单元 -> done_n=3", f.done_n == 3, "done_n=%d" % f.done_n)
        # 末尾残缺单元: e 越过文件尾 -> 最后一个单元也算完成
        f._mark(15 * UNIT, f.size - 1)
        chk("末单元特殊处理 -> done_n=4", f.done_n == 4, "done_n=%d" % f.done_n)
        chk("bm_bytes 被 min(size) 夹住", f.bm_bytes() <= f.size)
    except Exception as e:
        chk("记账不变式", False, "%s: %s" % (type(e).__name__, e))

    print("-" * 62)
    if fails:
        print("自检失败 %d 项: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("全部通过 ✅")
    return 0


def build_parser():
    """构造命令行解析器。单独抽出来是为了让 GUI 也能复用同一份参数定义
    (GUI 直接构造一个同字段的 Namespace 传给 run_flow, 不碰 sys.argv)。"""
    ap = argparse.ArgumentParser(
        description="小米 ROM 官方下载加速器 (破单连接限速 / 自动选线 / 并发调优)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法:")[1] if "用法:" in __doc__ else "")
    ap.add_argument("url", nargs="?",
                    help="任意一个小米 CDN 下载链接 (用 --selftest 时可省略)")
    ap.add_argument("-o", "--outdir", default=".", help="输出目录 (默认当前目录)")
    ap.add_argument("--save-as", default=None, metavar="NAME",
                    help="另存为指定文件名 (用于'已存在同名文件时下载副本')。"
                         "只影响本地文件名, 不影响 URL 与内嵌 MD5 的提取")
    ap.add_argument("--conns", type=int, default=0, help="手动指定总连接数 (默认自动调优)")
    ap.add_argument("--max-conns", type=int, default=256, help="自动调优上限 (默认 256)")
    ap.add_argument("--chunk-mb", type=int, default=16, help="初始分片 MB (默认 16)")
    ap.add_argument("--single", action="store_true",
                    help="只测试并使用单连接最快的线路 (最稳, 同时隐含 --no-failover)")
    ap.add_argument("--retune", action="store_true",
                    help="忽略 24 小时内的调优缓存, 强制重新爬坡")
    ap.add_argument("--insecure", action="store_true",
                    help="跳过 TLS 证书校验 (默认严格校验; 仅在企业代理等特殊环境使用)")
    ap.add_argument("--limit", type=float, default=0.0, metavar="MB/s",
                    help="总带宽上限, 单位 MB/s (默认 0 = 不限速)")
    ap.add_argument("--proxy", default=None, metavar="URL",
                    help="HTTP 代理, 如 http://127.0.0.1:7890 "
                         "(默认读取环境变量 HTTPS_PROXY / https_proxy)")
    ap.add_argument("--no-lock", action="store_true",
                    help="不创建单实例锁文件 (默认会加锁, 防止两个实例写坏同一文件)")
    ap.add_argument("--force", action="store_true",
                    help="目标文件已存在且校验通过时也强制重新下载")
    ap.add_argument("--multi", action="store_true", help="多线路聚合 (最快)")
    ap.add_argument("--no-failover", action="store_true",
                    help="禁用线路退化自动切换 (严格单线)")
    ap.add_argument("--probe", action="store_true", help="只测速, 不下载")
    ap.add_argument("--no-verify", action="store_true", help="跳过 MD5 校验")
    ap.add_argument("--add-host", action="append", default=[], help="追加自定义镜像 host")
    ap.add_argument("--include-dead", action="store_true",
                    help="也探测已知挂死的 bigota/hugeota (默认跳过)")
    ap.add_argument("--budget", type=float, default=120.0,
                    help="并发调优总时间预算(秒, 所有节点合计; 默认 120)")
    ap.add_argument("--no-color", action="store_true", help="禁用彩色输出")
    ap.add_argument("--no-prealloc", action="store_true", help="跳过磁盘预分配")
    ap.add_argument("--selftest", action="store_true",
                    help="运行内置自检 (不联网, 检查解析/正则/磁盘探测等) 后退出")
    ap.add_argument("-V", "--version", action="version",
                    version="mirom %s %s (Python %s, %s)"
                            % (VERSION, BYLINE, sys.version.split()[0], sys.platform))
    return ap


def run_flow(a):
    """
    实际流程: 探测 → 调优 → 下载 → 校验。
    入参 a 是 argparse.Namespace —— CLI 由 build_parser().parse_args() 产生,
    GUI 由 MiromEngine 自己合成同字段的 Namespace。返回进程退出码。

    ⚠ 这里【不能】调用 ap.error()/sys.exit() 这类会抛 SystemExit 的东西,
      否则后台线程里会静默死掉。参数校验一律返回非 0 退出码。
    """
    if not a.url:
        log(c("❌ 未提供下载链接", C_BAD))
        return 2

    # ── 取消检查点 ──
    #   探测(①)和单连接基准测速(②)合计要跑 40 秒以上, 期间 Downloader 尚未创建,
    #   靠 cancelled() 才能在阶段边界及时收手 (见 HOOKS["cancelled"] 的注释)。
    def bail():
        log(c("已取消 —— 断点已保留, 重跑同一条命令即可续传", C_WARN))
        return 130

    def cleanup_prealloc():
        if prealloc:
            try:
                prealloc.cancel()
            except Exception:
                pass
    if a.insecure:
        set_insecure(True)
    global PROXY
    pspec = (a.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if pspec:
        PROXY = parse_proxy(pspec)
        if PROXY:
            log("代理: %s:%d" % PROXY)
        elif pspec.lower().startswith("socks"):
            # parse_proxy 对 SOCKS 返回 None (见那里的注释)。这里给出【唯一】一句
            # 说明, 而不是"先宣布代理已设置、再警告它不能用"。
            log(c("❌ 不支持 SOCKS 代理 (本工具只会发 HTTP CONNECT)。"
                  "请改用 HTTP 代理, 或用 privoxy 把 SOCKS 转成 HTTP。\n"
                  "   已忽略该设置, 将尝试直连。", C_WARN))
        else:
            log(c("⚠ 无法识别的代理设置 %r, 已忽略" % pspec, C_WARN))

    # Unix 下把 SIGTERM 也转成可优雅保存断点的中断
    if not IS_WIN:
        try:
            import signal

            def _term(signum, frame):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, _term)
        except Exception:
            pass

    info = parse_url(a.url)
    mirrors = expand_mirrors(info, a.add_host, a.include_dead)
    print()
    log("小米 ROM 下载加速器  %s" % BYLINE)
    log("  版本目录 : %s" % info["version"])
    log("  文件名   : %s" % info["file"])
    log("  镜像节点 : %d 个%s" % (len(mirrors),
                                 "" if a.include_dead else " (已跳过已知挂死的 bigota/hugeota)"))
    print()

    # ── 1. 探测 ──
    stage("probe", "探测节点", 1)
    log(c("① 探测各节点可用性 (含假206挂死检测)", C_DIM))
    alive = []
    out = os.path.join(a.outdir, info["file"])
    # --save-as: 只改本地文件名, 不改 URL 路径, 也不改 MD5 的提取来源。
    # ⚠ 有个容易写错的地方: 内嵌 MD5 必须从【原始文件名 info["file"]】提取,
    #   不能从改名后的 out 里提 —— 副本名 "xxx (1).zip" 匹配不上正则,
    #   结果就是副本静默跳过校验。
    if getattr(a, "save_as", None):
        _sn = safe_name(str(a.save_as))
        if _sn:
            # 再查一次占用情况: 界面给的是"xxx (1).zip", 但用户可能已经有 (1) 了。
            # 这里自动挪到 (2)/(3)…, 绝不覆盖任何已有文件。
            out = unique_copy_path(os.path.join(a.outdir, _sn))
    size = 0
    prealloc = None

    # ── 输出路径预检 ──
    #   放在最前面: 路径太长/目录建不出来这种事, 必须在花掉两分钟探测+调优【之前】
    #   就告诉用户。--probe 不写盘, 跳过。
    if not a.probe:
        _risk = path_risk(out)
        if _risk:
            log(c("❌ 输出路径不可用", C_BAD))
            for _ln in str(_risk).split("\n"):
                log("   %s" % _ln)
            return 1

    # ── 单实例锁 ──
    #   必须在任何写盘动作之前拿到 (下面的后台预分配就会写盘)。
    #   GUI/EXE 场景下用户双击两次图标几乎是必然事件, 没有这把锁
    #   两个进程会各写各的分片, 最终文件必然损坏。
    lock = ""
    if not a.no_lock and not a.probe:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        except Exception:
            pass
        lock, lerr = acquire_lock(out)
        if lerr:
            log(c("❌ %s。若确认没有别的实例在跑, 请加 --no-lock。" % lerr, C_BAD))
            return 1
        if lock:
            global CURRENT_LOCK
            CURRENT_LOCK = lock
            atexit.register(release_lock, lock)

    # ── 测量结果记录 ──
    #   把探测/基准/爬坡的【结构化】结果攒起来, 通过 measure 钩子交给 UI。
    #   CLI 下钩子是 None, 零开销; GUI 下用来支持"导出测速数据"
    #   (旧实现只把这些数字打印成日志, 一条都没留, 所以没东西可导)。
    measure = {"url": a.url, "file": info["file"], "host": info["host"],
               "version": info["version"], "size": 0, "ts": time.time(),
               "probe": [], "baseline": [], "ramp": [], "chosen": None,
               "proxy": pspec or "", "limit_mbps": a.limit}

    def m_emit():
        emit("measure", measure)

    for m in mirrors:
        if cancelled():
            if prealloc:
                try:
                    prealloc.cancel()
                except Exception:
                    pass
            return bail()
        r = probe(m)
        q = m.get("quality", "?")
        measure["probe"].append({
            "tag": m["tag"], "host": m["host"], "quality": q,
            "ok": bool(r.get("ok")), "size": int(r.get("size") or 0),
            "ttfb_ms": r.get("ttfb_ms"), "dns_ms": r.get("dns_ms"),
            "tcp_ms": r.get("tcp_ms"), "server": r.get("server", ""),
            "range": bool(r.get("range")),
            # 劣质节点: 没有 ttfb 却有 stall_rate, 这个字段是它的核心特征
            "stall_kbps": round(r.get("stall_rate", 0) / 1024.0, 1)
                          if r.get("stall_rate") else None,
            "err": r.get("err"),
        })
        m_emit()
        if r["ok"]:
            alive.append((m, r))
            log("   %-7s %s %s  %s  TTFB %sms  %s"
                % (m["tag"], c(q, C_OK), c("可用", C_OK), human(r["size"]),
                   r["ttfb_ms"], r["server"]))
            if not size:
                size = r["size"]
                # ── 已有完整文件? 先校验, 通过就不必再下一次 ──
                #   旧行为: 只要没有断点文件就无脑重下 7GB, 哪怕文件早就下好了且完好。
                #   --force 可强制重下。
                if not a.force and not a.probe and not a.no_verify:
                    eh0 = embedded_hash(info["file"])
                    # ★ 关键判据: 【有没有断点文件】, 而不是"文件在不在、尺寸对不对"。
                    #   踩过的坑 (两边都踩了, 记录在这里):
                    #     · 取消后的文件是【预分配】过的, 尺寸和完整文件一模一样。
                    #       于是"同名文件已存在且尺寸相符"对每个续传任务都成立 ——
                    #       每按一次继续就要把整个 2.4GB 重算一遍 MD5 (7GB 要十几秒),
                    #       算完必然"不通过", 然后又去重下。续传等于白做。
                    #     · 更难查的是: 一旦在"不通过"分支里丢掉断点, 续传进度就没了。
                    #   正确逻辑:
                    #     有断点 → 那是续传, 交给 Downloader.load_state() 恢复, 别插嘴;
                    #     无断点 → 才可能是"上次下好了没删", 值得花一次 MD5 确认。
                    _sf0 = state_path(out)
                    has_state = os.path.exists(_sf0)
                    if (not has_state and eh0
                            and os.path.exists(out) and os.path.getsize(out) == size):
                        log("检测到同名文件已存在且尺寸相符, 先校验...")
                        _, good = verify_md5(out, eh0)
                        if good:
                            log(c("文件已完整且 MD5 通过, 无需重新下载 (要强制重下请加 --force)", C_OK))
                            return 0
                        # 无断点却校验不通过 = 文件坏了, 必须重下整份。
                        # (这里没有断点文件要清, 说清楚即可)
                        log(c("校验不通过, 将重新下载整份文件", C_WARN))
                # ★ 第一个节点探到大小就【立刻】开始后台预分配, 不等三个节点探完。
                #   原因: Windows 预分配要真写一遍零, 实测约 2~4 秒/GB ——
                #   7.11GB 花了 26.1 秒, 而"探测剩余节点 + 单连接测速"窗口只有 22 秒,
                #   于是露出 4 秒的 "等待后台预分配完成..." 阻塞 (实测抓到)。
                #   提前 ~10 秒起步, 才能把这段磁盘时间彻底藏进网络等待里。
                # ★ --probe 只测速, 绝不能在磁盘上留下一个整份大小的文件。
                #   实测踩过: 只跑 --probe 却留下 7.28GB 的全零 tgz。
                prealloc = None if (a.no_prealloc or a.probe) else start_prealloc(out, size)
        else:
            log("   %-7s %s %s  %s"
                % (m["tag"], c(q, C_WARN), c("不可用", C_BAD), r.get("err", "?")))
            if m.get("note"):
                log("           %s" % c(m["note"], C_DIM))
    if not alive:
        log(c("所有节点均不可用", C_BAD))
        return 1

    # 探测阶段结束 → 用户可能在探测期间就点了取消
    if cancelled():
        if prealloc:
            try:
                prealloc.cancel()
            except Exception:
                pass
        return bail()

    # ── 磁盘探测: 不假设用户的介质是 SSD ──
    dtype, dtext = detect_disk(out)
    cap_conns, hint_chunk, hint_text = disk_hint(dtype)
    log("磁盘: %s -> %s" % (a.outdir if a.outdir != "." else os.getcwd(), dtext))
    log("      %s" % c(hint_text, C_DIM))
    if dtype in ("network", "removable"):
        log(c("      ⚠ 该介质写入带宽有限, 已自动收敛并发, 否则写队列打满会导致重试激增", C_WARN))
    a.max_conns = min(a.max_conns, cap_conns)
    # 把文件/镜像/磁盘信息一次性交给 UI, 用于填「文件信息卡片」和「镜像徽章」
    emit("meta", {
        "version_dir": info["version"], "filename": info["file"],
        "size": size, "out": out,
        "disk_type": dtype, "disk_text": dtext,
        "disk_cap_conns": cap_conns, "disk_hint": hint_text,
        "md5_prefix": embedded_hash(info["file"]),
        "mirrors": [{"tag": t, "host": h, "quality": q, "note": n,
                     # enabled=False 的是被自动剔除的挂死节点。
                     # UI 要把它们也画出来并置灰 —— 否则用户看到自己给的
                     # bigota 链接"消失"了, 会以为程序没识别成功。
                     "enabled": any(mm["tag"] == t for mm in mirrors)}
                    for t, h, _r, q, n in CDN_HOSTS],
    })
    if a.chunk_mb > hint_chunk:
        log(c("提示: %s, 分片已由 %d MB 自动下调为 %d MB"
              % (hint_text.split(":")[0], a.chunk_mb, hint_chunk), C_WARN))
        a.chunk_mb = hint_chunk

    # ── 磁盘空间预检: 别下到 90% 才发现盘满了 ──
    if not a.probe:
        ok, free = check_space(out, size)
        if free:
            log("空间: 目标盘剩余 %s, 需要 %s" % (human(free), human(size)))
        if not ok:
            log(c("❌ 磁盘空间不足: 剩余 %s, 需要 %s (含 3%% 余量)。已中止。"
                  % (human(free), human(size * 1.03)), C_BAD))
            return 1

    # 预分配已在上面探测到大小后立即启动 (见 ① 里的 start_prealloc)
    print()

    # ── 2. 选线与调优 ──
    cache = load_tune()
    stage("tune", "并发调优", 2)
    log(c("② 单连接基准测速 (识别限速类型)", C_DIM))
    base = {}
    for m, r in alive:
        if cancelled():
            if prealloc:
                try:
                    prealloc.cancel()
                except Exception:
                    pass
            return bail()
        s = speed_test(m, 1, size, dur=7.0, warm=3.0)
        if s:
            base[m["tag"]] = s["MBps"]
            measure["baseline"].append({
                "tag": m["tag"], "nconn": 1, "MBps": round(s["MBps"], 2),
                "min": round(s.get("min", 0), 2), "max": round(s.get("max", 0), 2),
                "cv": round(s.get("cv", 0), 1), "errors": s.get("errors", 0),
                "limited": s["MBps"] < 3,
            })
            m_emit()
            log("   %-7s 单连接 %6.2f MB/s  %s"
                % (m["tag"], s["MBps"],
                   c("← 典型单连接限速, 必须靠并发", C_WARN) if s["MBps"] < 3 else c("← 单连接即较快", C_OK)))
    print()

    if a.conns:
        best_m, best_n = alive[0][0], a.conns
        log(c("③ 使用手动并发: %d 连接 × %s" % (best_n, best_m["tag"]), C_DIM))
    else:
        log(c("③ 并发爬坡找拐点 (%s)" % ("单线最稳" if a.single else "既快又稳"), C_DIM))
        cands = sorted(alive, key=lambda x: -base.get(x[0]["tag"], 0))
        if a.single:
            # --single: 只测「单连接基准最快」的那一条, 省掉对其余节点的爬坡。
            #   旧版本这个参数在 --help 里写着却从未被引用 —— 纯摆设, 已修。
            cands = cands[:1]
            log("  仅测试候选线路 %s (--single)" % cands[0]["tag"])
        best_m, best_n, best_sp, best_cv = None, 0, 0, 999
        levels = [64, 128, 192, 256]
        levels = [l for l in levels if l <= a.max_conns]
        if not levels:
            # --max-conns 比最低档还小时 (如 --max-conns 32), 旧版本得到空 levels
            # → 调优失败 → 回退写死的 128 连接, 直接突破用户设定的上限。已修。
            levels = [max(1, a.max_conns)]
        # budget 是【所有节点合计】的总预算, 不是每个节点各给一份 ——
        # 否则 3 个节点 × 90 秒 = 4.5 分钟, 对只想快速开下的人来说太久。
        # ── 复用上次的调优结果 ──
        #   旧版本 load_tune() 读出来的值从未被使用 (只写不读的死代码)。
        #   现在: 24 小时内测过同一节点就直接复用, 跳过几十秒爬坡 ——
        #   连续下同一机型的多个版本时体感提升明显。--retune 可强制重测。
        cached_hit = None
        if not a.retune:
            hits = []
            for m, _ in cands:
                cc = cache.get(m["host"])
                if cc and time.time() - cc.get("ts", 0) < 24 * 3600 and cc.get("conns"):
                    hits.append((m, min(int(cc["conns"]), a.max_conns),
                                 float(cc.get("MBps", 0)), float(cc.get("cv", 999.0)),
                                 time.time() - cc["ts"]))
            # ⚠ 多个节点都有缓存时, 必须用与爬坡【相同】的"既快又稳"准则来挑,
            #   不能按 cands 顺序取第一个命中的 (旧实现就是这样, 结果复用了
            #   比 cdnorg 更抖的 aliyun)。缺 cv 字段的老缓存按 999 处理, 自然落选。
            for h in hits:
                if (cached_hit is None
                        or h[2] > cached_hit[2] * 1.15
                        or (h[2] > cached_hit[2] * 0.85 and h[3] < cached_hit[3] * 0.75)):
                    cached_hit = h
        if cached_hit:
            m, cn, cmb, ccv, age = cached_hit
            best_m, best_n, best_sp, best_cv = m, cn, cmb, ccv
            log("  复用 %.0f 分钟前的调优结果: %s @ %d 连接 (%.1f MB/s, CV %.1f%%) —— 跳过爬坡"
                % (age / 60, m["tag"], best_n, cmb, ccv))
            log("  需要重新调优请加 --retune")
        else:
            # budget 是【所有节点合计】的总预算, 不是每个节点各给一份 ——
            # 否则 3 个节点 × 90 秒 = 4.5 分钟, 对只想快速开下的人来说太久。
            deadline = time.monotonic() + a.budget
            for m, _ in cands:
                if cancelled():
                    prealloc and prealloc.cancel()
                    return bail()
                remain = deadline - time.monotonic()
                if remain < 18:
                    log("  (调优总预算已用完, 跳过剩余节点)")
                    break
                log("  测试 %s ... (剩余预算 %.0fs)" % (m["tag"], remain))
                b, allv = autotune(m, size, levels, remain)
                # 爬坡的【每一档】都记下来 —— 拐点长什么样, 这是唯一的一手证据
                for lv in (allv or []):
                    measure["ramp"].append({
                        "tag": m["tag"], "nconn": lv.get("nconn"),
                        "MBps": round(lv.get("MBps", 0), 2),
                        "min": round(lv.get("min", 0), 2),
                        "max": round(lv.get("max", 0), 2),
                        "cv": round(lv.get("cv", 0), 1),
                        "errors": lv.get("errors", 0),
                    })
                m_emit()
                if not b:
                    continue
                # 选线准则 = 既快又稳 (用户的原始诉求就是"不要忽上忽下"):
                #   · 吞吐明显高出 15% → 换 (快到可以容忍抖动)
                #   · 速度相差在 ±15% 内, 但抖动不到其 3/4 → 换 (用稳定性换掉微小速度差)
                #   实测意义: aliyun 常比 cdnorg 快 5~8%, 但抖动是它的 2~3 倍,
                #   且尾部会突然塌陷; 这种"看起来快一点"的节点不该被选中。
                if (best_m is None
                        or b["MBps"] > best_sp * 1.15
                        or (b["MBps"] > best_sp * 0.85 and b["cv"] < best_cv * 0.75)):
                    best_m, best_n, best_sp, best_cv = m, b["nconn"], b["MBps"], b["cv"]
        if not best_m:
            fb = min(128, max(1, a.max_conns))
            log(c("调优失败, 回退 %d 连接 (受 --max-conns=%d 约束)" % (fb, a.max_conns), C_WARN))
            best_m, best_n = cands[0][0], fb
        else:
            log("  %s 最优: %s @ %d 连接 → %.2f MB/s (抖动 CV %.1f%%)"
                % (c("★", C_WARN), best_m["tag"], best_n, best_sp, best_cv))
            cache[best_m["host"]] = {"conns": best_n, "MBps": round(best_sp, 2),
                                     "cv": round(best_cv, 1), "ts": time.time()}
            save_tune(cache)
    measure["size"] = int(size)
    measure["chosen"] = {
        "tag": best_m.get("tag") if hasattr(best_m, "get") else None,
        "conns": int(best_n),
        "MBps": round(float(best_sp or 0), 2),
        "cv": (round(float(best_cv), 1) if best_cv not in (None, 999) else None),
    }
    m_emit()
    print()

    if a.probe:
        # 仅测速也要把结果送出去 —— 这正是"导出测速数据"最主要的使用场景
        m_emit()
        log("仅测速模式, 结束")
        return 0

    # 调优阶段结束 → 进下载前最后一道取消检查 (此后由 Downloader.stop 接管)
    if cancelled():
        if prealloc:
            try:
                prealloc.cancel()
            except Exception:
                pass
        return bail()

    # ── 3. 组装下载线路 ──
    if a.multi and len(alive) > 1:
        # 只纳入 ✓/△ 的线路。实测把 ⚠ 的 bn 一起聚合时:
        #   12 次连接失败, 尾部 91.8%→100% 硬拖 60 秒, 总耗时 1:33
        #   比单走 cdnorg 的 0:49 还慢一倍 —— 差线路会把整体拖下水。
        good = [m for m, _ in alive if m.get("quality") in ("✓", "△")]
        if len(good) < 2:
            good = [m for m, _ in alive][:1]
            log(c("可用线路不足, 退化为单线: %s" % good[0]["tag"], C_WARN))
        plan, each = [], max(16, best_n // len(good))
        for m in good:
            for _ in range(each):
                plan.append(m)
        alt = []
        log(c("④ 多线路聚合: %d 条线 × %d 连接 = %d (%s)"
              % (len(good), each, len(plan), " + ".join(m["tag"] for m in good)), C_DIM))
    else:
        plan = [best_m] * best_n
        # --single 明确要求"只用一条线", 因此同时关掉退化切换
        alt = [] if (a.no_failover or a.single) else [m for m, _ in alive if m["tag"] != best_m["tag"]]
        log(c("④ 主线路: %s @ %d 连接" % (best_m["tag"], best_n), C_DIM))
        if alt:
            log("   备用线路(退化时自动顶上): %s"
                % ", ".join("%s" % m["tag"] for m in alt))
    print()

    # ── 4. 下载 ──
    limiter = RateLimiter(a.limit * MB) if a.limit > 0 else None
    if limiter is not None:
        log("限速: %.2f MB/s (全局生效, 所有连接共享一个令牌桶)" % a.limit)
    d = Downloader(plan, info["path"], out, size, chunk=a.chunk_mb * MB, alt=alt,
                   prealloc=prealloc, limiter=limiter)
    emit("attach", d)      # 交给 GUI, 此后 pause/resume/cancel 才有对象可控
    stage("download", "下载", 3)
    ok = d.run()
    if not ok:
        return 1

    # ── 5. 校验 ──
    eh = embedded_hash(info["file"])
    if eh and not a.no_verify:
        print()
        stage("verify", "完整性校验", 4)
        log(c("⑤ 完整性校验 (文件名内嵌 MD5 前缀)", C_DIM))
        _, good = verify_md5(out, eh)
        if not good:
            return 2
    elif not eh:
        log(c("文件名中未发现内嵌 MD5, 跳过校验 (可用 --no-verify 忽略此提示)", C_WARN))

    # ★ 校验通过后删掉断点文件。
    #   留着它的坏处 (实测踩到): 位图是全满的, 于是"文件被改坏 → 重跑"时
    #   Downloader.load_state() 恢复出"全部已完成", 一个分片都不下, 最后又报
    #   校验失败 —— 用户重跑多少次都修不好, 只能手动去删两个文件。
    #   删掉之后重跑会走"检测到同名文件已存在 → 校验 → 通过就跳过"这条路:
    #   文件没坏就直接秒过, 坏了就重下整份。两条路都是对的。
    #   --no-verify 时不删: 那种情况下断点是唯一的正确性依据。
    if eh and not a.no_verify:
        try:
            _sf = state_path(out)
            if os.path.exists(_sf):
                os.remove(_sf)
                log("   断点文件已清理 (下载已完成并通过校验)")
        except Exception:
            pass

    print()
    log(c("全部完成 ✅", C_OK))
    return 0


def main():
    """CLI 入口: 解析参数 → 处理仅 CLI 关心的开关 → 跑流程。"""
    ap = build_parser()
    a = ap.parse_args()

    if a.no_color:
        global COLOR, C_OK, C_BAD, C_WARN, C_DIM, C_END
        COLOR = False
        C_OK = C_BAD = C_WARN = C_DIM = C_END = ""

    if a.selftest:
        return selftest()
    if not a.url:
        ap.error("需要提供小米 CDN 下载链接 (或使用 --selftest 运行自检)")
    return run_flow(a)


class MiromEngine(threading.Thread):
    """
    GUI 门面 (A 方案的接口层) —— 把整条流程跑在后台线程, 通过回调交给 UI。

    典型用法 (PySide6):
        self.eng = MiromEngine(url, outdir, conns=192)
        self.eng.on_log      = lambda s: self.sig_log.emit(s)
        self.eng.on_stage    = lambda k, l, i, n: self.sig_stage.emit(k, l, i, n)
        self.eng.on_meta     = lambda d: self.sig_meta.emit(d)
        self.eng.on_progress = lambda d: self.sig_prog.emit(d)   # 喂折线图
        self.eng.on_done     = lambda r: self.sig_done.emit(r)
        self.eng.on_error    = lambda s: self.sig_err.emit(s)
        self.eng.start()          # 非阻塞

        # 按钮直接接过去:
        self.eng.pause(); self.eng.resume(); self.eng.cancel()

    ⚠ 三个必须注意的点:
      1. 回调是在【后台线程】里被调用的 —— PySide6 里必须用 Signal 转到主线程,
         绝不能在回调里直接改控件, 否则界面会随机崩溃。
      2. 引擎实例【一次性】: 跑完/取消后要重新 new 一个, 不要复用。
      3. cancel() 之后不要立刻 new, 等 on_done/on_error 回调到了再说 (断点要写完)。
    """

    def __init__(self, url, outdir=".", conns=0, limit_mbps=0.0, proxy=None,
                 verify=True, include_dead=False, multi=False, single=False,
                 retune=False, insecure=False, no_lock=False, force=False,
                 budget=120.0, max_conns=256, probe=False, extra_args=None):
        threading.Thread.__init__(self, daemon=True, name="mirom-engine")
        self.result = None
        self._dl = None          # 当前 Downloader, 供 pause/resume/cancel 用
        self._lock = threading.Lock()
        # 线程级取消标志: Downloader 还不存在时 (探测/调优阶段) 靠它中断 run_flow
        self._abort = threading.Event()

        # ── UI 回调 (由调用方赋值) ──
        self.on_log = None       # fn(str)
        self.on_stage = None     # fn(key, label, index, total)
        self.on_meta = None      # fn(dict)
        self.on_progress = None  # fn(dict)   每 0.5s
        self.on_done = None      # fn(dict)
        self.on_error = None     # fn(str)
        self.on_measure = None   # fn(dict)  探测/基准/爬坡的结构化结果

        # 合成一个与 build_parser() 同字段的 Namespace, 直接喂给 run_flow
        d = build_parser().parse_args([url, "-o", outdir])
        d.conns = conns
        d.limit = limit_mbps
        d.proxy = proxy
        d.no_verify = not verify
        d.include_dead = include_dead
        d.multi = multi
        d.single = single
        d.retune = retune
        d.insecure = insecure
        d.no_lock = no_lock
        d.force = force
        d.budget = budget
        d.max_conns = max_conns
        d.probe = probe
        d.no_color = True
        # ★ extra_args: 覆盖/补充上面没显式暴露的 argparse 字段
        #   (no_prealloc / chunk_mb / add_host / budget 的微调等)。
        #   旧实现只把它存进 self._extra 就再也没读过 —— 纯死参数:
        #   调用方传了以为生效, 实际上参数被静默丢弃, 表现是"设置了却没反应"。
        #   校验字段名, 拼错要立刻报错而不是默默无效。
        _extra = dict(extra_args or {})
        _valid = set(vars(d))
        _bad = [k for k in _extra if k not in _valid]
        if _bad:
            raise ValueError("extra_args 含未知字段 %s; 可用字段: %s"
                             % (sorted(_bad), ", ".join(sorted(_valid))))
        for _k, _v in _extra.items():
            setattr(d, _k, _v)
        self.args = d
        self._extra = _extra

    # ---- 线程体 ----
    def run(self):
        prev = dict(HOOKS)
        HOOKS["log"] = self._h_log
        HOOKS["stage"] = self._h_stage
        HOOKS["meta"] = self._h_meta
        HOOKS["progress"] = self._h_prog
        HOOKS["attach"] = self.attach
        HOOKS["cancelled"] = self.is_cancelled
        HOOKS["measure"] = self._h_measure
        try:
            rc = run_flow(self.args)
            self.result = {"code": rc, "ok": rc == 0}
            if self.on_done:
                self.on_done(self.result)
        except SystemExit as e:                       # 参数类错误
            msg = "参数错误 (exit %s)" % getattr(e, "code", "?")
            if self.on_error:
                self.on_error(msg)
        except BaseException as e:                    # 任何异常都要交给 UI, 不能静默死
            import traceback
            if self.on_error:
                self.on_error("%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()))
        finally:
            HOOKS.update(prev)
            # 任务结束就主动放锁, 而不是拖到进程退出 —— 否则同一 GUI 会话里
            # 第二次下载会看到自己留下的锁文件 (即便可重入也已无意义)。
            try:
                if CURRENT_LOCK:
                    release_lock(CURRENT_LOCK)
            except Exception:
                pass

    # ---- 回调桥接 ----
    def _h_log(self, s):
        if self.on_log:
            self.on_log(s)

    def _h_stage(self, key, label, idx, total):
        if self.on_stage:
            self.on_stage(key, label, idx, total)

    def _h_meta(self, d):
        if self.on_meta:
            self.on_meta(d)

    def _h_prog(self, d):
        if self.on_progress:
            self.on_progress(d)

    def _h_measure(self, d):
        # 深拷贝一层再交出去: measure 是引擎里持续被改写的活对象, 直接给 UI
        # 会变成"UI 读的时候后台正在写"的数据竞争 (导出出来的 CSV 时对时错)。
        if not self.on_measure:
            return
        try:
            snap = {
                "url": d.get("url"), "file": d.get("file"), "host": d.get("host"),
                "version": d.get("version"), "size": d.get("size", 0),
                "ts": d.get("ts"), "proxy": d.get("proxy", ""),
                "limit_mbps": d.get("limit_mbps", 0),
                "probe": [dict(x) for x in (d.get("probe") or [])],
                "baseline": [dict(x) for x in (d.get("baseline") or [])],
                "ramp": [dict(x) for x in (d.get("ramp") or [])],
                "chosen": dict(d["chosen"]) if d.get("chosen") else None,
            }
        except Exception:
            return
        self.on_measure(snap)

    # ---- 供 run_flow 内部登记当前 Downloader, 以便暂停/取消 ----
    def attach(self, dl):
        with self._lock:
            self._dl = dl

    # ---- UI 控制 ----
    def pause(self):
        with self._lock:
            if self._dl:
                self._dl.pause()
                return True
        return False

    def resume(self):
        with self._lock:
            if self._dl:
                self._dl.resume()
                return True
        return False

    def cancel(self):
        # ★ 先立线程级标志, 再转发给 Downloader。
        #   起因 (实测抓到的真 bug): 探测节点/单连接基准测速这两个阶段跑在
        #   Downloader 创建【之前】, 此时 self._dl 还是 None —— 旧代码直接
        #   return False, 而 GUI 的 _cancel() 不看返回值, 照样打"已请求取消…"。
        #   于是用户点取消后界面骗他说在取消, 引擎却一路把探测(21s)+基准测速(22s)
        #   全跑完, 40 秒后才真的停下来。
        self._abort.set()
        with self._lock:
            if self._dl:
                self._dl.cancel()
                return True
        return True          # 标志已立, run_flow 会在下一个阶段边界退出

    def is_cancelled(self):
        return self._abort.is_set()

    def is_paused(self):
        with self._lock:
            return bool(self._dl and self._dl.is_paused())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断 (断点已保留, 重新运行本命令即可续传)")
        sys.exit(130)
