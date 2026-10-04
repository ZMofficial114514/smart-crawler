"""
验证 5xx 误报修复。

用**真实页面文本**与一组构造样本, 确认:
  1. HTTP 200 的正常页面(正文含 "50"/"500"/"504" 之类数字)不再被判成服务端错误;
  2. 真的 5xx 页面(HTTP 500, 正文含 "500 Internal Server Error")**仍然**被判出来;
  3. 构造的假阳性样本(如 "投稿超过 50 万件"、尺寸 "500x500")不再命中 5xx 模式。

第 2、3 条是**反向验证**: 只证明"不再误报"是不够的, 还要证明真错误没被一起放过。
"""

import asyncio
import json
import pathlib
import re
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.async_api import async_playwright  # noqa: E402

from smartcrawler.access_control import _KEYWORDS  # noqa: E402

SESSION = pathlib.Path("data/session.json")
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
SEARCH = ("https://www.pixiv.net/search?q=%E5%88%9D%E9%9F%B3%E3%83%9F%E3%82%AF"
          "&s_mode=tag&type=artwork&r=1")

#: 取 5xx 那条模式
SERVER_PATTERNS = [(p, l) for p, l, k, _w in _KEYWORDS if k == "server_error"]

#: 应该**命中**的文本(真错误)
SHOULD_HIT = [
    "500 Internal Server Error",
    "HTTP 502",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "服务器内部错误",
    "500 错误",
]
#: 应该**不命中**的文本(正常页面里的数字)
SHOULD_MISS = [
    "投稿超过 50 万件",
    "投稿超过 500 万件",
    "尺寸 500x500",
    "共 504 个作品",
    "收藏 502 次",
    "浏览 5031",
    "pixiv 500万用户",
    "作品 ID 5001234",
]

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def hits(text: str) -> list[str]:
    out = []
    for pat, label in SERVER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            out.append(label)
    return out


async def main() -> int:
    print("=== 1) 正则层面: 真错误应命中 / 正常数字不应命中 ===")
    for t in SHOULD_HIT:
        h = hits(t)
        check(bool(h), f"命中: {t!r}", str(h))
    for t in SHOULD_MISS:
        h = hits(t)
        check(not h, f"不命中: {t!r}", str(h))

    print("\n=== 2) 端到端: 真实 pixiv 搜索页(HTTP 200)不应报服务端错误 ===")
    if not SESSION.exists():
        print("  (没有会话, 跳过)")
        return 1 if failures else 0

    state = json.loads(SESSION.read_text(encoding="utf-8"))
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.crawler import SmartCrawler  # noqa: PLC0415

    settings = get_settings()
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()
        report, _stats = await crawler.analyze_only(SEARCH)
        if report is None:
            print("  分析失败(页面没打开)")
            return 1
        issue = report.access_issue
        print(f"    标题 = {report.title!r}")
        if issue is None:
            check(True, "没有产生访问诊断(即未判定为受限)")
        else:
            print(f"    issue_type={issue.issue_type} confidence={issue.confidence} "
                  f"detected={issue.detected}")
            print(f"    理由 = {issue.reasons}")
            check(issue.issue_type != "server_error",
                  "**未被判成 server_error**", f"实际 {issue.issue_type}")
            check(not (issue.detected and issue.issue_type == "server_error"),
                  "没有误报服务端错误")
    finally:
        await crawler.close()

    print("\n" + "=" * 66)
    if failures:
        print(f"5xx 误报修复验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("5xx 误报修复验收: 通过 ✓")
        print("  (真错误仍能识别, 正常页面里的数字不再误报)")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
