"""
字段选择器生成的行为快照 —— **改 relSelector 之前必须先有它**。

为什么需要这个文件: `relSelector` 承担很多职责(唯一性、层级、兄弟关系、多标签), 而且
网易云/酷我/洛谷这些站点的列能不能取到值**全都依赖它的现有行为**。我上一轮直接改它, 结果
网易云的 `link` 从 30/30 掉到 0/30 —— 因为"只用 class"的形式对 title 链接退化成 `.text`,
太泛, 匹配到了行内别的元素。

所以这里把几个**已知真实场景**的期望行为固定下来: 改完跑一遍, 退化立刻暴露。
断言分两类:
  A. 语义断言(与站点无关, 必须永远成立)—— 选择器合法、在 scope 内唯一、能取到目标文本;
  B. 快照断言(记录当前形状)—— 防止无意改变, 有意改变时同步更新并说明原因。
"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

NETEASE = "https://music.163.com/#/search/m/?s=%E4%B8%9C%E4%BA%AC%E4%B8%8D%E5%A4%AA%E7%83%AD&type=1"
KUWO = "https://kuwo.cn/search/list?key=%E7%90%B5%E7%90%B6%E6%9B%B2"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def audit(page, frame, item_selector: str, label: str,
                expect_nonempty: dict[str, float]) -> dict[str, int]:
    """对某个列表跑一遍: 选择器合法性 + 各字段非空率。"""
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.extractor import _EXTRACT_DOM_JS  # noqa: PLC0415
    from smartcrawler.models import ExtractionRule  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    rep = await StructureAnalyzer(get_settings()).analyze(page)
    cand = next((c for c in rep.candidate_lists if c.item_selector == item_selector), None)
    if cand is None:
        cand = next((c for c in rep.candidate_lists
                     if "srchsongst" in c.item_selector or "search_list" in c.item_selector
                     or "student_info" in c.item_selector), None)
    if cand is None:
        check(False, f"{label}: 找到目标候选")
        return {}
    print(f"\n  --- {label} ---")
    print(f"      item_selector = {cand.item_selector!r}  count={cand.count}")
    fields = cand.sample_fields or []
    for f in fields:
        print(f"          {str(f.get('name')):<14} {f.get('selector')!r}")

    # A1. 每个选择器必须合法
    for f in fields:
        try:
            n = await frame.evaluate(
                "([s, sel]) => { const root = document.querySelector(s);"
                " if (!root) return -1;"
                " try { return root.querySelectorAll(sel).length } catch(e) { return -2 } }",
                [cand.item_selector, f.get("selector")])
        except Exception:  # noqa: BLE001
            n = -3
        check(n >= 1, f"{label}: 选择器合法且能命中 {f.get('name')!r}",
              f"行内命中 {n}")

    rule = ExtractionRule.model_validate({
        "list_rule": {
            "item_selector": cand.item_selector,
            "fields": [{"name": f.get("name"), "selector": f.get("selector"),
                        "attribute": f.get("attribute"), "transform": ["strip"]}
                       for f in fields],
        }
    })
    rows = await frame.evaluate(_EXTRACT_DOM_JS, rule.model_dump(mode="json"))
    print(f"      行数 = {len(rows)}")
    rates: dict[str, int] = {}
    for f in fields:
        name = f.get("name")
        vals = [str(r.get(name) or "").strip() for r in rows]
        n = sum(1 for v in vals if v)
        rates[name] = n
        need = expect_nonempty.get(name)
        mark = "" if need is None else ("  <= 关注" if n < need * len(rows) else "")
        print(f"          {name:<14} 非空 {n}/{len(rows)}{mark}")
    for name, ratio in expect_nonempty.items():
        if name in rates:
            check(rates[name] >= ratio * len(rows),
                  f"{label}: {name} 非空率 >= {int(ratio*100)}%",
                  f"{rates[name]}/{len(rows)}")
    return rates


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    state = json.loads(pathlib.Path("data/session.json").read_text(encoding="utf-8"))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # ---- 网易云: 专辑列曾经只有 11/30, 已修为 30/30 ----
        ctx = await browser.new_context(storage_state=state, user_agent=UA, locale="zh-CN")
        page = await ctx.new_page()
        await page.goto(NETEASE, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(9000)
        inner = next((f for f in page.frames
                      if (getattr(f, "name", "") or "") == "contentFrame"), None)
        if inner is None:
            check(False, "网易云: 找到 contentFrame")
        else:
            await audit(page, inner, "div.srchsongst > div.item.f-cb.h-flag", "网易云搜索结果",
                        {"title": 1.0, "link": 1.0, "artist": 0.9, "album": 1.0})

        # ---- 酷我: 歌手/专辑列在无 class 的 span 里 ----
        page2 = await ctx.new_page()
        await page2.goto(KUWO, wait_until="domcontentloaded", timeout=45000)
        await page2.wait_for_timeout(7000)
        await audit(page2, page2, "ul.search_list > li.song_item.flex_c", "酷我搜索结果",
                    {"title": 1.0, "link": 1.0, "artist": 1.0, "album": 1.0})

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"选择器生成回归: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("选择器生成回归: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
