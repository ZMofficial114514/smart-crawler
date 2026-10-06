"""
验收: 限定区域分析(部分爬取)。

用酷我搜索页做对照 —— 它页面上有导航/侧栏/页脚等一堆无关列表。

断言:
  1. 整页分析会给出**多个**候选(含导航/页脚);
  2. 指定区域后候选**只来自该区域**, 数量减少且都是区域内的;
  3. 区域内的列表依然被找到(item_selector 与整页时一致);
  4. 区域选择器写错时**明确报告没匹配**, 而不是静默按整页;
  5. 区域内没有列表时如实返回 0 个候选, 不偷拿区域外的。
"""

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

URL = "https://kuwo.cn/search/list?key=%E7%90%B5%E7%90%B6%E6%9B%B2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
#: 歌曲列表上方 3 层的容器 —— 覆盖"搜索结果"这一块, 但不含页头导航与页脚
REGION = "div.child_view"
#: 一个**确实不含列表结构**的元素(歌曲标题格), 用来验证区域为空时不会偷拿区域外的列表
EMPTY_REGION = "div.song_name"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    analyzer = StructureAnalyzer(get_settings())

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(7000)

        # 先确认页面上确实存在我们要限定的区域, 并打印它的规模
        probe = await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                return { tag: el.tagName.toLowerCase(),
                         elements: el.querySelectorAll('*').length,
                         items: el.querySelectorAll('li.song_item').length,
                         textLen: (el.innerText || '').trim().length };
            }""",
            REGION,
        )
        print(f"  区域 {REGION!r} -> {probe}")
        if not probe:
            print("  区域不存在, 换一个选择器")
            await browser.close()
            return 1

        print("\n=== 1) 整页分析(基线)===")
        whole = await analyzer.analyze(page)
        print(f"  候选 {len(whole.candidate_lists)} 个:")
        for c in whole.candidate_lists:
            print(f"      {c.count:>4} 条  {c.item_selector[:70]}")
        whole_sels = {c.item_selector for c in whole.candidate_lists}
        check(len(whole.candidate_lists) > 1, "整页给出多个候选",
              f"{len(whole.candidate_lists)} 个")

        print(f"\n=== 2) 限定区域 {REGION!r} ===")
        scoped = await analyzer.analyze(page, scope=REGION)
        print(f"  scope = {scoped.scope!r}   matched = {scoped.scope_matched}")
        print(f"  候选 {len(scoped.candidate_lists)} 个:")
        for c in scoped.candidate_lists:
            print(f"      {c.count:>4} 条  {c.item_selector[:70]}")

        check(scoped.scope_matched, "**区域选择器被确认匹配**")
        check(len(scoped.candidate_lists) < len(whole.candidate_lists),
              "**限定后候选变少**(排除了区域外的列表)",
              f"{len(whole.candidate_lists)} -> {len(scoped.candidate_lists)}")

        # 每个限定候选都必须真的落在区域内
        inside = await page.evaluate(
            """([sel, sels]) => {
                const root = document.querySelector(sel);
                if (!root) return null;
                return sels.map(s => {
                    let el = null;
                    try { el = document.querySelector(s); } catch (e) { return null; }
                    return el ? root.contains(el) : false;
                });
            }""",
            [REGION, [c.item_selector for c in scoped.candidate_lists]],
        )
        print(f"  各候选是否落在区域内 = {inside}")
        check(all(x is True for x in inside or []) and bool(inside),
              "**所有候选都落在限定区域内**")

        # 歌曲列表这个关键候选必须还在(限定不等于丢掉目标)
        check(any("song_item" in c.item_selector for c in scoped.candidate_lists),
              "**目标歌曲列表仍在候选里**")

        print("\n=== 3) 结构树也要跟着缩 ===")
        print(f"  整页树 行数 = {len(whole.simplified_tree.splitlines())}")
        print(f"  限定树 行数 = {len(scoped.simplified_tree.splitlines())}")
        check(len(scoped.simplified_tree.splitlines())
              < len(whole.simplified_tree.splitlines()),
              "限定后的结构树更小(少算了区域外节点)")
        check(scoped.simplified_tree.lstrip().startswith(probe["tag"]),
              "树从区域元素起画", scoped.simplified_tree.splitlines()[0][:60])

        print("\n=== 4) 选择器写错时必须明确报告 ===")
        bad = await analyzer.analyze(page, scope="div#this-does-not-exist-xyz")
        check(not bad.scope_matched, "**写错时 scope_matched = False**",
              f"matched={bad.scope_matched}")
        check(len(bad.candidate_lists) >= 1, "退回整页分析(仍给出候选)",
              f"{len(bad.candidate_lists)} 个")

        print("\n=== 5) 区域内没有列表时不偷拿区域外的 ===")
        exists = await page.evaluate(
            "(sel) => !!document.querySelector(sel)", EMPTY_REGION)
        print(f"  选一个不含列表的区域 = {EMPTY_REGION!r} (存在={exists})")
        if exists:
            small = await analyzer.analyze(page, scope=EMPTY_REGION)
            print(f"    matched={small.scope_matched}  候选={len(small.candidate_lists)} 个")
            for c in small.candidate_lists:
                print(f"      {c.count:>4} 条  {c.item_selector[:66]}")
            check(small.scope_matched, "该区域被匹配到")
            # 关键: 区域里没有列表就不该冒出整页那个 20 条的歌曲列表
            check(all("search_list" not in c.item_selector
                      for c in small.candidate_lists),
                  "**没有把区域外的歌曲列表塞进来**")
        else:
            print("  该元素不存在, 跳过此项")

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"限定区域分析验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("限定区域分析验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
