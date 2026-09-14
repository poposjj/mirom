# -*- coding: utf-8 -*-
"""
把 mirom 项目发布到 GitHub。

为什么用 REST API 而不是 git 命令:
  这台机器上没装 git (winget 可用, 但为了发一次仓库去装一套工具没必要)。
  GitHub 的 Git Data API 可以直接建仓库、上传 blob、拼 tree、提交、建分支,
  全程不需要本地 git。

安全约定 (重要):
  · token 只从环境变量 GH_TOKEN 读, 绝不写进任何文件、绝不打印
  · 本脚本不落盘任何凭据

用法:
  set GH_TOKEN=github_pat_xxx
  python tools/publish_github.py [--repo NAME] [--private] [--dry-run]
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

OWNER_FALLBACK = "poposjj"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 要上传的文件 (相对仓库根 -> 本地路径)
FILES = [
    ".gitignore",
    "LICENSE",
    "README.md",
    "mirom.py",
    "mirom_gui.py",
    "vdesk.py",
    "assets/logo.ico",
    "assets/logo.png",
    "assets/logo_512.png",
    "assets/logo_256.png",
    "assets/logo_128.png",
    "assets/logo_64.png",
    "assets/logo_48.png",
    "assets/logo_32.png",
    "assets/logo_24.png",
    "assets/logo_16.png",
    "assets/logo_src.webp",
    "docs/使用说明.txt",
    "tests/test_edge.py",
    "tests/test_settings.py",
    "tests/test_gui_buttons.py",
    "tools/打包.bat",
    "tools/make_logo.py",
    "tools/version_info.txt",
    "tools/publish_github.py",
    "tools/release_github.py",
]

API = "https://api.github.com"


def tok():
    t = os.environ.get("GH_TOKEN", "").strip()
    if not t:
        raise SystemExit("缺少环境变量 GH_TOKEN")
    return t


def call(method, path, body=None, ok=(200, 201, 204), retries=3):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok())
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "mirom-publisher")
    if data:
        req.add_header("Content-Type", "application/json")
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.status not in ok:
                    raise RuntimeError("HTTP %s: %s" % (r.status, raw[:300]))
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            last = "HTTP %s %s: %s" % (e.code, e.reason, detail)
            # 422 往往是"已经存在", 不该重试
            if e.code in (401, 403, 404, 409, 422):
                raise RuntimeError(last)
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def main():
    args = sys.argv[1:]
    repo = "mirom"
    private = False
    dry = False
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    if "--private" in args:
        private = True
    if "--dry-run" in args:
        dry = True

    print("=" * 62)
    print("发布 mirom 到 GitHub")
    print("=" * 62)

    # 1) 确认身份
    me = call("GET", "/user")
    owner = me.get("login") or OWNER_FALLBACK
    print("  账号    : %s" % owner)
    print("  仓库    : %s/%s  (%s)" % (owner, repo, "私有" if private else "公开"))

    # 2) 本地文件清点
    entries, total = [], 0
    for rel in FILES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            raise SystemExit("  缺少文件: %s" % p)
        sz = os.path.getsize(p)
        total += sz
        entries.append((rel, p, sz))
    print("  文件    : %d 个, 共 %.1f KB" % (len(entries), total / 1024.0))
    for rel, _p, sz in entries:
        print("      %-28s %8.1f KB" % (rel, sz / 1024.0))

    if dry:
        print("\n  [dry-run] 到此为止, 没有改动 GitHub。")
        return 0

    # 3) 仓库不存在就建
    try:
        call("GET", "/repos/%s/%s" % (owner, repo))
        print("\n  仓库已存在, 直接往里提交")
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        print("\n  仓库不存在, 创建中...")
        r = call("POST", "/user/repos", {
            "name": repo,
            "description": "小米 ROM 官方下载加速器 —— 把 1MB/s 的龟速下载变成 100MB/s  by poposjj",
            "private": bool(private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        print("    已创建: %s" % r.get("html_url"))

    # 4) 空仓库要先有一个初始提交, 否则 Git Data API 会拒绝建 blob:
    #      HTTP 409  {"message":"Git Repository is empty."}
    #    这是 GitHub 的硬约束 —— blob/tree 都得挂在某个提交之下。
    #    做法: 用 Contents API 塞一个占位 .gitignore 造出首个提交
    #    (Contents API 在空仓库上会自动创建 initial commit),
    #    紧接着那个大提交会把它覆盖成真正的 .gitignore。
    try:
        call("GET", "/repos/%s/%s/contents/README.md" % (owner, repo))
        print("\n  仓库已有内容, 跳过种子提交")
    except RuntimeError:
        print("\n  空仓库 -> 先造一个初始提交")
        call("PUT", "/repos/%s/%s/contents/.gitignore" % (owner, repo), {
            "message": "init",
            "content": base64.b64encode(b"# placeholder\n").decode("ascii"),
            "branch": "main",
        })
        print("    初始提交已建立")

    # 5) 逐个上传 blob
    print("\n  上传文件...")
    tree = []
    for rel, p, sz in entries:
        with open(p, "rb") as f:
            raw = f.read()
        # 文本用 utf-8 直接传, 二进制走 base64 —— GitHub 两种都收
        try:
            text = raw.decode("utf-8")
            is_text = b"\x00" not in raw
        except UnicodeDecodeError:
            is_text = False
            text = None
        body = {"content": text, "encoding": "utf-8"} if is_text else {
            "content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"}
        b = call("POST", "/repos/%s/%s/git/blobs" % (owner, repo), body)
        tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": b["sha"]})
        print("    %-28s -> %s" % (rel, b["sha"][:10]))

    # 5) 拼 tree
    t = call("POST", "/repos/%s/%s/git/trees" % (owner, repo), {"tree": tree})

    # 6) 建提交 (有父提交就接上, 没有就是初始提交)
    parents = []
    try:
        ref = call("GET", "/repos/%s/%s/git/ref/heads/main" % (owner, repo))
        parents = [ref["object"]["sha"]]
        print("\n  发现已有 main 分支, 追加提交")
    except RuntimeError:
        print("\n  main 分支还不存在, 这是初始提交")
    cbody = {"message": "mirom 1.0.0  by poposjj\n\n"
                        "小米 ROM 官方下载加速器。\n"
                        "自动识别被限速到 16KB/s 的 bigota/hugeota 节点, 高并发分片把\n"
                        "小米官网 1MB/s 的下载提升到 100MB/s 以上, 并做 MD5 完整性校验。",
             "tree": t["sha"]}
    if parents:
        cbody["parents"] = parents
    c = call("POST", "/repos/%s/%s/git/commits" % (owner, repo), cbody)
    print("  提交    : %s" % c["sha"][:10])

    # 7) 指向 main
    if parents:
        call("PATCH", "/repos/%s/%s/git/refs/heads/main" % (owner, repo),
             {"sha": c["sha"], "force": False})
    else:
        call("POST", "/repos/%s/%s/git/refs" % (owner, repo),
             {"ref": "refs/heads/main", "sha": c["sha"]})
    print("  分支    : main")

    # 8) 补上仓库元信息 (主题标签 + 主页)
    try:
        call("PATCH", "/repos/%s/%s" % (owner, repo), {
            "description": "小米 ROM 官方下载加速器 —— 把 1MB/s 的龟速下载变成 100MB/s  by poposjj",
            "homepage": "https://github.com/%s/%s" % (owner, repo),
            "has_issues": True,
        })
        call("PUT", "/repos/%s/%s/topics" % (owner, repo),
             {"names": ["xiaomi", "rom", "downloader", "python", "pyside6",
                        "accelerator", "miui", "multithread", "resume"]})
        print("  主题标签: 已设置")
    except RuntimeError as e:
        print("  (元信息设置跳过: %s)" % str(e)[:80])

    print("\n" + "=" * 62)
    print("  完成!  https://github.com/%s/%s" % (owner, repo))
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
