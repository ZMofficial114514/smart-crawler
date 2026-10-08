"""验收: kivo.wiki 的 info_value 列能否抓到(用 :is() 覆盖 div/a 两种标签)。"""

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

URL = "https://kivo.wiki/data/character/114?mode=info"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EXPECT = ["★★★", "圣三一综合学园", "正义实现委员会", "163 cm", "11月11日",
          "鹫见友美Jiena", "沐霏", "大号杂鱼酱"]

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.extractor import _EXTRACT_DOM_JS  # noqa: PLC0415
    from smartcrawler.models import ExtractionRule  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN",
                                        viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(9000)

        rep = await StructureAnalyzer(get_settings()).analyze(page)
        cand = next((c for c in rep.candidate_lists
                     if "student_info" in c.item_selector), None)
        if cand is None:
            check(False, "找到 student_info 候选")
            await browser.close()
            return 1
        print(f"  item_selector = {cand.item_selector!r}  count={cand.count}")
        fields = cand.sample_fields or []
        print("  字段:")
        for f in fields:
            print(f"      {str(f.get('name')):<14} {f.get('selector')!r}")

        # :is( 只应在**标签确实不同**时出现
        mixed = [f for f in fields if ":is(" in str(f.get("selector") or "")]
        if mixed:
            check(True, "生成了 :is() 形式的选择器",
                  "; ".join(f"{f.get('name')} -> {f.get('selector')}" for f in mixed))

        rule = ExtractionRule.model_validate({
            "list_rule": {
                "item_selector": cand.item_selector,
                "fields": [{"name": f.get("name"), "selector": f.get("selector"),
                            "attribute": f.get("attribute"), "transform": ["strip"]}
                           for f in fields],
            }
        })
        rows = await page.evaluate(_EXTRACT_DOM_JS, rule.model_dump(mode="json"))
        print(f"\n  行数 = {len(rows)}")
        for r in rows:
            print(f"      {r}")

        # 期望: 存在某个字段能覆盖全部 8 行, 且取到的值就是那 8 个期望值
        best_name, best_vals = None, []
        for f in fields:
            name = f.get("name")
            vals = [str(r.get(name) or "").strip() for r in rows]
            if sum(1 for v in vals if v) > sum(1 for v in best_vals if v):
                best_name, best_vals = name, vals
        print(f"\n  覆盖最好的字段 = {best_name!r}  非空 "
              f"{sum(1 for v in best_vals if v)}/{len(rows)}")
        print(f"      值 = {[v for v in best_vals if v]}")

        check(best_name is not None and "info_value" in str(best_name).lower(),
              "**存在名为 info_value 的字段**(而不是 n-a 这种样式类名)",
              f"实际最好的是 {best_name!r}")
        got = [v for v in best_vals if v]
        check(len(got) == len(EXPECT),
              f"**取到全部 {len(EXPECT)} 个值**", f"实际 {len(got)} 个")
        missing = [e for e in EXPECT if e not in got]
        check(not missing, "**8 个期望值全部命中**", f"缺: {missing}")

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"kivo.wiki info_value 验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("kivo.wiki info_value 验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
