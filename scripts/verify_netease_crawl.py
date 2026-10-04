"""
端到端验证: 网易云搜索页能否真的抓到歌曲数据。

不走 Web 接口(避免 venv 启动器的不稳定性), 直接在进程内跑 crawl()。
判据是**提取到的记录数**与字段内容, 而不是"分析识别了几个候选"。
"""

import asyncio
import json
import pathlib
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


async def main() -> int:
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.crawler import SmartCrawler  # noqa: PLC0415

    settings = get_settings()
    settings.crawler.max_items = 60          # 别下 150 条的全部图片
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()
        print(f"  目标: {URL}\n")
        result = await crawler.crawl(URL, goal="抓取歌曲名称与链接", max_pages=1)

        print(f"\n  === 抓取结果 ===")
        print(f"    条数        = {result.item_count}")
        print(f"    errors      = {result.errors}")
        rule = result.rule
        if rule and rule.list_rule:
            print(f"    item_selector = {rule.list_rule.item_selector[:64]!r}")
            print(f"    字段          = {[f.name for f in rule.list_rule.fields]}")

        rows = result.items or []
        print(f"\n  === 前 5 条 ===")
        for r in rows[:5]:
            print(f"    {json.dumps(r, ensure_ascii=False)[:150]}")

        songs = [r for r in rows if "/song" in json.dumps(r, ensure_ascii=False)]
        titles = [r.get("title") for r in rows if r.get("title")]

        check(result.item_count > 0, "**提取到记录**", f"{result.item_count} 条")
        check(len(songs) > 0, "记录里含歌曲链接(/song)", f"{len(songs)} 条")
        check(len(titles) > 0, "记录里含歌曲名称", f"{len(titles)} 条, 例: {titles[:2]}")
    finally:
        await crawler.close()

    print("\n" + "=" * 70)
    if failures:
        print(f"网易云端到端验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("网易云端到端验收: 通过 ✓")
        print("  (iframe 内层数据已被提取, 不再是空外壳)")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
