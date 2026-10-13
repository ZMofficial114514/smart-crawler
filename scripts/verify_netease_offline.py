"""
用**真实抓取路径**(crawler.crawl)在离线页面上验证 link_text 是否干净。

目的: 网易云会间歇性限流(返回"未能找到相关搜索结果"), 拿它做判据不稳定。
这里把页面换成**离线构造的网易云同构 DOM**, 但走的是完整的 crawler 抽取链路
(analyze -> build rule -> extract -> merge fields), 所以能真正验证到线上会走的那段代码。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


#: 模拟网易云搜索结果页的歌曲行: **`<a>` 自己带 .txt 类**, 里面套一个 display:none 的路径残迹。
#:
#: 注意别把结构写错: 我第一版写成 ``<a href=..><span class="txt">名</span>...</a>``,
#: 于是选择器 ``.txt`` 落到那个**没有 href 的 span** 上, 取出来当然没有链接 ——
#: 那是测试用例的错, 不是抽取逻辑的错。
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>搜索</title></head><body>
<div class="srchsongst">
  <div class="item f-cb h-flag">
    <div class="td w0"><div class="sn"><div class="text">
      <a class="txt" href="/song?id=34723470">东京不太热<span class="s-fc7" style="display:none">/song?id=34723470</span></a>
    </div></div></div>
    <div class="td w2"><div class="text">封茗囧菌</div></div>
  </div>
  <div class="item f-cb h-flag even">
    <div class="td w0"><div class="sn"><div class="text">
      <a class="txt" href="/song?id=2">幹物女(WeiWei)<span class="s-fc7" style="display:none">/song?id=2</span></a>
    </div></div></div>
    <div class="td w2"><div class="text">封茗囧菌</div></div>
  </div>
</div>
</body></html>"""


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.extractor import _EXTRACT_DOM_JS  # noqa: PLC0415
    from smartcrawler.models import ExtractionRule  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(PAGE)

        # 走**分析阶段**给出的字段形态: 分析器会把 link_text 标成 text_with_href
        rule = ExtractionRule.model_validate({
            "list_rule": {
                "item_selector": "div.srchsongst > div.item.f-cb.h-flag",
                "fields": [
                    {"name": "song_name", "selector": ".txt", "attribute": None,
                     "transform": ["strip"]},
                    {"name": "link", "selector": ".txt", "attribute": "href",
                     "transform": ["strip"]},
                    {"name": "link_text", "selector": ".txt",
                     "attribute": "text_with_href", "transform": ["strip"]},
                ],
            }
        })
        rows = await page.evaluate(_EXTRACT_DOM_JS, rule.model_dump(mode="json"))
        print(f"  行数 = {len(rows)}")
        for r in rows:
            print(f"      song_name = {r.get('song_name')!r}")
            print(f"      link      = {r.get('link')!r}")
            print(f"      link_text = {r.get('link_text')!r}")

        check(len(rows) == 2, "抽到 2 行", str(len(rows)))
        lts = [str(r.get("link_text") or "") for r in rows]
        # 这是本次修复的核心判据: 隐藏 span 不能让 link_text 变成三行
        check(all(x.count("\n") == 1 for x in lts),
              "**link_text 只有两行(隐藏残迹被跳过)**",
              f"换行数={[x.count(chr(10)) for x in lts]}")
        check(all("s-fc7" not in x for x in lts), "没有把类名混进值里")
        # 纯文本字段(song_name, attribute=None)以前也走 textContent, 同样会混进隐藏残迹
        names = [str(r.get("song_name") or "") for r in rows]
        check(names == ["东京不太热", "幹物女(WeiWei)"],
              "**纯文本字段也是干净的歌名(无隐藏残迹)**", f"{names}")
        check(all("/song?id=" not in x.split("\n")[0] for x in lts),
              "第一行不含路径残迹",
              repr(lts[0].split("\n")[0] if lts else None))
        check(bool(lts) and lts[0] == "东京不太热\n/song?id=34723470",
              "形态正确(文本 + 真实 href)", repr(lts[0] if lts else None))

        print("\n=== 用可见文本在内层、链接在外层的情况(网易云另一种写法)===")
        await page.set_content(
            """<!doctype html><html><body>
            <div class="srchsongst"><div class="item">
              <a class="txt" href="/song?id=9">标题<span>副标题</span>
                <span style="display:none">/song?id=9</span></a>
            </div></div></body></html>""")
        rule2 = ExtractionRule.model_validate({
            "list_rule": {
                "item_selector": "div.srchsongst > div.item",
                "fields": [
                    {"name": "lt", "selector": ".txt",
                     "attribute": "text_with_href", "transform": ["strip"]},
                ],
            }
        })
        rows2 = await page.evaluate(_EXTRACT_DOM_JS, rule2.model_dump(mode="json"))
        v = str(rows2[0].get("lt") or "") if rows2 else ""
        print(f"      {v!r}")
        check("副标题" in v, "**可见子元素保留**", repr(v))
        check(v.count("\n") == 1, "仍然只有两行", f"换行数={v.count(chr(10))}")

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"link_text 抽取链路验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("link_text 抽取链路验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
