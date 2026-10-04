"""
验证 iframe 下探: 网易云音乐搜索页是否终于能分析到内层 frame 的 150 首歌。

判据(不只看"有没有候选"):
  - report.content_frame 是否为 g_iframe 的 name (contentFrame)
  - 候选列表数量、DOM 元素数、图片数(内层 frame 应远多于外壳)
  - 简化树里是否出现歌曲列表特征(n-srchrst / srchsongst / data-res-id)
  - 简化树里是否还残留 ArtTemplate 模板代码(textarea 已加入 SKIP)
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

SESSION = pathlib.Path("data/session.json")
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
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()
        report, _stats = await crawler.analyze_only(URL, scroll_rounds=0)
        if report is None:
            check(False, "分析返回报告", "None")
            return 1

        tree = report.simplified_tree or ""
        lines = [x for x in tree.splitlines() if x.strip()]
        stats = report.dom_stats or {}

        print("=" * 72)
        print("  网易云音乐搜索页 · 结构分析")
        print("=" * 72)
        print(f"    标题          = {report.title!r}")
        print(f"    content_frame = {report.content_frame!r}")
        print(f"    候选列表      = {len(report.candidate_lists)} 个")
        print(f"    DOM           = 元素 {stats.get('total_elements')} / "
              f"图 {stats.get('images')} / 链接 {stats.get('links')}")
        print(f"    树            = {len(lines)} 行 / {len(tree)} 字符")

        check(report.content_frame == "contentFrame",
              "**定位到内容 frame**", repr(report.content_frame))
        check(stats.get("total_elements", 0) > 600,
              "DOM 元素数来自内层 frame(应 >600, 外壳仅 336)",
              str(stats.get("total_elements")))
        check(len(report.candidate_lists) > 0,
              "识别到候选列表", f"{len(report.candidate_lists)} 个")

        print("\n  === 树里的歌曲列表特征 ===")
        for kw, desc in (("n-srchrst", "搜索结果容器"), ("srchsongst", "歌曲列表"),
                         ("data-res-id", "歌曲资源 id"), ("m-topbar", "顶部导航"),
                         ("g-btmbar", "底部播放条")):
            n = tree.count(kw)
            print(f"    {desc:<14} {kw!r:<14} {n if n else '0'}")

        print("\n  === ArtTemplate 模板残留(textarea 应已被跳过) ===")
        tmpl = len(re.findall(r"\{if |\{list |\$\{", tree))
        print(f"    模板语法出现 = {tmpl} 处")
        check(tmpl == 0, "**树中没有 ArtTemplate 模板代码**", f"{tmpl} 处")

        print("\n  === 候选列表(前 5 个) ===")
        for i, c in enumerate(report.candidate_lists[:5]):
            names = [f.get("name") for f in c.sample_fields]
            print(f"    [{i}] count={c.count:<4} {c.item_selector[:56]!r}")
            print(f"        字段 = {names}")

        print("\n  === 树里的歌曲相关行(样本) ===")
        shown = 0
        for l in lines:
            if "srchsongst" in l or "data-res-id" in l or "/song" in l:
                print(f"    {l.strip()[:92]}")
                shown += 1
                if shown >= 8:
                    break
        if shown == 0:
            print("    (无)")
    finally:
        await crawler.close()

    print("\n" + "=" * 72)
    if failures:
        print(f"iframe 下探验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("iframe 下探验收: 通过 ✓")
    print("=" * 72)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
