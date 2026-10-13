"""
验收: text_with_href 必须**跳过 CSS 隐藏的子元素**。

背景(实测踩到的): 网易云歌曲行的 ``<a class="txt">`` 里套了一个 ``display:none`` 的 span,
内容形如 ``/song?id=34723470``。用 ``textContent`` 取值会把隐藏文本也算进去, 于是

    "东京不太热\n/song?id=34723470\nhttps://music.163.com/song?id=34723470"

变成三行 —— 中间那行是只给搜索引擎看的残迹, 用户拿到这种数据会莫名其妙。
改用 ``innerText`` 后只剩两行。

这里用**离线构造的页面**做判据(不依赖目标站点), 同时覆盖:
  - 隐藏 span 被跳过;
  - 可见的子元素文本仍然保留(不能把该留的也丢了);
  - ``innerText`` 取不到时退化为 ``textContent``, 不能变成空值。
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


#: 模拟网易云的结构: 可见锚文本 + display:none 的路径残迹
NETEASE_LIKE = """
<ul>
  <li class="it">
    <a class="txt" href="https://music.163.com/song?id=34723470">
      东京不太热
      <span class="s-fc7" style="display:none">/song?id=34723470</span>
    </a>
  </li>
  <li class="it">
    <a class="txt" href="https://music.163.com/song?id=2">
      幹物女(WeiWei)
      <span class="s-fc7" style="display:none">/song?id=2</span>
    </a>
  </li>
</ul>
"""

#: 隐藏方式换一种(HTML hidden 属性)也要跳过
HIDDEN_ATTR = """
<ul><li class="it"><a class="txt" href="/a/1">标题<span hidden>残迹</span></a></li></ul>
"""

#: 可见子元素必须保留 —— 不能为了躲隐藏文本把正常结构也丢了
VISIBLE_CHILD = """
<ul><li class="it"><a class="txt" href="/a/2">主标题<span>副标题</span></a></li></ul>
"""

#: 没有 href
NO_HREF = """
<ul><li class="it"><span class="txt">纯文本项</span></li></ul>
"""


async def main() -> int:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from smartcrawler.extractor import _EXTRACT_DOM_JS  # noqa: PLC0415
    from smartcrawler.models import ExtractionRule  # noqa: PLC0415

    rule = ExtractionRule.model_validate({
        "list_rule": {
            "item_selector": "ul > li.it",
            "fields": [
                {"name": "t", "selector": ".txt", "attribute": "text_with_href",
                 "transform": ["strip"]},
            ],
        }
    })

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        async def rows_of(html: str) -> list[str]:
            await page.set_content(f"<!doctype html><html><body>{html}</body></html>")
            out = await page.evaluate(_EXTRACT_DOM_JS, rule.model_dump(mode="json"))
            return [str(r.get("t") or "") for r in out]

        print("=== 1) 隐藏 span 必须被跳过(网易云形态)===")
        got = await rows_of(NETEASE_LIKE)
        for g in got:
            print(f"      {g!r}")
        check(len(got) == 2 and all(g.count("\n") == 1 for g in got),
              "**每行只有两行(文本 + 链接), 隐藏残迹被跳过**",
              f"{[g.count(chr(10)) for g in got]}")
        check(all("/song?id=" not in g.split("\n")[0] for g in got),
              "第一行只含可见锚文本")
        check(bool(got) and got[0].split("\n")[0] == "东京不太热",
              "第一行是干净的锚文本", repr(got[0].split("\n")[0] if got else None))
        check(bool(got) and got[0].split("\n")[1] == "https://music.163.com/song?id=34723470",
              "第二行是完整链接", repr(got[0].split("\n")[1] if got else None))

        print("\n=== 2) 另一种隐藏方式(hidden 属性)===")
        got = await rows_of(HIDDEN_ATTR)
        print(f"      {got!r}")
        check(bool(got) and "\n" in got[0] and got[0].split("\n")[0] == "标题",
              "**hidden 属性的内容也被跳过**", repr(got[0] if got else None))

        print("\n=== 3) 可见子元素必须保留 ===")
        got = await rows_of(VISIBLE_CHILD)
        print(f"      {got!r}")
        check(bool(got) and "副标题" in got[0],
              "**可见子元素的文本不能丢**", repr(got[0] if got else None))

        print("\n=== 4) 没有 href 时退化为纯文本 ===")
        got = await rows_of(NO_HREF)
        print(f"      {got!r}")
        check(got == ["纯文本项"], "**无 href -> 纯文本, 无多余空行**", repr(got))

        await browser.close()

    print("\n" + "=" * 68)
    if failures:
        print(f"text_with_href 隐藏元素验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("text_with_href 隐藏元素验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
