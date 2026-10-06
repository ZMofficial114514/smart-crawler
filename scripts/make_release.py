"""为已推送的 tag 创建 GitHub Release, 说明直接取自 CHANGELOG 对应小节。

**为什么从 CHANGELOG 取而不是另写一份**: 两处各写一遍必然会漂移 —— 发布说明说 A、
CHANGELOG 说 B, 之后没人知道哪个是准的。CHANGELOG 是唯一来源。
"""

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = "ZMofficial114514/smart-crawler"
TAG = sys.argv[1] if len(sys.argv) > 1 else "v1.2.2"


def token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "SC_GITHUB__TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            print(f"  用 {name} 认证")
            return value
    return ""


def changelog_section(version: str) -> str:
    """从 CHANGELOG.md 里抠出这个版本的详细小节(到下一个 `## [` 为止)。"""
    text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        raise SystemExit(f"CHANGELOG.md 里找不到 [{version}] 小节")
    return m.group(1).strip()


def main() -> int:
    version = TAG.lstrip("v")
    body = changelog_section(version)
    print(f"  取到 {version} 小节: {len(body)} 字符")

    payload = {
        "tag_name": TAG,
        "name": f"SmartCrawler {TAG}",
        "body": body,
        # 允许之后手工编辑(CHANGELOG 小节是技术向的, 发布页可能要再润色)
        "draft": False,
        "prerelease": False,
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "dsh",
        "Content-Type": "application/json",
    }
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.load(resp)
        print(f"  ✓ Release 已创建: {data.get('html_url')}")
        return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        print(f"  ✗ 创建失败 HTTP {exc.code}: {detail}")
        if exc.code in (401, 403):
            print("  -> 需要带写权限的 token。请设置环境变量 GITHUB_TOKEN 后重跑:")
            print(f"     $env:GITHUB_TOKEN='<你的 token>'; python {sys.argv[0]} {TAG}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
