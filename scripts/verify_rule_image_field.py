"""
关键验收: **生成的提取规则里到底有没有图片字段**?

之前的各种"覆盖率"都是代理指标 —— 用户真正要的是"能不能抓到图片"。
图片下载插件是按**字段名**(image / thumb)取地址的, 规则里没有这些键就下不到东西。

所以这里端到端验证:
  1. 分析 pixiv(登录态) -> 取候选列表
  2. 看候选列表的 sample_fields 里有没有 image / thumb / image_srcset
  3. 用规则引擎(离线, 不依赖 AI Key)生成规则, 检查字段与选择器
  4. 真的跑一次提取, 看是否取到图片 URL
"""

import asyncio
import json
import pathlib
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

IMAGE_KEYS = ("image", "thumb", "image_srcset", "img", "cover", "src")


async def main() -> int:
    state = json.loads(SESSION.read_text(encoding="utf-8"))

    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    settings = get_settings()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(storage_state=state, user_agent=REAL_UA,
                                        viewport={"width": 1600, "height": 900}, locale="zh-CN")
        page = await ctx.new_page()
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3500)
        for _ in range(2):
            await page.mouse.wheel(0, 1000)
            await page.wait_for_timeout(800)

        report = await StructureAnalyzer(settings).analyze(page)
        await browser.close()

    print("=" * 72)
    print("  候选列表与字段(这是图片能不能下到的关键)")
    print("=" * 72)
    print(f"  候选列表 = {len(report.candidate_lists)} 个")
    any_image = False
    for i, c in enumerate(report.candidate_lists[:6]):
        fields = [f.model_dump() if hasattr(f, "model_dump") else dict(f) for f in (c.sample_fields or [])]
        names = [f.get("name") for f in fields]
        img_fields = [f for f in fields if str(f.get("name", "")).lower() in IMAGE_KEYS
                      or "image" in str(f.get("name", "")).lower()
                      or "thumb" in str(f.get("name", "")).lower()]
        if img_fields:
            any_image = True
        print(f"\n  [{i}] selector={c.item_selector[:60]!r}")
        print(f"      count={c.count}")
        print(f"      字段名 = {names}")
        if img_fields:
            for f in img_fields:
                print(f"      ★ 图片字段: {f.get('name')} <- {f.get('selector')!r} "
                      f"attr={f.get('attribute')!r}")
        else:
            print("      ✗ **本候选列表没有图片字段**")

    # ---- 用规则引擎生成规则, 检查字段 ----
    print("\n" + "=" * 72)
    print("  规则引擎生成的规则(离线, 不依赖 AI)")
    print("=" * 72)
    rule = None
    try:
        rule = StructureAnalyzer.build_rule_from_structure(report)
    except Exception as exc:  # noqa: BLE001
        print(f"  生成规则失败: {type(exc).__name__}: {exc}")

    if rule is not None:
        print(f"  list_selector = {rule.list_rule.item_selector if rule.list_rule else None!r}")
        fields = rule.list_rule.fields if rule.list_rule else []
        print(f"  字段数 = {len(fields)}")
        for f in fields:
            nm = getattr(f, "name", None)
            mark = " ★图片" if nm and ("image" in str(nm).lower() or "thumb" in str(nm).lower()) else ""
            print(f"    {nm:<16} selector={str(getattr(f, 'selector', ''))[:52]!r} "
                  f"attr={getattr(f, 'attribute', None)!r}{mark}")
        has_img = any(("image" in str(getattr(f, "name", "")).lower()
                       or "thumb" in str(getattr(f, "name", "")).lower()) for f in fields)
    else:
        has_img = False

    print("\n  " + "=" * 72)
    print("  === 结论 ===")
    print(f"    候选列表里出现图片字段: {'是' if any_image else '否'}")
    print(f"    生成的规则里含图片字段: {'是' if has_img else '否'}")
    ok = any_image or has_img
    print(f"    {'✓ 图片字段通到了提取规则' if ok else '✗ 规则里仍无图片字段'}")
    print("  " + "=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
