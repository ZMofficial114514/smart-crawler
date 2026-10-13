"""
网易云 iframe 场景端到端验收。

判据不止"分析到了 frame", 还要**真的抓到数据**:
  1. 结构分析选到 contentFrame(不是外壳);
  2. 内层 frame 元素数远多于外壳;
  3. 候选列表里出现歌曲行选择器;
  4. 走完整抓取流程后, 拿到 >= 20 条真实条目, 且字段有内容(不是空壳);
  5. 明确区分"站点限流返回空结果"与"框架抓不到" —— 前者必须被诊断出来。

第 5 条是关键: 之前几次失败都是**站点限流**(任意关键词都返回"未能找到相关搜索结果"),
而不是 iframe 逻辑有问题。这个脚本会把这个区别打出来, 避免下次又误判成框架 bug。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

URL = "https://music.163.com/#/search/m/?s=%E4%B8%9C%E4%BA%AC%E4%B8%8D%E5%A4%AA%E7%83%AD&type=1"

failures: list[str] = []
notes: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.crawler import SmartCrawler  # noqa: PLC0415

    settings = get_settings()
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()

        print("=== 1) 结构分析 ===")
        report, _stats = await crawler.analyze_only(URL, scroll_rounds=0)
        if report is None:
            check(False, "分析返回报告", "None")
            return 1
        stats = report.dom_stats or {}
        check(report.content_frame == "contentFrame", "**选中内容 frame contentFrame**",
              repr(report.content_frame))
        check(int(stats.get("total_elements") or 0) > 600,
              "DOM 元素数来自内层 frame(>600, 外壳约 336)",
              str(stats.get("total_elements")))
        sels = [c.item_selector for c in report.candidate_lists]
        song_sel = [s for s in sels if "srchsongst" in s or "item" in s]
        check(bool(song_sel), "候选里出现歌曲行选择器",
              f"{len(report.candidate_lists)} 个候选, 命中 {song_sel[:1]}")
        for s in sels[:3]:
            print(f"        {s}")

        print("\n=== 2) 内层 frame 是否真的加载了数据 ===")
        async with crawler._lock:  # noqa: SLF001
            page = await crawler.browser.new_page()
            try:
                await crawler.browser.goto(page, URL)
                await asyncio.sleep(8)
                inner = next((f for f in page.frames if f.name == "contentFrame"), None)
                if inner is None:
                    check(False, "页面上存在 contentFrame")
                else:
                    d = await inner.evaluate(
                        """() => ({
                            els: document.querySelectorAll('*').length,
                            songs: document.querySelectorAll('div.srchsongst > div.item').length,
                            text: (document.body ? document.body.innerText : '')
                                  .replace(/\\s+/g,' ').trim().slice(0, 200),
                        })"""
                    )
                    print(f"        内层元素 {d['els']}, 歌曲行 {d['songs']}")
                    print(f"        文本: {d['text'][:90]!r}")
                    empty_marker = ("未能找到相关搜索结果" in d["text"]
                                    or "没有找到相关内容" in d["text"])
                    if empty_marker:
                        # 这是**站点限流**, 不是框架问题 —— 必须说清楚
                        notes.append("站点返回空结果(限流): 任意关键词都会 0 结果, "
                                     "不是 iframe 逻辑的问题")
                        print("        **站点返回「未能找到相关搜索结果」= 被限流**")
                    else:
                        check(d["songs"] > 0, "内层 frame 有歌曲行", str(d["songs"]))
            finally:
                await page.close()

        print("\n=== 3) 完整抓取流程 ===")
        result = await crawler.crawl(URL, goal="抓取歌曲名称与链接", max_pages=1)
        items = result.items or []
        check(len(items) >= 20, "**抓到 >= 20 条**", f"{len(items)} 条")
        if items:
            first = items[0]
            non_empty = [k for k, v in first.items() if v not in ("", None, [], {})]
            check(len(non_empty) >= 3, "首条字段有实际内容", f"{non_empty}")
            for k, v in list(first.items())[:6]:
                print(f"        {k} = {str(v)[:60]!r}")
        check(not result.errors, "无错误", str((result.errors or [])[:2]))
    finally:
        await crawler.close()

    print("\n" + "=" * 68)
    for n in notes:
        print(f"  注意: {n}")
    if failures:
        print(f"网易云 iframe 端到端: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("网易云 iframe 端到端: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
