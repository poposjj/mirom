# 改了什么

<!-- 一两句话说明这个 PR 做了什么、为什么 -->

## 类型

- [ ] 修 bug
- [ ] 新功能
- [ ] 文档
- [ ] 重构 / 性能
- [ ] 其他：

## 自查清单

提交前请确认：

- [ ] `python mirom.py --selftest` 全部通过
- [ ] `python tests/test_edge.py` 通过（108 项，不需要联网）
- [ ] `python tests/test_settings.py` 通过
- [ ] 如果改了打包相关的东西，`python tools/build.py` 能跑出可用的 exe
- [ ] 新增的行为有对应的测试；修的 bug 有回归测试
- [ ] 代码注释写的是**为什么**这么做，而不是复述代码在做什么

## 特别留意

这个项目里几处**看起来可以简化、但简化就会出事**的地方，改动时请留意：

- **进度必须用位图算，不能用 `written`**：`written` 含重下/重排的重复计数，
  实测能虚高 27%，还会超过文件大小导致进度条提前显示 100%
- **续传判据是「有没有断点文件」**，不是「文件在不在、大小对不对」——
  取消后的文件是预分配过的，大小和完整文件一模一样
- **取消必须是线程级的**：探测/调优阶段还没有 `Downloader`，
  只挂 `Downloader.stop` 的话那 40 秒里取消无效
- **子进程必须 `CREATE_NO_WINDOW`**：冻结的 `--windowed` EXE 没有控制台，
  Windows 会给子进程新开一个黑框
- **`parse_url` 的 `path` 要保留查询串，文件名要剥掉**：文件名里带 `?`
  在 Windows 上是非法的，而且会让内嵌 MD5 提取失败（静默跳过校验）
- **不要排除 `PySide6.QtOpenGL`**：pyqtgraph 的 `PlotCurveItem` 会 import 它

## 相关 issue

<!-- 例：Closes #12 -->
