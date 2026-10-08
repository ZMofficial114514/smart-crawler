"""验收: text_with_href 取值形态 + link 不被污染 + 下载器仍能解析。"""

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

KUWO = "https://kuwo.cn/search/list?key=%E7%90%B5%E7%90%B6%E6%9B%B2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.extractor import _EXTRACT_DOM_JS  # noqa: PLC0415
    from smartcrawler.models import ExtractionRule  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="zh-CN")
        page = await ctx.new_page()
        await page.goto(KUWO, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(7000)

        rule = ExtractionRule.model_validate({
            "list_rule": {
                "item_selector": "ul.search_list > li.song_item.flex_c",
                "fields": [
                    {"name": "title", "selector": "a.name", "attribute": None,
                     "transform": ["strip"]},
                    {"name": "link", "selector": "a.name", "attribute": "href",
                     "transform": ["strip"]},
                    {"name": "link_text", "selector": "a.name",
                     "attribute": "text_with_href", "transform": ["strip"]},
                ],
            }
        })
        rows = await page.evaluate(_EXTRACT_DOM_JS, rule.model_dump(mode="json"))
        print(f"  行数 = {len(rows)}")
        for r in rows[:4]:
            print(f"      title     = {r.get('title')!r}")
            print(f"      link      = {r.get('link')!r}")
            print(f"      link_text = {r.get('link_text')!r}")

        # 1) link 保持纯 URL(不能被污染, 否则下载器解析不了)
        links = [str(r.get("link") or "") for r in rows]
        check(all("\n" not in x for x in links),
              "**link 仍是纯 URL, 没有被换行污染**",
              f"例={links[0]!r}" if links else "")
        check(all("/play_detail/" in x for x in links if x),
              "link 是可直接用的路径")

        # 2) link_text 是"文本 + 换行 + 链接"两行
        lts = [str(r.get("link_text") or "") for r in rows]
        check(all("\n" in x for x in lts if x),
              "**link_text 是两行(文本 + 链接)**",
              f"例={lts[0]!r}" if lts else "")
        two_line = [x for x in lts if x]
        if two_line:
            first = two_line[0]
            text_part, _, url_part = first.partition("\n")
            check(bool(text_part.strip()), "第一行是锚文本", repr(text_part))
            check(url_part.strip().startswith("/") or url_part.strip().startswith("http"),
                  "第二行是链接", repr(url_part))
            # 文本部分应与 title 一致, 链接部分应与 link 一致
            check(text_part.strip() == str(rows[0].get("title") or "").strip(),
                  "第一行与 title 字段一致")
            check(url_part.strip() == str(rows[0].get("link") or "").strip(),
                  "第二行与 link 字段一致")

        # 3) 没有 href 时退化为纯文本, 不留空行
        page2 = await ctx.new_page()
        await page2.set_content(
            "<ul><li class='i'><span class='t'>纯文本项</span></li>"
            "<li class='i'><a class='t' href='/x/1'>有链接项</a></li></ul>")
        rule2 = ExtractionRule.model_validate({
            "list_rule": {
                "item_selector": "ul > li.i",
                "fields": [
                    {"name": "t", "selector": ".t", "attribute": "text_with_href",
                     "transform": ["strip"]},
                ],
            }
        })
        rows2 = await page2.evaluate(_EXTRACT_DOM_JS, rule2.model_dump(mode="json"))
        print(f"\n  退化用例行数 = {len(rows2)}")
        for r in rows2:
            print(f"      t = {r.get('t')!r}")
        check(rows2 and rows2[0].get("t") == "纯文本项",
              "**没有 href 时退化为纯文本(无多余空行)**",
              repr(rows2[0].get("t")) if rows2 else "")
        check(rows2 and rows2[1].get("t") == "有链接项\n/x/1",
              "有 href 时是两行", repr(rows2[1].get("t")) if len(rows2) > 1 else "")

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"text_with_href 验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("text_with_href 验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
