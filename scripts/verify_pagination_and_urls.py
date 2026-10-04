"""
验证两个新修复:
  1) 简化树里的图片 URL 不再被硬切断(能在路径分隔符处省略, 保留文件名)
  2) pixiv 搜索页能识别出"下一页"(数字页码 + aria-current 策略)

第 2 条同时验证**规则里带上了分页** —— 识别到但没传下去等于没修。
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

SESSION = pathlib.Path("data/session.json")
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
SEARCH = ("https://www.pixiv.net/search?q=%E5%88%9D%E9%9F%B3%E3%83%9F%E3%82%AF"
          "&s_mode=tag&type=artwork&r=1")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    if not SESSION.exists():
        print("  没有会话")
        return 2
    state = json.loads(SESSION.read_text(encoding="utf-8"))

    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=state, user_agent=REAL_UA,
                                        viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        for attempt in range(4):
            try:
                await page.goto(SEARCH, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception:  # noqa: BLE001
                await page.wait_for_timeout(3000)
        await page.wait_for_timeout(4000)
        for _ in range(5):
            await page.mouse.wheel(0, 1400)
            await page.wait_for_timeout(900)

        report = await StructureAnalyzer(get_settings()).analyze(page)

        # DOM 里的真实图片 URL(用于核对树里的 URL 有没有被切断)
        dom_urls = await page.evaluate(
            """() => [...document.querySelectorAll('img')]
                .map(i => i.getAttribute('src') || '')
                .filter(u => u.includes('pximg.net'))"""
        )
        await browser.close()

    tree = report.simplified_tree or ""

    # ================= 1) 分页检测 =================
    print("=== 1) 下一页检测 ===")
    pag = report.pagination
    if pag is None:
        check(False, "识别到分页", "pagination 仍为 None")
    else:
        print(f"    next_selector = {pag.next_selector!r}")
        print(f"    next_text     = {pag.next_text!r}")
        print(f"    next_href     = {str(pag.next_href)[-60:]!r}")
        strategy = (pag.model_extra or {}).get("strategy") if hasattr(pag, "model_extra") else None
        check(True, "识别到分页", f"next_text={pag.next_text!r}")
        check(bool(pag.next_selector), "next_selector 非空", str(pag.next_selector)[:50])
        # 页码应该是 2(第 1 页的下一页)
        check("p=2" in str(pag.next_href) or pag.next_text == "2",
              "**指向第 2 页**", f"href={str(pag.next_href)[-30:]!r}")

    rule = StructureAnalyzer.build_rule_from_structure(report)
    if rule is None:
        check(False, "生成了规则")
    else:
        check(rule.pagination is not None,
              "**规则里带上了分页(否则识别到也没用)**",
              str(getattr(rule.pagination, "next_selector", None))[:50])

    # ================= 2) 图片 URL 完整性 =================
    print("\n=== 2) 树里的图片 URL ===")
    tree_urls = re.findall(r"https?://[^\s'\"]*pximg\.net[^\s'\"]*", tree)
    print(f"    DOM 中 pximg URL = {len(dom_urls)}  树中 = {len(tree_urls)}")
    if tree_urls:
        for u in tree_urls[:3]:
            print(f"      {u}")
    # 被切断的判据: URL 以 "/" 或明显的半截路径结尾, 或者以 "_p"/"_" 之类截断
    truncated = [u for u in tree_urls
                 if u.endswith("/") or re.search(r"_\d*$", u) or re.search(r"/\d+$", u)]
    # 允许略号形式(head/…/tail)
    ellipsized = [u for u in tree_urls if "/…/" in u]
    hard_cut = [u for u in truncated if "/…/" not in u]
    print(f"    以路径分隔符处省略(可接受) = {len(ellipsized)}")
    print(f"    硬切(不好)                 = {len(hard_cut)}")
    for u in hard_cut[:4]:
        print(f"      ✗ {u}")
    check(not hard_cut,
          "**没有硬切一半的图片 URL**",
          f"{len(hard_cut)} 个" if hard_cut else "全部完整或在 '/' 处省略")

    # 抽查: 树里应能看出"缩略图 vs 原图"的区别
    kinds = set()
    for u in tree_urls:
        if "custom-thumb" in u:
            kinds.add("缩略图")
        if "img-master" in u or "master1200" in u:
            kinds.add("原图")
    print(f"    可区分的图片种类 = {sorted(kinds)}")

    print("\n" + "=" * 68)
    if failures:
        print(f"验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("验收: 通过 ✓  (分页可识别且进入规则; 图片 URL 不再硬切)")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
