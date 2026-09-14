# -*- coding: utf-8 -*-
"""
mirom 边界条件测试 —— 专打"正常路径不会走到"的分支。

为什么单独写一个:
  test_gui_buttons.py 走的是"用户正常操作"的 41 条路径。但断点续传、单实例锁、
  限速、异常状态文件这些东西, 恰恰是出错时才会碰到的 —— 而"出错时也得出对"
  才是这类下载工具的价值所在。这里的每一项都对应一个真实的失败模式。

用法:  python test_edge.py            (只跑纯逻辑, 不下载)
       python test_edge.py --net      (额外跑需要发起真实请求的项)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ★ 测试现在住在 tests\ 子目录, 而 mirom.py 在上一级。
#   只把 __file__ 所在目录加进 sys.path 的话, import mirom 会直接失败 ——
#   整理目录时最容易漏的就是这一处。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mirom as M

PASS = FAIL = 0
FAILED = []


def chk(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %-46s %s" % (name, detail))
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL  %-46s %s" % (name, detail))


def sect(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


URL = "https://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip"
SIZE = 8 * M.MB          # 用 8MB 的假目标, 不碰真实 CDN
TMP = tempfile.mkdtemp(prefix="mirom_edge_")


def make_dl(out, size=SIZE, bitmap=None, nunit=None):
    """造一个不走网络的 Downloader, 专门用来验状态读写与位图账目。
    注意 _eff_chunk/_seed 会用到 nconn 和 chunk, 必须一并给上, 否则 load_state
    会半路抛异常 —— 那验的就是异常分支, 不是正常恢复了。"""
    d = M.Downloader.__new__(M.Downloader)
    d.out = out
    d.size = size
    d.nunit = (size + M.UNIT - 1) // M.UNIT
    d.done = bytearray(d.nunit)
    d.done_n = 0
    d.dlock = M.threading.Lock()
    d.qlock = M.threading.Lock()
    d.q = M.deque()
    d.nconn = 128
    d.chunk = 16 * M.MB
    if bitmap:
        d.done = bytearray(bitmap)
        d.done_n = sum(d.done)
    return d


# ═══════════════════════════════════════════════════════════════════
sect("1. 断点状态文件: 损坏 / 不匹配 / 被删")
# ═══════════════════════════════════════════════════════════════════

def state_path(out):
    return out + ".mirom.json"


# 1a 数据文件与状态文件都在, 尺寸相符 -> 应恢复
out = os.path.join(TMP, "a.bin")
with open(out, "wb") as f:
    f.write(b"\0" * SIZE)
bm = bytearray(8)
for i in range(4):
    bm[i] = 1
d = make_dl(out)
d.save_state = lambda *a, **k: None          # save_state 需要更多属性, 这里只测 load
with open(state_path(out), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": bytes(bm).hex()}, f)
before = sum(d.done)
try:
    M.Downloader.load_state(d)
    chk("状态文件正常时能恢复位图", sum(d.done) == 4, "已标记 %d 单元" % sum(d.done))
    chk("恢复后 done_n 同步重算", d.done_n == 4, "done_n=%d" % d.done_n)
except Exception as e:
    chk("状态文件正常时能恢复位图", False, "%s: %s" % (type(e).__name__, e))

# 1b 尺寸不匹配 -> 必须丢弃 (否则续传会拼出坏文件)
out2 = os.path.join(TMP, "b.bin")
with open(out2, "wb") as f:
    f.write(b"\0" * SIZE)
d2 = make_dl(out2)
with open(state_path(out2), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE + M.MB, "unit": M.UNIT, "bitmap": bytes(bm).hex()}, f)
try:
    M.Downloader.load_state(d2)
    chk("尺寸不匹配时丢弃断点", sum(d2.done) == 0, "位图保持全 0")
    chk("丢弃后 done_n 也归零", d2.done_n == 0, "done_n=%d" % d2.done_n)
except Exception as e:
    chk("尺寸不匹配时丢弃断点", False, "%s: %s" % (type(e).__name__, e))

# 1c unit 粒度不匹配 -> 必须丢弃
out3 = os.path.join(TMP, "c.bin")
with open(out3, "wb") as f:
    f.write(b"\0" * SIZE)
d3 = make_dl(out3)
with open(state_path(out3), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT // 2, "bitmap": bytes(bm).hex()}, f)
try:
    M.Downloader.load_state(d3)
    chk("unit 粒度不匹配时丢弃断点", sum(d3.done) == 0, "位图保持全 0")
except Exception as e:
    chk("unit 粒度不匹配时丢弃断点", False, "%s: %s" % (type(e).__name__, e))

# 1d JSON 损坏 -> 不能抛异常
out4 = os.path.join(TMP, "d.bin")
with open(out4, "wb") as f:
    f.write(b"\0" * SIZE)
d4 = make_dl(out4)
with open(state_path(out4), "w", encoding="utf-8") as f:
    f.write("{这不是合法 JSON####")
try:
    M.Downloader.load_state(d4)
    chk("JSON 损坏时不抛异常", True, "静默忽略")
except Exception as e:
    chk("JSON 损坏时不抛异常", False, "%s: %s" % (type(e).__name__, e))

# 1e 位图 hex 损坏 -> 不能抛异常
out5 = os.path.join(TMP, "e.bin")
with open(out5, "wb") as f:
    f.write(b"\0" * SIZE)
d5 = make_dl(out5)
with open(state_path(out5), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": "zzzz"}, f)
try:
    M.Downloader.load_state(d5)
    chk("位图 hex 损坏时不抛异常", True, "静默忽略")
except Exception as e:
    chk("位图 hex 损坏时不抛异常", False, "%s: %s" % (type(e).__name__, e))

# 1f 位图长度对不上 -> 必须丢弃
out6 = os.path.join(TMP, "f.bin")
with open(out6, "wb") as f:
    f.write(b"\0" * SIZE)
d6 = make_dl(out6)
with open(state_path(out6), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": "0102"}, f)
try:
    M.Downloader.load_state(d6)
    chk("位图长度不符时丢弃", sum(d6.done) == 0, "位图保持全 0")
except Exception as e:
    chk("位图长度不符时丢弃", False, "%s: %s" % (type(e).__name__, e))

# 1g 状态文件存在但【数据文件被删】-> 必须丢弃 (否则会以为已下好)
out7 = os.path.join(TMP, "g.bin")
d7 = make_dl(out7)                      # 注意: 不创建数据文件
with open(state_path(out7), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": bytes(bm).hex()}, f)
try:
    M.Downloader.load_state(d7)
    allzero = sum(d7.done) == 0
    chk("数据文件被删时丢弃断点", allzero, "已标记 %d 单元" % sum(d7.done))
except Exception as e:
    chk("数据文件被删时丢弃断点", False, "%s: %s" % (type(e).__name__, e))

# 1h 数据文件尺寸被改小 -> 必须丢弃
out8 = os.path.join(TMP, "h.bin")
with open(out8, "wb") as f:
    f.write(b"\0" * (SIZE // 2))
d8 = make_dl(out8)
with open(state_path(out8), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": bytes(bm).hex()}, f)
try:
    M.Downloader.load_state(d8)
    chk("数据文件尺寸不符时丢弃", sum(d8.done) == 0, "已标记 %d 单元" % sum(d8.done))
except Exception as e:
    chk("数据文件尺寸不符时丢弃", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("1b. 续传判据: 有断点就不许跑全文件 MD5")
# ═══════════════════════════════════════════════════════════════════
# 回归用例。这条判据踩过两次坑, 必须钉死:
#   取消后的数据文件是【预分配】的 —— 尺寸和完整文件完全一样。所以
#   "文件在 + 尺寸对" 根本区分不出"下好了"和"下了一半", 只有断点文件能区分。
#   写错的后果有两个:
#     ① 每次续传都白算一遍全文件 MD5 (2.4GB 要 4 秒, 7GB 要十几秒), 算完必然
#        "不通过", 然后拿这个假结论去重下 —— 续传等于白做;
#     ② 更糟: 在"不通过"分支里顺手把断点删了, 直接把用户的续传进度毁掉。
try:
    import inspect
    src = inspect.getsource(M.run_flow)
    i = src.find("has_state")
    chk("run_flow 用断点文件存在性做判据", i > 0, "找到 has_state")
    # "先校验" 那次调用必须被 not has_state 守住
    seg = src[max(0, i - 400):i + 700]
    chk("无断点才做整份 MD5 预校验",
        "not has_state" in seg and "verify_md5" in seg, "")
    chk("有断点时不再无条件删除断点文件",
        "已丢弃失效断点" not in src, "")
except Exception as e:
    chk("续传判据检查", False, "%s: %s" % (type(e).__name__, e))

# 行为验证: 断点文件存在时, load_state 必须恢复出进度 (而不是被当成"从头开始")
out9 = os.path.join(TMP, "resume.bin")
with open(out9, "wb") as f:
    f.write(b"\0" * SIZE)                    # 模拟预分配后的半成品
d9 = make_dl(out9)
bm9 = bytearray(d9.nunit)
for i in range(d9.nunit // 2):
    bm9[i] = 1
with open(state_path(out9), "w", encoding="utf-8") as f:
    json.dump({"size": SIZE, "unit": M.UNIT, "bitmap": bytes(bm9).hex()}, f)
try:
    M.Downloader.load_state(d9)
    half = d9.nunit // 2
    chk("预分配半成品 + 断点 => 恢复进度",
        d9.done_n == half and d9.bm_bytes() == half * M.UNIT,
        "done_n=%d / %d" % (d9.done_n, half))
    chk("续传队列只覆盖未完成部分",
        sum((e - s + 1) for s, e in d9.q) == SIZE - half * M.UNIT,
        "队列 %d 字节" % sum((e - s + 1) for s, e in d9.q))
except Exception as e:
    chk("预分配半成品 + 断点 => 恢复进度", False, "%s: %s" % (type(e).__name__, e))

# 断点文件损坏时, 不能被误当成"有断点"而跳过整份校验 —— 必须真的重置
out10 = os.path.join(TMP, "resume_bad.bin")
with open(out10, "wb") as f:
    f.write(b"\0" * SIZE)
d10 = make_dl(out10)
with open(state_path(out10), "w", encoding="utf-8") as f:
    f.write("{坏掉的")
try:
    M.Downloader.load_state(d10)
    chk("断点损坏 => 清零并重建全量队列",
        d10.done_n == 0 and sum((e - s + 1) for s, e in d10.q) == SIZE,
        "done_n=%d 队列 %d 字节" % (d10.done_n, sum((e - s + 1) for s, e in d10.q)))
    chk("断点损坏 => 状态文件被清掉",
        not os.path.exists(state_path(out10)), "")
except Exception as e:
    chk("断点损坏 => 清零并重建全量队列", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("2. URL 解析边界")
# ═══════════════════════════════════════════════════════════════════

cases = [
    ("正常 bigota 链接", URL, True),
    ("cdnorg 链接", "https://cdnorg.d.miui.com/V14.0.27.0.TMRCNXM/"
                    "marble_images_V14.0.27.0.TMRCNXM_20240101.0000.00_13.0_cn_20c17bd222.tgz", True),
    ("http (非 https)", "http://bigota.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip", True),
    ("空串", "", False),
    ("非 URL", "hello world", False),
    ("只有域名", "https://bigota.d.miui.com/", False),
    ("带查询串", URL + "?foo=bar", True),
    ("带 URL 编码空格", URL.replace("20.5.6", "20.5.6%20x"), True),
]
for name, u, should_ok in cases:
    try:
        info = M.parse_url(u)
        ok = bool(info and info.get("file"))
    except Exception:
        ok = False
    chk("parse_url: %s" % name, ok == should_ok, "解析%s" % ("成功" if ok else "失败"))


# ═══════════════════════════════════════════════════════════════════
sect("3. 文件名内嵌 MD5 提取")
# ═══════════════════════════════════════════════════════════════════

hcases = [
    ("_cn_<10hex>.tgz", "marble_images_V14.0.27.0.TMRCNXM_20240101.0000.00_13.0_cn_20c17bd222.tgz",
     "20c17bd222"),
    ("_<10hex>_10.0.zip", "miui_DAVINCI_20.5.6_028480154d_10.0.zip", "028480154d"),
    ("无 hash", "miui_DAVINCI_20.5.6_10.0.zip", None),
    ("大小写混合", "x_ABCDEF1234_1.zip", "ABCDEF1234"),
    ("太短不算", "x_abc_1.zip", None),
]
for name, fn, want in hcases:
    got = M.embedded_hash(fn)
    chk("embedded_hash: %s" % name, got == want, "-> %s" % got)


# ═══════════════════════════════════════════════════════════════════
sect("4. 路径与文件名边界")
# ═══════════════════════════════════════════════════════════════════

# 4a 含空格与中文的目录
sp = os.path.join(TMP, "含 空 格 的 目 录")
os.makedirs(sp, exist_ok=True)
p = os.path.join(sp, "测试 文件.bin")
try:
    with open(p, "wb") as f:
        f.write(b"x" * 1024)
    chk("中文+空格路径可写", os.path.getsize(p) == 1024)
except Exception as e:
    chk("中文+空格路径可写", False, str(e))

# 4b 超长路径 (>260) —— Windows MAX_PATH 限制
#    这不是"功能坏了", 而是要确认失败时【能提前明确报错】, 而不是下到一半才炸。
long = os.path.join(TMP, "L" * 120)
try:
    os.makedirs(long, exist_ok=True)
    lp = os.path.join(long, "F" * 120 + ".bin")
    with open(lp, "wb") as f:
        f.write(b"x")
    chk("超长路径可写", os.path.exists(lp), "全长 %d 字符" % len(lp))
except Exception as e:
    # 记录为"已知平台限制", 只要能被预检拦下就不算失败
    try:
        warn = M.path_risk(lp)
    except Exception:
        warn = None
    chk("超长路径可写 (或能被预检拦下)", bool(warn),
        "%s -> %s" % (type(e).__name__, (warn or "无预检!")[:60]))

# 4c 磁盘探测对不存在的路径不能抛
try:
    dt, txt = M.detect_disk(os.path.join(TMP, "不存在", "x.bin"))
    chk("detect_disk 对不存在路径不抛", bool(dt), "-> %s" % dt)
except Exception as e:
    chk("detect_disk 对不存在路径不抛", False, "%s: %s" % (type(e).__name__, e))

# 4d 各磁盘类型的 hint 都合法。注意第二个返回值单位是 MB, 不是字节。
for dt in ("ssd", "hdd", "network", "removable", "unknown", "", None, "bogus"):
    try:
        c, ch_mb, t = M.disk_hint(dt)
        ok = (isinstance(c, int) and c >= 1
              and isinstance(ch_mb, int) and 1 <= ch_mb <= 64
              and isinstance(t, str) and t)
    except Exception as e:
        ok = False
        t = "%s: %s" % (type(e).__name__, e)
    chk("disk_hint(%r) 合法" % dt, ok, "%s 连接 / %s MB" % (c, ch_mb) if ok else str(t))


# ═══════════════════════════════════════════════════════════════════
sect("5. 空间检查")
# ═══════════════════════════════════════════════════════════════════

# 5a 需要 1 字节 -> 应该够
try:
    ok, msg = M.check_space(TMP, 1024)
    chk("check_space 小需求返回可写", ok is True, msg if isinstance(msg, str) else "")
except Exception as e:
    chk("check_space 小需求返回可写", False, "%s: %s" % (type(e).__name__, e))

# 5b 需要 10 PB -> 必须报不足, 且不能抛
try:
    ok, msg = M.check_space(TMP, 10 * 1024 * 1024 * M.MB)
    chk("check_space 巨量需求报不足", ok is False, str(msg)[:48])
except Exception as e:
    chk("check_space 巨量需求报不足", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("6. 限速器 (--limit) 精度")
# ═══════════════════════════════════════════════════════════════════

try:
    lim = M.RateLimiter(2.0 * M.MB)   # ⚠ 参数单位是【字节/秒】, 不是 MB/s
    t0 = time.monotonic()
    total = 0
    for _ in range(8):
        lim.consume(256 * 1024)
        total += 256 * 1024
    el = time.monotonic() - t0
    want = total / (2.0 * M.MB)
    dev = abs(el - want) / want * 100
    chk("RateLimiter 2MB/s 偏差 <15%", dev < 15,
        "%.2fs (期望 %.2fs, 偏差 %.1f%%)" % (el, want, dev))
except Exception as e:
    chk("RateLimiter 2MB/s 偏差 <15%", False, "%s: %s" % (type(e).__name__, e))

# 6b 初始配额必须为 0 (旧 bug: 一上来就放行一整秒的额度)
try:
    lim = M.RateLimiter(1.0 * M.MB)
    t0 = time.monotonic()
    lim.consume(1 * M.MB)             # 1MB @ 1MB/s 应该要等约 1 秒
    el = time.monotonic() - t0
    chk("RateLimiter 初始配额为 0", el > 0.6, "1MB@1MB/s 实耗 %.2fs" % el)
except Exception as e:
    chk("RateLimiter 初始配额为 0", False, "%s: %s" % (type(e).__name__, e))

# 6c 限速 0 / 负数 = 不限速, consume 必须立刻返回
for v in (0, 0.0, -1):
    try:
        lim = M.RateLimiter(v)
        t0 = time.monotonic()
        lim.consume(100 * M.MB)
        el = time.monotonic() - t0
        chk("RateLimiter(%s) 不限速" % v, el < 0.1, "实耗 %.3fs" % el)
    except Exception as e:
        chk("RateLimiter(%s) 不限速" % v, False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("7. 代理解析")
# ═══════════════════════════════════════════════════════════════════

pcases = [
    ("http://127.0.0.1:8080", True),
    ("http://user:pass@127.0.0.1:3128", True),
    ("127.0.0.1:8080", True),
    ("http://proxy.corp.local:80", True),
    ("", False),
    # SOCKS 必须返回 None: 本工具只会发 HTTP CONNECT, 把 SOCKS 端点当 HTTP 代理
    # 用会得到一堆看不懂的连接超时, 而不是"协议不支持"。
    ("socks5://127.0.0.1:1080", False),
    ("socks4://127.0.0.1:1080", False),
    ("socks://127.0.0.1:1080", False),
    ("SOCKS5://127.0.0.1:1080", False),
]
for spec, should in pcases:
    try:
        got = M.parse_proxy(spec)
        ok = bool(got) if should else not got
    except Exception:
        ok = False
    chk("parse_proxy(%r)" % spec[:32], ok, "")


# ═══════════════════════════════════════════════════════════════════
sect("8. 单实例锁")
# ═══════════════════════════════════════════════════════════════════

lock_target = os.path.join(TMP, "locktest.bin")
try:
    lk1, err1 = M.acquire_lock(lock_target)
    chk("首次加锁成功", bool(lk1) and not err1, lk1 or err1)
    # 同 PID 再拿一次 -> 必须可重入 (否则同一次 GUI 会话里第二次下载会被自己挡掉)
    lk2, err2 = M.acquire_lock(lock_target)
    chk("同 PID 可重入", err2 is None, err2 or "ok")
    if lk1:
        M.release_lock(lk1)
    chk("释放后锁文件被清掉", not os.path.exists(lk1), lk1)
except Exception as e:
    chk("单实例锁基本流程", False, "%s: %s" % (type(e).__name__, e))

# 8b 另一个进程占着锁 -> 本进程必须被拒
try:
    lkA, _ = M.acquire_lock(lock_target)
    script = (
        "import sys; sys.path.insert(0, %r); import mirom as M;"
        "lk, err = M.acquire_lock(%r);"
        "print('ERR' if err else 'OK')" % (os.path.dirname(os.path.abspath(M.__file__)),
                                           lock_target)
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                       timeout=60)
    got = (r.stdout or "").strip()
    chk("跨进程独占生效", got == "ERR", "子进程返回 %r" % got[-40:])
    if lkA:
        M.release_lock(lkA)
except Exception as e:
    chk("跨进程独占生效", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("9. 参数解析 (CLI)")
# ═══════════════════════════════════════════════════════════════════

ap = M.build_parser()
try:
    a = ap.parse_args([URL])
    chk("最小参数可解析", a.url == URL and a.outdir == ".", "")
except Exception as e:
    chk("最小参数可解析", False, str(e))

try:
    a = ap.parse_args([URL, "-o", TMP, "--conns", "64", "--limit", "10",
                       "--proxy", "http://127.0.0.1:8080", "--no-verify",
                       "--no-prealloc", "--no-lock", "--force", "--multi",
                       "--max-conns", "128", "--chunk-mb", "8", "--budget", "30"])
    chk("全参数组合可解析", a.conns == 64 and a.limit == 10.0 and a.max_conns == 128,
        "conns=%d limit=%s max=%d" % (a.conns, a.limit, a.max_conns))
except Exception as e:
    chk("全参数组合可解析", False, str(e))

# 9c 非法数值必须报错而不是静默接受
for bad in (["--conns", "abc"], ["--limit", "xyz"], ["--budget", "?"]):
    try:
        ap.parse_args([URL] + bad)
        ok = False
    except SystemExit:
        ok = True
    except Exception:
        ok = True
    chk("非法参数被拒 %s" % " ".join(bad), ok, "")

# 9d --conns 0 / 负数 不应炸
for v in ("0", "-5", "1000000"):
    try:
        a = ap.parse_args([URL, "--conns", v])
        ok = True
    except Exception:
        ok = False
    chk("--conns %s 不炸" % v, ok, "")


# ═══════════════════════════════════════════════════════════════════
sect("10. 调优缓存容错")
# ═══════════════════════════════════════════════════════════════════

# ⚠ 必须把 TUNE_CACHE 指到临时文件再测。
#   上一版直接改真实的 ~/.mirom_tune.json, 结果喂进去的垃圾字段没被还原干净,
#   下一个测试跑真实下载时在"调优缓存查询"那行崩了 (TypeError: float - str) ——
#   测试污染了真实状态, 还顺带掩盖了 load_tune 缺少字段校验这个真 bug。
_real_cache = M.TUNE_CACHE
M.TUNE_CACHE = os.path.join(TMP, "tune_cache.json")


def _write_tune(txt):
    with open(M.TUNE_CACHE, "w", encoding="utf-8") as f:
        f.write(txt)


# 10a 各种垃圾输入都必须安全退化, 绝不能抛
for bad_txt, label in [
    ("@@@ 不是 JSON @@@", "非 JSON"),
    ("[]", "顶层是数组"),
    ('"str"', "顶层是字符串"),
    ("123", "顶层是数字"),
    ('{"h": {"conns": "x", "cv": null, "ts": "y"}}', "字段类型全错"),
    ('{"h": {"conns": 128}}', "缺 ts"),
    ('{"h": null}', "值是 null"),
    ('{"h": []}', "值是数组"),
    ('{"h": {"conns": -1, "ts": 1}}', "conns 为负"),
    ('{"h": {"conns": 128, "ts": 1e12}}', "只有 conns+ts"),
]:
    _write_tune(bad_txt)
    try:
        r = M.load_tune()
        chk("load_tune 容忍: %s" % label, isinstance(r, dict), "-> %r" % (r,))
    except Exception as e:
        chk("load_tune 容忍: %s" % label, False, "%s: %s" % (type(e).__name__, e))

# 10b 合法条目必须被保留 (别把好的也一起丢了)
_write_tune('{"good.host": {"conns": 128, "MBps": 90.5, "cv": 13.1, "ts": 1e12}}')
try:
    r = M.load_tune()
    e0 = r.get("good.host") or {}
    chk("load_tune 保留合法条目",
        e0.get("conns") == 128 and abs(e0.get("MBps", 0) - 90.5) < 0.01,
        "-> %r" % (e0,))
except Exception as e:
    chk("load_tune 保留合法条目", False, "%s: %s" % (type(e).__name__, e))

# 10c 坏条目和好条目混在一起时, 只丢坏的
_write_tune('{"bad": {"conns": "x", "ts": "y"}, '
            '"good": {"conns": 64, "MBps": 50, "cv": 20, "ts": 1e12}}')
try:
    r = M.load_tune()
    chk("load_tune 只丢坏条目", ("good" in r) and ("bad" not in r),
        "留下 %s" % sorted(r))
except Exception as e:
    chk("load_tune 只丢坏条目", False, "%s: %s" % (type(e).__name__, e))

M.TUNE_CACHE = _real_cache

# 10d 真实缓存文件仍然可用
try:
    chk("真实 TUNE_CACHE 路径未被测试污染", M.TUNE_CACHE == _real_cache,
        M.TUNE_CACHE)
    r = M.load_tune()
    chk("真实缓存可正常读取", isinstance(r, dict), "%d 个条目" % len(r))
except Exception as e:
    chk("真实缓存可正常读取", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("11. 镜像展开")
# ═══════════════════════════════════════════════════════════════════

info = M.parse_url(URL)
try:
    ms = M.expand_mirrors(info, [], False)
    chk("默认展开跳过挂死节点", len(ms) == 3, "%d 个: %s" % (len(ms), [m["tag"] for m in ms]))
    chk("跳过的节点带 note 说明",
        all("note" not in m or m["note"] for m in ms), "")
except Exception as e:
    chk("默认展开跳过挂死节点", False, str(e))

try:
    ms = M.expand_mirrors(info, [], True)
    chk("--include-dead 展开全部 5 个", len(ms) == 5, "%d 个" % len(ms))
    paths = set(m["path"] for m in ms)
    chk("所有镜像 path 一致", len(paths) == 1, "%d 种" % len(paths))
except Exception as e:
    chk("--include-dead 展开全部 5 个", False, str(e))

try:
    ms = M.expand_mirrors(info, ["my.cdn.example.com"], False)
    chk("--add-host 生效", any(m["host"] == "my.cdn.example.com" for m in ms),
        "%d 个" % len(ms))
except Exception as e:
    chk("--add-host 生效", False, str(e))

# 11d 未知 host 的 URL 也应该能解析出路径
try:
    i2 = M.parse_url("https://unknown.d.miui.com/20.5.6/miui_DAVINCI_20.5.6_028480154d_10.0.zip")
    ms = M.expand_mirrors(i2, [], False)
    chk("未知 host 也能展开镜像", len(ms) >= 3, "%d 个" % len(ms))
except Exception as e:
    chk("未知 host 也能展开镜像", False, str(e))


# ═══════════════════════════════════════════════════════════════════
sect("11b. 副本命名 / --save-as / 磁盘探测缓存")
# ═══════════════════════════════════════════════════════════════════
# 用户需求: "如果下载文件夹有相同文件, 提醒之后创建副本下载"。
# 这里守住三件事:
#   ① 副本名要自动避让 (a.zip -> a (1).zip -> a (2).zip), 绝不覆盖已有文件
#   ② 改名后 MD5 必须仍从【原文件名】提取 —— 副本名匹配不上正则, 用错来源
#      就会静默跳过校验 (这是最容易写错的一处)
#   ③ 磁盘探测要缓存 —— 否则每次下载/测速/自检都起一个 PowerShell 子进程

_d = os.path.join(TMP, "copytest")
os.makedirs(_d, exist_ok=True)
_p0 = os.path.join(_d, "rom.zip")
open(_p0, "w").close()
chk("副本名避让: 首次 -> (1)",
    os.path.basename(M.unique_copy_path(_p0)) == "rom (1).zip",
    os.path.basename(M.unique_copy_path(_p0)))
open(os.path.join(_d, "rom (1).zip"), "w").close()
chk("副本名避让: (1) 占用 -> (2)",
    os.path.basename(M.unique_copy_path(_p0)) == "rom (2).zip",
    os.path.basename(M.unique_copy_path(_p0)))
open(os.path.join(_d, "rom (2).zip"), "w").close()
chk("副本名避让: (2) 占用 -> (3)",
    os.path.basename(M.unique_copy_path(_p0)) == "rom (3).zip",
    os.path.basename(M.unique_copy_path(_p0)))
_pnew = os.path.join(_d, "不存在.zip")
chk("副本名: 未占用时原样返回",
    M.unique_copy_path(_pnew) == _pnew, os.path.basename(M.unique_copy_path(_pnew)))
chk("副本名: 绝不返回已被占用的路径",
    not os.path.exists(M.unique_copy_path(_p0)), "")

# ② 改名后 MD5 来源
_fn = "miui_DAVINCI_20.5.6_028480154d_10.0.zip"
chk("原文件名能提出 MD5", M.embedded_hash(_fn) == "028480154d", "")
chk("副本名提不出 MD5 (所以必须用原名)",
    M.embedded_hash("miui_DAVINCI_20.5.6_028480154d_10.0 (1).zip") is None, "")
# run_flow 里必须对 info["file"] 取 hash, 不能对 out 取
try:
    import inspect as _ins
    _src = _ins.getsource(M.run_flow)
    chk("run_flow 用 info['file'] 提取 MD5 (不是改名后的 out)",
        'embedded_hash(info["file"])' in _src, "")
    chk("run_flow 支持 --save-as", "save_as" in _src, "")
except Exception as e:
    chk("run_flow MD5 来源检查", False, "%s: %s" % (type(e).__name__, e))

# ③ 磁盘探测缓存
try:
    _calls = [0]
    _orig_dtw = M._disk_type_windows
    _orig_has = M._DISK_CACHE_HAS if hasattr(M, "_DISK_CACHE_HAS") else None

    def _counted(p):
        _calls[0] += 1
        return _orig_dtw(p)
    M._disk_type_windows = _counted
    M._disk_cache.clear()
    for _ in range(4):
        M.detect_disk(os.path.join(_d, "x.bin"))
    chk("磁盘探测被缓存 (4 次调用只问系统 1 次)",
        _calls[0] <= 1, "实际起子进程 %d 次" % _calls[0])
    M._disk_type_windows = _orig_dtw
except Exception as e:
    chk("磁盘探测被缓存", False, "%s: %s" % (type(e).__name__, e))

# ④ 子进程绝不能弹窗 (用户报的"点测速弹出 PowerShell 又瞬间闪退")
try:
    import inspect as _ins2
    _s = _ins2.getsource(M._run_hidden)
    chk("_run_hidden 设置了 CREATE_NO_WINDOW",
        "CREATE_NO_WINDOW" in _s or "_CREATE_NO_WINDOW" in _s, "")
    chk("_run_hidden 设置了 SW_HIDE 双保险",
        "SW_HIDE" in _s or "wShowWindow" in _s, "")
    _s2 = _ins2.getsource(M._disk_type_windows)
    chk("PowerShell 调用走 _run_hidden",
        "_run_hidden(" in _s2 and "subprocess.run(" not in _s2, "")
    chk("subprocess 已在模块级导入", hasattr(M, "subprocess"), "")
except Exception as e:
    chk("子进程隐藏检查", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("12. 测量结果 (measure) 通道")
# ═══════════════════════════════════════════════════════════════════
# 用户报的 bug: "工具中保存测速数据, 这个功能无效"——
# 他先点仅测速再导出, 得到"暂无数据", 因为旧实现只导出下载时序, 而仅测速
# 压根不创建下载器。现在探测/基准/爬坡的结构化结果会经 measure 钩子送出来。
chk("HOOKS 含 measure 通道", "measure" in M.HOOKS, "")
chk("HOOKS['measure'] 默认为 None (CLI 零开销)", M.HOOKS.get("measure") is None, "")

_got = []
_prev_m = M.HOOKS.get("measure")
try:
    M.HOOKS["measure"] = lambda d: _got.append(d)
    M.emit("measure", {"probe": [{"tag": "x"}]})
    chk("emit('measure') 能送达", len(_got) == 1 and _got[0]["probe"][0]["tag"] == "x", "")
finally:
    M.HOOKS["measure"] = _prev_m

# 引擎必须把它桥接成 on_measure, 并且送出去的是【快照】——
# 直接送活对象会变成"UI 读的时候后台正在写", 导出的 CSV 时对时错。
try:
    e = M.MiromEngine.__new__(M.MiromEngine)
    seen = []
    e.on_measure = lambda d: seen.append(d)
    live = {"probe": [{"tag": "a"}], "baseline": [], "ramp": [], "chosen": None}
    e._h_measure(live)
    live["probe"].append({"tag": "b"})          # 后台继续改
    chk("on_measure 收到的是快照 (后续改动不影响)",
        len(seen[0]["probe"]) == 1, "快照 %d 条, 活对象 %d 条"
        % (len(seen[0]["probe"]), len(live["probe"])))
    e.on_measure = None
    e._h_measure(live)                           # 没接回调时不能炸
    chk("无 on_measure 回调时不抛异常", True, "")
except Exception as e:
    chk("on_measure 桥接", False, "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
sect("13. 编码: 非 UTF-8 控制台不能崩")
# ═══════════════════════════════════════════════════════════════════
# ★ 这是 GitHub Actions 抓出来的真 bug, 本地永远测不到:
#     UnicodeEncodeError: 'charmap' codec can't encode characters in position 12-13
#       File "mirom.py", line 2004, in selftest
#         print("mirom %s 自检" % VERSION)
#   英文版 Windows 的控制台代码页是 1252, 编不出中文 —— 于是任何一条命令都直接崩。
#   中文版 Windows 是 936, 编得出中文, 所以本地怎么跑都正常。
#   这就是"只有换台机器才暴露"的典型, 必须用子进程显式改掉编码来验。
_here = os.path.dirname(os.path.abspath(__file__))
_engine = os.path.join(os.path.dirname(_here), "mirom.py")
for enc in ("cp1252", "ascii", "cp437", "latin-1"):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = enc
    env.pop("NO_COLOR", None)
    try:
        r = subprocess.run(
            [sys.executable, "-u", _engine, "--selftest"],
            capture_output=True, timeout=180, env=env,
            cwd=os.path.dirname(_engine))
        out = (r.stdout or b"") + (r.stderr or b"")
        # 退出码 0 + 不能有 UnicodeEncodeError
        bad = b"UnicodeEncodeError" in out or b"charmap" in out
        chk("PYTHONIOENCODING=%s 下自检不崩" % enc,
            r.returncode == 0 and not bad,
            "exit=%s%s" % (r.returncode, " (有编码异常)" if bad else ""))
    except subprocess.TimeoutExpired:
        chk("PYTHONIOENCODING=%s 下自检不崩" % enc, False, "超时")
    except Exception as e:
        chk("PYTHONIOENCODING=%s 下自检不崩" % enc, False,
            "%s: %s" % (type(e).__name__, e))

# ★ 所有工具脚本也必须过关 —— 第一次只修了 mirom.py, CI 紧接着就在
#   tools/build.py 上炸了第二次。所以这里逐个脚本都跑一遍, 而不是只测引擎。
_root = os.path.dirname(_here)
_tools = [
    ("tools/build.py", ["--help"]),
    ("tools/make_logo.py", None),          # 直接跑, 会重新生成图标
    ("tools/publish_github.py", ["--dry-run"]),
    ("tools/release_github.py", ["--no-upload"]),
]
for rel, argv in _tools:
    p = os.path.join(_root, rel.replace("/", os.sep))
    if not os.path.exists(p):
        chk("工具脚本存在: %s" % rel, False, "文件缺失")
        continue
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"     # 模拟英文版 Windows
    env.setdefault("GH_TOKEN", "dummy-not-used")
    args = [sys.executable, "-u", p] + (argv or [])
    try:
        r = subprocess.run(args, capture_output=True, timeout=300, env=env,
                           cwd=_root)
        out = (r.stdout or b"") + (r.stderr or b"")
        bad = b"UnicodeEncodeError" in out or b"charmap" in out
        chk("cp1252 下 %s 不因编码崩" % rel, not bad,
            "exit=%s%s" % (r.returncode, " (有编码异常)" if bad else ""))
    except subprocess.TimeoutExpired:
        chk("cp1252 下 %s 不因编码崩" % rel, False, "超时")
    except Exception as e:
        chk("cp1252 下 %s 不因编码崩" % rel, False, "%s: %s" % (type(e).__name__, e))

# 每个入口脚本都必须自己调 force_utf8 —— 这是一条约定, 漏一个就复发
for rel in ("mirom.py", "tools/build.py", "tools/make_logo.py",
            "tools/publish_github.py", "tools/release_github.py"):
    p = os.path.join(_root, rel.replace("/", os.sep))
    try:
        txt = open(p, encoding="utf-8").read()
        chk("%s 调用了 force_utf8" % rel, "force_utf8()" in txt, "")
    except Exception as e:
        chk("%s 调用了 force_utf8" % rel, False, str(e))

try:
    import inspect as _ins13
    _src13 = _ins13.getsource(M)
    chk("有 _force_utf8 处理", "def _force_utf8" in _src13, "")
    chk("模块级就调用了 _force_utf8",
        "\n_force_utf8()" in _src13, "不能等 main() 才调")
    chk("reconfigure 带 errors=replace",
        'errors="replace"' in _src13, "兜底, 编不出也不能崩")
    chk("tools/_console.py 是共享实现",
        os.path.exists(os.path.join(_root, "tools", "_console.py")),
        "抽成共享模块才不会漏")
except Exception as e:
    chk("编码处理检查", False, "%s: %s" % (type(e).__name__, e))


# ★ 命令行参数在两个入口上必须一致。
#   踩过的坑: mirom.exe 的入口是【图形界面】, 而 --selftest 是 mirom.py 的参数。
#   敲 `mirom.exe --selftest` 时 GUI 静默忽略它、照常弹窗口然后一直等 ——
#   看起来就像"卡死了", 实测在开发中因此白等过两次几百秒的超时。
#   现在两个入口都认这组无界面参数, 这里守住这个契约。
_gui = os.path.join(os.path.dirname(_here), "mirom_gui.py")
for flag, want in (("--version", "mirom"), ("--selftest", "PASS")):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for entry, label in ((_engine, "mirom.py"), (_gui, "mirom_gui.py")):
        try:
            t0 = time.time()
            r = subprocess.run([sys.executable, "-u", entry, flag],
                               capture_output=True, timeout=180, env=env,
                               cwd=os.path.dirname(_engine))
            el = time.time() - t0
            out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")
            # 关键: 必须【快速返回】且带上内容 —— 超时或空输出都算失败
            chk("%s %s 能正常返回" % (label, flag),
                r.returncode == 0 and want in out and el < 120,
                "%.1fs, exit=%s" % (el, r.returncode))
        except subprocess.TimeoutExpired:
            chk("%s %s 能正常返回" % (label, flag), False,
                "超时 —— 参数被忽略了, 窗口在等 (这就是要防的坑)")
        except Exception as e:
            chk("%s %s 能正常返回" % (label, flag), False,
                "%s: %s" % (type(e).__name__, e))


# ═══════════════════════════════════════════════════════════════════
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 70)
print("总计: PASS %d  FAIL %d" % (PASS, FAIL))
if FAILED:
    print("失败项:")
    for f in FAILED:
        print("  - %s" % f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
