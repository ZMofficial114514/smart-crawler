"""
验证结构树修复: 正文(图片/作品)到底有没有进树。

判据取自用户的原话 —— "真实浏览器正文部分和爬取的 dom 树明显不同"。
所以这里不测"树有多长", 而测**封面/作品内容有没有进来**:
  - DOM 里有多少 <img> / /artworks/ 链接, 树里有多少 -> 覆盖率
  - 树里是否出现仅正文才有的特征(home_recommend / 作品链接 / 图片行)
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
TARGET = "https://www.pixiv.net/"
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def main() -> int:
    if not SESSION.exists():
        print("  没有会话, 无法进入登录态")
        return 1
    state = json.loads(SESSION.read_text(encoding="utf-8"))

    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=state, user_agent=REAL_UA,
                                       viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        for _ in range(10):
            before = page.url
            await page.wait_for_timeout(1000)
            if page.url == before:
                break
        await page.wait_for_timeout(4000)
        for _ in range(3):
            await page.mouse.wheel(0, 1200)
            await page.wait_for_timeout(900)
        await page.mouse.wheel(0, -6000)
        await page.wait_for_timeout(1500)

        # DOM 侧的事实
        dom = await page.evaluate(
            """() => ({
                imgs: document.querySelectorAll('img').length,
                art: document.querySelectorAll('a[href*="/artworks/"]').length,
                users: document.querySelectorAll('a[href*="/users/"]').length,
                elements: document.querySelectorAll('*').length,
            })"""
        )

        report = await StructureAnalyzer(get_settings()).analyze(page)
        tree = report.simplified_tree or ""
        shot = pathlib.Path("data/screenshots/pixiv_tree_fixed.png")
        await page.screenshot(path=str(shot), full_page=False)
        await browser.close()

    def count(pat: str) -> int:
        return len(re.findall(pat, tree, re.M))

    # 数"树里有多少张图片", 而不是"有多少行以 img 开头"。
    #
    # 两者不等价, 而且后者会低估: 简化树只在**没有子元素**的节点上附文本, 而 <img> 通常
    # 没有子元素, 所以绝大多数就是 `img` 一行 —— 但只要有极少数 img 带了子节点(或行首
    # 缩进/前后缀差异), 正则就会漏数。更稳的做法是匹配 **img 元素本身**(行内任意位置),
    # 因为它不会出现在别的标签名里。
    imgs_tree = count(r"(?<![\w-])img(?![\w-])")
    art_tree = len(re.findall(r"/artworks/", tree))
    users_tree = len(re.findall(r"/users/", tree))
    lines = len([x for x in tree.splitlines() if x.strip()])

    def pct(a: int, b: int) -> str:
        return f"{(a / b * 100):.0f}%" if b else "n/a"

    print("=" * 70)
    print("  结构树正文覆盖(修复后)")
    print("=" * 70)
    print(f"  {'项目':<18} {'DOM':>8} {'树内':>8} {'覆盖':>8}")
    print("  " + "-" * 46)
    print(f"  {'<img>':<18} {dom['imgs']:>8} {imgs_tree:>8} {pct(imgs_tree, dom['imgs']):>8}")
    print(f"  {'/artworks/ 链接':<18} {dom['art']:>8} {art_tree:>8} {pct(art_tree, dom['art']):>8}")
    print(f"  {'/users/ 链接':<18} {dom['users']:>8} {users_tree:>8} {pct(users_tree, dom['users']):>8}")
    print(f"\n  树规模   = {lines} 行 / {len(tree)} 字符")
    print(f"  DOM 元素 = {dom['elements']}")
    print(f"  标记截断 = {report.simplified_tree_truncated}")
    print(f"  候选列表 = {len(report.candidate_lists)}")

    print("\n  === 树里出现的内容区特征 ===")
    for kw, label in (("home_recommend", "首页推荐区"), ("figcaption", "作品标题"),
                      ("-profile/", "用户头像图"), ("/artworks/", "作品链接"),
                      ("ランキング", "排行榜"), ("イラスト", "插画")):
        n = tree.count(kw)
        print(f"    {label:<12} {kw:<16} {n:>4} 次  {'✓' if n else ''}")

    print("\n  === 树的前 12 行(看内容是否提前了) ===")
    for l in [x for x in tree.splitlines() if x.strip()][:12]:
        print(f"    {l[:88]}")

    print("\n  === 树里含 img 的行(样本, 最多 8 条) ===")
    shown = 0
    for l in tree.splitlines():
        if re.match(r"^\s*img\b", l):
            print(f"    {l.strip()[:88]}")
            shown += 1
            if shown >= 8:
                break
    if shown == 0:
        print("    (无)")

    print(f"\n  截图 -> {shot}")
    print("=" * 70)
    # 阈值说明: 目标是"正文确实进来了", 不是"一个不漏" —— 树必然要按节点上限裁剪。
    # 图片 ≥65% 且 作品链接 ≥80% 才说明正文真的进了树; 低于这个数就是又被切掉了。
    img_ok = dom["imgs"] == 0 or (imgs_tree / dom["imgs"]) >= 0.65
    art_ok = dom["art"] == 0 or (art_tree / dom["art"]) >= 0.80
    ok = img_ok and art_ok
    verdict = "✓ 正文已进入结构树" if ok else "✗ 正文仍然缺失"
    print(f"  {verdict}  (图片 {pct(imgs_tree, dom['imgs'])} / 作品 {pct(art_tree, dom['art'])})")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
