"""
在**真实有头浏览器**里访问 pixiv, 等用户手动登录, 然后保持窗口打开并分析结构。

与框架流程的区别(这正是本次要验证的):
  - 用**独立浏览器实例**, 全屏窗口, 真实桌面 UA, 不注入任何 stealth 脚本;
  - 登录后**不关闭浏览器**, 在同一个上下文里直接分析 —— 用户提出的思路;
  - 同时把会话存下来, 供后续对比"关掉再恢复"是否等价。

阶段:
  1) 打开 pixiv, 轮询登录态; 用户看到窗口后手动登录
  2) 检测到已登录 -> 保存会话 -> 立即分析结构(窗口保持打开)
  3) 分析结果写入文件并打印摘要
"""

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from playwright.async_api import async_playwright  # noqa: E402

TARGET = "https://www.pixiv.net/"
SESSION_OUT = pathlib.Path("data/session.json")
SHOT_DIR = pathlib.Path("data/screenshots")
SHOT_DIR.mkdir(parents=True, exist_ok=True)
TREE_OUT = SHOT_DIR / "pixiv_manual_login_tree.txt"

#: 真实桌面 Chrome。有头模式下**必须**用真实 UA —— 之前框架用随机 UA,
#: 那本身就不像"真实浏览器环境"。
REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

#: 只用于判断"用户是否已登录"。判据是 pixiv 自己的会话证据, 不依赖框架逻辑。
LOGIN_CHECK_JS = r"""
() => {
    const ACCOUNT = ['/logout', '/stacc', '/dashboard', '/history.php',
                     '/bookmark.php', '/novel/marker_all', '/manage/requests',
                     '/group/group_list'];
    const hits = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const h = a.getAttribute('href') || '';
        if (ACCOUNT.some(p => h.includes(p))) hits.push(h);
    }
    const vis = (el) => {
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    let visibleHits = 0;
    for (const a of document.querySelectorAll('a[href]')) {
        const h = a.getAttribute('href') || '';
        if (ACCOUNT.some(p => h.includes(p)) && vis(a)) visibleHits++;
    }
    return {
        accountLinks: hits.length,
        visibleAccountLinks: visibleHits,
        hasLoginModal: /用pixiv账号登录|登录|ログイン/.test(
            (document.body ? document.body.innerText : '').slice(0, 400)),
        title: document.title,
        url: location.href,
    };
}
"""


async def analyse(page) -> tuple[dict, str]:
    """在**当前这个**上下文里分析结构(不重开浏览器)。"""
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.login_state import DETECT_JS, evaluate_login_state  # noqa: PLC0415
    from smartcrawler.structure import StructureAnalyzer  # noqa: PLC0415

    report = await StructureAnalyzer(get_settings()).analyze(page)

    # 登录态判定也一并跑, 用来验证"检测器"在这个真实环境里怎么说
    raw = await page.evaluate(DETECT_JS)
    login = evaluate_login_state(raw, session_restored=False)

    info = {
        "title": report.title,
        "candidate_lists": len(report.candidate_lists),
        "tree_chars": len(report.simplified_tree or ""),
        "tree_lines": len([x for x in (report.simplified_tree or "").splitlines() if x.strip()]),
        "login_state": login.state,
        "login_confidence": login.confidence,
        "login_signals": login.signals,
        "login_reasons": login.reasons,
    }
    return info, (report.simplified_tree or "")


async def main() -> int:
    print("=" * 70)
    print("  真实有头浏览器 —— pixiv 手动登录 + 结构分析")
    print("=" * 70)
    print(f"  目标: {TARGET}")
    print("  窗口即将打开, 请在其中完成登录(含可能的人机验证)")
    print("  登录完成后我会自动检测到, 并**保持窗口打开**进行分析\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized",
                  "--window-position=0,0"],
        )
        ctx = await browser.new_context(
            user_agent=REAL_UA,
            viewport={"width": 1600, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await ctx.new_page()

        # ---- 阶段 1: 打开并等待用户登录 ----
        print("  [1/3] 正在打开 pixiv…")
        for attempt in range(4):
            try:
                await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"    导航失败({attempt + 1}/4): {type(exc).__name__}")
                await page.wait_for_timeout(3000)
        else:
            print("  无法打开 pixiv, 退出")
            await browser.close()
            return 2

        await page.bring_to_front()
        print("  [2/3] 窗口已打开, 等待你登录…(最长等 10 分钟)")

        deadline = time.time() + 600
        logged_in = False
        last_note = 0.0
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                st = await page.evaluate(LOGIN_CHECK_JS)
            except Exception:  # noqa: BLE001 - 用户正在导航/跳转时会短暂失败
                continue
            if st["accountLinks"] > 0:
                logged_in = True
                print(f"\n  ✓ 检测到已登录! 标题={st['title']!r}")
                print(f"    登录专属入口 = {st['accountLinks']} 个 "
                      f"(其中可见 {st['visibleAccountLinks']} 个)")
                break
            now = time.time()
            if now - last_note > 30:
                last_note = now
                left = int(deadline - now)
                print(f"    …仍在等待登录(剩余 {left}s)  当前标题={st['title'][:34]!r}")

        if not logged_in:
            print("\n  超时未检测到登录。窗口保持打开, 你可以继续登录;")
            print("  也可以关掉窗口后重新运行本脚本。")
            await page.wait_for_timeout(60000)
            await browser.close()
            return 3

        # ---- 保存会话(供后续"关闭再恢复"的对比) ----
        try:
            SESSION_OUT.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(SESSION_OUT))
            data = json.loads(SESSION_OUT.read_text(encoding="utf-8"))
            n_cookie = len(data.get("cookies") or [])
            n_pixiv = len([c for c in (data.get("cookies") or [])
                           if "pixiv" in (c.get("domain") or "")])
            print(f"  会话已保存 -> {SESSION_OUT} "
                  f"({n_cookie} 个 Cookie, 其中 pixiv {n_pixiv} 个)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 保存会话失败: {type(exc).__name__}: {exc}")

        # ---- 阶段 2: 保持窗口打开, 就在这个上下文里分析 ----
        print("\n  [3/3] 正在分析结构(**窗口保持打开**, 不重开浏览器)…")
        await page.wait_for_timeout(2000)

        # 滚动一下触发懒加载, 让结构更完整
        for _ in range(3):
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(900)
        await page.mouse.wheel(0, -4000)
        await page.wait_for_timeout(1200)

        try:
            info, tree = await analyse(page)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 分析失败: {type(exc).__name__}: {exc}")
            await browser.close()
            return 4

        TREE_OUT.write_text(tree, encoding="utf-8")
        shot = SHOT_DIR / "pixiv_manual_login.png"
        await page.screenshot(path=str(shot), full_page=False)

        print("\n  " + "=" * 66)
        print("  === 分析结果(真实有头浏览器 · 登录态) ===")
        print(f"    标题          = {info['title']!r}")
        print(f"    候选列表      = {info['candidate_lists']}")
        print(f"    结构树        = {info['tree_lines']} 节点 / {info['tree_chars']} 字符")
        print(f"    登录态判定    = {info['login_state']} (置信度 {info['login_confidence']})")
        sig = info["login_signals"]
        print(f"    信号          = logout={sig.get('logout')} avatar={sig.get('avatar_count')} "
              f"own_nav={sig.get('own_nav_count')} account_path={sig.get('account_path_count')} "
              f"login_links={sig.get('login_link_count')}")
        for r in (info["login_reasons"] or [])[:5]:
            print(f"      · {r}")
        print(f"\n    结构树 -> {TREE_OUT}")
        print(f"    截图   -> {shot}")
        print("  " + "=" * 66)

        # 窗口保持打开一段时间, 方便用户自己看
        print("\n  窗口将保持打开 20 秒供你查看(之后自动关闭)…")
        await page.wait_for_timeout(20000)
        await browser.close()
        print("  已关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
