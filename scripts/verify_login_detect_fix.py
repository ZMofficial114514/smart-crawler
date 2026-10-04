"""
验证登录态判定修复: 同一份会话下, 判定应该从 unknown 变成 logged_in。

同时做**反向验证**: 不带会话(匿名)时必须仍是 anonymous —— 否则就是把判据放松过头,
把"任何页面都说已登录", 那比漏判更糟。
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

from smartcrawler.login_state import evaluate_login_state, DETECT_JS  # noqa: E402

SESSION = pathlib.Path("data/session.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CASES = [
    ("pixiv 已登录(带会话)", "https://www.pixiv.net/", True),
    ("pixiv 匿名(不带会话)", "https://www.pixiv.net/", False),
    ("books.toscrape 匿名", "https://books.toscrape.com/", False),
]


async def check(pw, label, url, with_session, state):
    browser = await pw.chromium.launch(headless=True)
    kwargs = {"user_agent": UA, "viewport": {"width": 1280, "height": 900}, "locale": "zh-CN"}
    if with_session and state is not None:
        kwargs["storage_state"] = state
    ctx = await browser.new_context(**kwargs)
    page = await ctx.new_page()
    out = {"label": label}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        raw = await page.evaluate(DETECT_JS)
        result = evaluate_login_state(raw, session_restored=with_session)
        out.update({
            "state": result.state,
            "confidence": result.confidence,
            "summary": result.summary,
            "signals": result.signals,
            "reasons": result.reasons,
            "anon_score": result.anon_score,
            "auth_score": result.auth_score,
        })
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    await browser.close()
    return out


async def main() -> int:
    state = json.loads(SESSION.read_text(encoding="utf-8"))
    results = []
    async with async_playwright() as pw:
        for label, url, with_session in CASES:
            r = await check(pw, label, url, with_session, state)
            results.append(r)
            await asyncio.sleep(4)

    print("  === 判定结果 ===")
    for r in results:
        print(f"\n  ── {r['label']} ──")
        if "error" in r:
            print(f"    错误: {r['error']}")
            continue
        print(f"    state      = {r['state']}   置信度 = {r['confidence']}")
        print(f"    summary    = {r['summary']}")
        print(f"    得分        = 已登录 {r['auth_score']:.2f} / 未登录 {r['anon_score']:.2f}")
        s = r["signals"]
        print(f"    信号        = logout={s.get('logout')} avatar={s.get('avatar_count')} "
              f"own_nav={s.get('own_nav_count')} account_path={s.get('account_path_count')} "
              f"login_links={s.get('login_link_count')}")
        for reason in r["reasons"][:4]:
            print(f"      · {reason}")

    print("\n  " + "=" * 62)
    by = {r["label"]: r for r in results}
    p_auth = by.get("pixiv 已登录(带会话)", {})
    p_anon = by.get("pixiv 匿名(不带会话)", {})
    a = p_auth.get("state") == "logged_in"
    b = p_anon.get("state") == "anonymous"
    print(f"  {'✓' if a else '✗'} pixiv 已登录 -> 判定为 logged_in (实际 {p_auth.get('state')})")
    print(f"  {'✓' if b else '✗'} pixiv 匿名   -> 判定为 anonymous (实际 {p_anon.get('state')})")
    print("  (反向验证: 确保不是『什么都判已登录』)")
    print("  " + "=" * 62)
    return 0 if (a and b) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
