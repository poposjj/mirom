# -*- coding: utf-8 -*-
"""
从 assets/logo_src.webp 生成全套图标资源。

用法:  python make_logo.py

产出:
  assets/logo.png        512px 透明底 (窗口/关于页用)
  assets/logo_256.png    256px
  assets/logo_64.png      64px
  assets/logo_32.png      32px
  assets/logo.ico        16/24/32/48/64/128/256 多分辨率 (EXE 与任务栏用)

★ 关键点: 抠背景【不能】用"白色变透明"。
  这个 logo 是橙底白字, 里面的「mi」本身就是纯白的 —— 一律按颜色抠的话,
  字会被一起抠掉, 只剩一个橙色圆角方块, 看起来像图标坏了。
  正确做法是【从四个角泛洪填充】: 只有与画面边缘连通的白色才算背景,
  被橙色包住的那块白色自然保留。
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QRect  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
# ★ 本脚本住在 tools\ 里, 而 assets\ 在上一级 (项目根)。
#   原来写 HERE/assets 在整理目录之后会找不到源图。
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
SRC = os.path.join(ASSETS, "logo_src.webp")

# 判定"这算背景白吗"。真实背景是纯白 (255,255,255); logo 边缘有抗锯齿的
# 过渡像素, 阈值放宽一点才能把毛边一起收干净, 否则成品边上会留一圈白霜。
BG_MIN = 200


def alpha_stats(img):
    """返回 (全透明占比, 全不透明占比)。"""
    w, h = img.width(), img.height()
    b = bytes(img.bits())
    st = img.bytesPerLine()
    n0 = n255 = tot = 0
    for y in range(0, h, max(1, h // 120)):
        for x in range(0, w, max(1, w // 120)):
            a = b[y * st + x * 4 + 3]
            tot += 1
            if a == 0:
                n0 += 1
            elif a == 255:
                n255 += 1
    return n0 * 1.0 / max(1, tot), n255 * 1.0 / max(1, tot)


def load_and_cut(src=SRC):
    """
    读源图并返回 ARGB32 图像。

    ★ 实测结论: 这张源图【本身就已经是带抗锯齿的透明底】——
      四角 alpha=0, 边缘 1.6% 是半透明过渡像素, 41% 全透明 / 57% 全不透明。
      所以不需要抠背景。但代码保留泛洪分支: 万一将来换成白底图, 它必须能处理,
      而且绝不能退化成"白色一律变透明" —— 那样 logo 里的白色「mi」会被一起抠掉,
      只剩一个橙色圆角块 (这是最容易踩的坑, 见下面的注释)。
    """
    img = QImage(src)
    if img.isNull():
        raise SystemExit("读不到图片: %s" % src)
    img = img.convertToFormat(QImage.Format_ARGB32)
    trans, opaque = alpha_stats(img)
    print("  源图 alpha: 全透明 %.1f%% / 全不透明 %.1f%%" % (trans * 100, opaque * 100))

    if trans < 0.02:
        # 没有透明区域 => 是白底图, 需要抠。从四角泛洪, 只吃与边缘连通的白色。
        print("  -> 判定为实心背景, 执行四角泛洪抠图")
        img = _flood_cut(img)
    else:
        print("  -> 已是透明底, 跳过抠图 (白色「mi」原样保留)")

    # ★ 转预乘 alpha 再缩放。
    #   透明像素的 RGB 是残留值 (实测边缘是 208,205,206 配 alpha=0)。
    #   直接对非预乘图做平滑缩放, 这些残留色会被当成真实颜色参与插值,
    #   成品边缘会出现一圈灰白毛边。预乘之后它们的贡献是 0, 干净。
    return img.convertToFormat(QImage.Format_ARGB32_Premultiplied)


def _flood_cut(img):
    w, h = img.width(), img.height()
    ptr = img.bits()
    buf = bytearray(ptr)
    stride = img.bytesPerLine()

    def is_bg(x, y):
        o = y * stride + x * 4
        b, g, r = buf[o], buf[o + 1], buf[o + 2]      # QImage 小端: BGRA
        return r >= BG_MIN and g >= BG_MIN and b >= BG_MIN

    seen = bytearray(w * h)
    stack = []
    for x in range(w):
        for y in (0, h - 1):
            if is_bg(x, y) and not seen[y * w + x]:
                seen[y * w + x] = 1
                stack.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg(x, y) and not seen[y * w + x]:
                seen[y * w + x] = 1
                stack.append((x, y))
    n_bg = 0
    while stack:
        x, y = stack.pop()
        n_bg += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and is_bg(nx, ny):
                seen[ny * w + nx] = 1
                stack.append((nx, ny))
    print("     抠掉 %d / %d 像素 (%.1f%%), 被橙色包围的白色保留"
          % (n_bg, w * h, n_bg * 100.0 / (w * h)))
    for y in range(h):
        row = y * stride
        brow = y * w
        for x in range(w):
            if seen[brow + x]:
                o = row + x * 4
                buf[o] = buf[o + 1] = buf[o + 2] = buf[o + 3] = 0
    return QImage(bytes(buf), w, h, stride, QImage.Format_ARGB32).copy()


def trim_and_pad(img, margin_ratio=0.0):
    """裁掉四周全透明的空白, 再补成正方形 (图标必须正方形, 否则会被拉伸)。"""
    w, h = img.width(), img.height()
    ptr = img.bits()
    buf = bytes(ptr)
    stride = img.bytesPerLine()
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * stride
        for x in range(w):
            if buf[row + x * 4 + 3] > 8:          # alpha
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < x0 or y1 < y0:
        return img
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    side = max(cw, ch)
    pad = int(side * margin_ratio)
    canvas = QImage(side + pad * 2, side + pad * 2, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.drawImage(QRect(pad + (side - cw) // 2, pad + (side - ch) // 2, cw, ch),
                img, QRect(x0, y0, cw, ch))
    p.end()
    print("  裁边: %dx%d -> 内容 %dx%d -> 方形 %dx%d"
          % (w, h, cw, ch, canvas.width(), canvas.height()))
    return canvas


def scaled(img, size):
    return img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def write_ico(path, img, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """
    手写多分辨率 ICO。

    ★ 为什么要自己拼: Qt 的 ico 写入器只会存【单张】图。而 Windows 会在不同场合
      挑不同尺寸 —— 任务栏 32、桌面大图标 256、Alt+Tab 48、资源管理器小图标 16。
      只塞一张 256 的话, 小尺寸是系统硬缩出来的, 会糊。
      ICO 容器本身很简单: 6 字节头 + 每张 16 字节目录项 + 图像数据。
      Vista 以后允许目录项直接放 PNG 数据, 所以不用自己做 BMP/掩码。
    """
    blobs = []
    for s in sizes:
        tmp = os.path.join(ASSETS, "_tmp_%d.png" % s)
        scaled(img, s).save(tmp, "PNG")
        with open(tmp, "rb") as f:
            blobs.append((s, f.read()))
        os.remove(tmp)

    n = len(blobs)
    header = struct.pack("<HHH", 0, 1, n)          # reserved, type=icon, count
    offset = 6 + 16 * n
    entries, data = b"", b""
    for s, blob in blobs:
        dim = 0 if s >= 256 else s                 # 256 在 ICO 里用 0 表示
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(blob), offset)
        data += blob
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(header + entries + data)
    return [s for s, _ in blobs]


def main():
    if not os.path.exists(SRC):
        raise SystemExit("缺少源图: %s" % SRC)
    print("源图: %s" % SRC)
    img = load_and_cut()
    img = trim_and_pad(img, margin_ratio=0.02)

    made = []
    # 小尺寸用稍大的留白, 否则在 16px 下圆角会贴边显得很挤
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        out = os.path.join(ASSETS, "logo_%d.png" % size)
        scaled(img, size).save(out, "PNG")
        made.append(out)
    # 主图 = 512, 代码里按需缩放
    main_png = os.path.join(ASSETS, "logo.png")
    scaled(img, 512).save(main_png, "PNG")
    made.append(main_png)

    ico = os.path.join(ASSETS, "logo.ico")
    sizes = write_ico(ico, img)
    made.append(ico)

    print("\n产出:")
    for p in made:
        print("  %-34s %7d 字节" % (os.path.basename(p), os.path.getsize(p)))
    print("  %-34s %7d 字节  尺寸=%s" % (os.path.basename(ico),
                                         os.path.getsize(ico), sizes))


if __name__ == "__main__":
    main()
