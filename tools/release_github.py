# -*- coding: utf-8 -*-
"""
把打包好的 dist\\mirom 压成 zip 并发布到 GitHub Releases。

为什么二进制不发进仓库:
  dist\\mirom 有 373 个文件 / 156 MB。放进 git 会让仓库永久背着一个巨大的历史,
  而且 GitHub 对单文件有 100MB 硬限制。正确做法是: 源码进仓库, 成品进 Releases。

用法:
  set GH_TOKEN=github_pat_xxx
  python tools/release_github.py            # 打包并发布 v1.1.0
  python tools/release_github.py --no-upload  # 只压 zip, 不上传
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

_HERE0 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE0)
from _console import force_utf8      # noqa: E402

force_utf8()          # 英文版 Windows 上中文输出会崩, 见 _console.py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist", "mirom")
REPO = "poposjj/mirom"
TAG = "v1.1.0"
API = "https://api.github.com"


def tok():
    t = os.environ.get("GH_TOKEN", "").strip()
    if not t:
        raise SystemExit("缺少环境变量 GH_TOKEN")
    return t


def call(method, path, body=None, ok=(200, 201, 204)):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "mirom-release")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))


def make_zip():
    if not os.path.isdir(DIST):
        raise SystemExit("先打包: %s 不存在" % DIST)
    out = os.path.join(ROOT, "dist", "mirom-%s-win64.zip" % TAG.lstrip("v"))
    print("  压缩中... (156 MB, 需要一两分钟)")
    t0 = time.time()
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for dirpath, _dirs, files in os.walk(DIST):
            for fn in files:
                fp = os.path.join(dirpath, fn)
                arc = os.path.join("mirom", os.path.relpath(fp, DIST))
                z.write(fp, arc)
                n += 1
    sz = os.path.getsize(out)
    print("  %d 个文件 -> %.1f MB (压缩率 %.0f%%), 耗时 %.0fs"
          % (n, sz / 1048576.0, sz * 100.0 / max(1, sum(
              os.path.getsize(os.path.join(dp, f))
              for dp, _d, fs in os.walk(DIST) for f in fs)), time.time() - t0))
    return out


def upload_asset(rel, path):
    """
    上传（或替换）附件。

    ★ 必须处理"同名附件已存在": 版本号不变、只是重新构建时, 附件名是一样的,
      GitHub 会直接返回 422 already_exists。所以要先删掉旧的那个再传 ——
      删了再传才有更新效果, 否则用户下到的还是上一版 exe。
    """
    name = os.path.basename(path)
    size = os.path.getsize(path)
    upload_url = rel["upload_url"]

    # 先清掉同名旧附件
    for a in rel.get("assets", []) or []:
        if a.get("name") == name:
            print("  替换已存在的附件: %s (旧 %d 字节)" % (name, a.get("size", 0)))
            call("DELETE", "/repos/%s/releases/assets/%d" % (REPO, a["id"]),
                 ok=(200, 204))

    url = upload_url.replace("{?name,label}", "?name=%s" % urllib.parse.quote(name))
    print("  上传 %s (%.1f MB)..." % (name, size / 1048576.0))
    t0 = time.time()
    with open(path, "rb") as f:
        data = f.read()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Bearer " + tok())
    req.add_header("Content-Type", "application/zip")
    req.add_header("Content-Length", str(size))
    req.add_header("User-Agent", "mirom-release")
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            j = json.loads(r.read())
        print("    完成: %s  (%.0fs, %.1f MB/s)"
              % (j.get("browser_download_url"), time.time() - t0,
                 size / 1048576.0 / max(0.1, time.time() - t0)))
        return j
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))


def main():
    import urllib.parse                    # noqa: F401  (upload_asset 里用到)
    globals()["urllib"].parse = urllib.parse

    zip_path = make_zip()
    if "--no-upload" in sys.argv:
        print("\n  [--no-upload] 已生成: %s" % zip_path)
        return 0

    print("\n  创建 Release %s ..." % TAG)
    body = """## mirom 1.1.0  ·  by poposjj

小米 ROM 官方下载加速器 —— 把官网 1 MB/s 的龟速下载变成 **100 MB/s**。

### 这个版本能做什么

- **自动识别劣质节点**：`bigota` / `hugeota` 会返回正常的 `206` 响应，实际却被限速到 ~16 KB/s。普通下载工具会被骗进去然后卡死
- **高并发分片 + 工作窃取**：数百条连接抢同一个共享分片队列，快的多干活
- **尾部防塌陷**：检测到并行度塌陷时让位重排 + 补备用线路
- **断点续传**：随时中断，重开自动接着下
- **MD5 校验**：从文件名内嵌的哈希前缀比对，确认文件完整
- **按盘型自适应**：自动区分固态 / 机械 / 网络盘 / 可移动介质

### 下载哪个

| 文件 | 说明 |
|---|---|
| `mirom-1.1.0-win64.zip` | **Windows 10/11 64 位，解压即用，免安装** |

> ⚠️ 解压后必须**整个文件夹一起用**，不能只拷 `mirom.exe` —— 同级 `_internal` 目录里是运行库。

### 怎么用

1. 解压，双击 `mirom.exe`
2. 把小米 ROM 下载链接粘到最上面的输入框
3. 点「开始下载」

第一次用建议先点「**仅测速**」，它只探测节点速度、不写任何文件。

### 实测成绩（千兆网络）

| 机型 | 大小 | 耗时 | 平均速度 |
|---|---|---|---|
| marble | 6.898 GB | 1:39 | 71 MB/s |
| renoir | 5.795 GB | 1:30 | 65 MB/s |
| davinci | 2.380 GB | 0:42 | 57 MB/s |

全部通过文件名内嵌 MD5 校验。

---

完整使用教程与实现思路见 [README](https://github.com/poposjj/mirom#readme)。
"""
    try:
        rel = call("POST", "/repos/%s/releases" % REPO, {
            "tag_name": TAG,
            "name": "mirom 1.1.0  by poposjj",
            "body": body,
            "draft": False,
            "prerelease": False,
        })
        print("  已创建: %s" % rel["html_url"])
    except RuntimeError as e:
        if "422" not in str(e):
            raise
        rel = call("GET", "/repos/%s/releases/tags/%s" % (REPO, TAG))
        print("  Release 已存在, 复用它: %s" % rel["html_url"])

    upload_asset(rel, zip_path)
    print("\n" + "=" * 62)
    print("  完成!  https://github.com/%s/releases" % REPO)
    print("=" * 62)
    return 0


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
