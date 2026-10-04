"""
补测: 网易云搜索页的翻页是否真的可用。

分析阶段已报"分页 ✓", 但"识别到"不等于"点得动、等得到、提得出"。这里端到端跑
max_pages=2, 比对:
  - result.pages_crawled 是否 > 1
  - 第 2 页是否真的带来新记录(而不是重复第 1 页后去重成 0 新增)
  - 记录的 /song?id= 是否出现第 1 页没有的 id
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

URL = "https://music.163.com/#/search/m/?s=on%20my%20way&type=1"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def song_ids(items: list[dict]) -> set[str]:
    out: set[str] = set()
    for it in items:
        blob = json.dumps(it, ensure_ascii=False)
        out.update(re.findall(r"/song\?id=(\d+)", blob))
    return out


async def main() -> int:
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.crawler import SmartCrawler  # noqa: PLC0415

    settings = get_settings()
    settings.crawler.max_items = 120
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()

        # 先只要 1 页, 拿到基线歌曲集合
        print("  === 第 1 页(基线) ===")
        r1 = await crawler.crawl(URL, goal="抓取歌曲名称与链接", max_pages=1)
        ids1 = song_ids(r1.items or [])
        print(f"    条数={r1.item_count}  页数={r1.pages_crawled}  歌曲 id 数={len(ids1)}")
        check(r1.item_count > 0, "第 1 页有数据", f"{r1.item_count} 条")

        # 再翻 2 页
        print("\n  === 翻 2 页 ===")
        r2 = await crawler.crawl(URL, goal="抓取歌曲名称与链接", max_pages=2)
        ids2 = song_ids(r2.items or [])
        new_ids = ids2 - ids1
        print(f"    条数={r2.item_count}  页数={r2.pages_crawled}  歌曲 id 数={len(ids2)}")
        print(f"    相比第 1 页新增歌曲 id = {len(new_ids)}")
        rule = r2.rule
        if rule and rule.pagination:
            print(f"    next_selector = {str(rule.pagination.next_selector)[:72]!r}")
            print(f"    max_pages     = {rule.pagination.max_pages}")

        check(r2.pages_crawled and r2.pages_crawled > 1,
              "**真的翻了页**(pages_crawled > 1)", str(r2.pages_crawled))
        check(len(new_ids) > 0,
              "**第 2 页带来了新记录**(不是重复第 1 页)",
              f"新增 {len(new_ids)} 首")
    finally:
        await crawler.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"网易云翻页验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("网易云翻页验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
