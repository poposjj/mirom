# -*- coding: utf-8 -*-
"""
把标准输出强制切成 UTF-8 —— 所有命令行工具脚本都该在第一行调用它。

★ 为什么必须有这个模块 (GitHub Actions 抓出来的真 bug, 而且是两次):
    UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-3
      File "tools/build.py", line 59, in main
        print("  打包 mirom.exe  v%s  by poposjj" % VERSION)

  英文版 Windows 的控制台代码页是 1252, 编不出中文 —— 任何一句中文 print 都会
  直接抛异常把脚本带崩。中文版 Windows 是 936, 编得出中文, 所以本地怎么跑都正常。
  这就是"只有换台机器才暴露"的典型。

  第一次只修了 mirom.py, CI 接着就在 tools/build.py 上炸了第二次 ——
  所以这次抽成共享模块: 以后新加工具脚本, 开头抄一行就行, 不用重新想一遍。

用法 (放在脚本最前面, 任何 print 之前):
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _console import force_utf8
    force_utf8()
"""
import sys

_IS_WIN = (sys.platform == "win32")
_ORIG_CP = None


def force_utf8():
    """
    强制标准输出/错误用 UTF-8, 并且永不为编码问题崩溃。

    两件事一起做:
      ① Windows: 把控制台输出代码页设成 65001 —— 只把字节按 UTF-8 写出去是不够的,
         终端仍会按旧代码页解释, 结果是乱码。必须让终端也切过去。
      ② 重新配置 sys.stdout/stderr 为 UTF-8。
         errors="replace" 是兜底: 万一某台机器还是编不出来, 最多显示成 ?,
         绝不再抛 UnicodeEncodeError —— 崩了连个提示都没有, 用户只会说"点了没反应"。
    """
    global _ORIG_CP
    if _IS_WIN:
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
            continue                              # PyInstaller --windowed 下就是 None
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Python 3.6 没有 reconfigure; 或 stdout 已被换成别的流对象。
            # 只是少了这层保险, 不该因此崩掉。
            pass
    return _ORIG_CP
