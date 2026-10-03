"""UI 实测: 登录状态提示条与手动登录弹层(截图存证)。

靶站默认用 pixiv(匿名首页是经典的"登录前后结构完全不同"场景)。若该站点在当前网络
下不可达, 本脚本会**明确跳过**而不是报失败 —— 环境不通和功能坏了是两件事, 混在一起
会让人白白去排查代码。可以用 `--target` 换一个可达的站点。
"""

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
OUT = Path("data/screenshots")
TARGET = "https://www.pixiv.net/"


def reachable(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """探测目标站点是否可达。返回 (是否可达, 说明)。"""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 有响应就算可达(403/401 也说明网络是通的)
        return True, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:70]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=TARGET, help="用于触发登录提示的站点")
    args = parser.parse_args()
    target = args.target

    OUT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    ok, why = reachable(target)
    print(f"靶站 {target} 可达性: {'✓' if ok else '✗'} {why}")
    if not ok:
        print("\n" + "=" * 60)
        print("登录 UI 实测: 跳过 ⊘ (靶站当前网络不可达, 非功能问题)")
        print(f"  可用 --target 指定其它站点重跑")
        print("=" * 60)
        return 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1680, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(800)
        page.fill("#analyzeUrl", target)
        page.click("#btnAnalyzeStart")

        # 等"登录一次"提示出现(匿名站点会给出这个提示)
        try:
            page.wait_for_selector("button:has-text('登录一次')", timeout=180000)
            banner = True
        except Exception:
            banner = False

        print(f"登录提示条: {'✓ 出现' if banner else '✗ 未出现'}")
        if not banner:
            failures.append("未出现登录提示条")
            # 也把当前页面内容打出来便于排查
            heads = page.eval_on_selector_all("#analyzeOutput h2", "e => e.map(x => x.textContent.trim())")
            print(f"  当前卡片: {heads}")
        else:
            # 检查提示文案里点明了"登录后结构可能不同"
            text = page.eval_on_selector("#analyzeOutput .alert", "e => e.textContent")
            print(f"  提示文案: {(text or '')[:120]}")
            if "登录" not in (text or ""):
                failures.append("提示条没有提到登录")
            shot = OUT / "18_login_state_banner.png"
            page.screenshot(path=str(shot))
            print(f"  截图 -> {shot.name}")

            # 打开登录弹层
            page.click("button:has-text('登录一次')")
            page.wait_for_timeout(1500)
            open_ok = page.eval_on_selector("#modal", "e => e.classList.contains('is-open')")
            print(f"登录弹层: {'✓ 已打开' if open_ok else '✗ 未打开'}")
            if not open_ok:
                failures.append("登录弹层未打开")
            else:
                # 弹层里应能看到安全说明与实际按钮
                body = page.eval_on_selector("#modal", "e => e.textContent")
                for needle, label in (
                    ("不会接触你的密码", "说明里声明了不接触密码"),
                    ("打开登录窗口", "有『打开登录窗口』按钮"),
                    ("保存会话", "有『保存会话』按钮"),
                    ("删除已保存的会话", "有退出登录入口"),
                ):
                    ok = needle in (body or "")
                    print(f"  {'✓' if ok else '✗'} {label}")
                    if not ok:
                        failures.append(label)
                shot2 = OUT / "19_login_modal.png"
                page.screenshot(path=str(shot2))
                print(f"  截图 -> {shot2.name}")
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)

        # ==============================================================
        # 已有会话时, 弹层必须直接给出"第 2 步"(这就是用户报的"没有下一步")
        # ==============================================================
        print("\n=== 已有会话时打开弹层: 应直接给出下一步 ===")
        from smartcrawler.session import save_storage_state  # noqa: PLC0415

        session_file = Path("data/session.json")
        backup = session_file.read_text(encoding="utf-8") if session_file.exists() else None
        try:
            save_storage_state(
                {
                    "cookies": [
                        {
                            "name": "demo_token",
                            "value": "UI-CHECK",
                            "domain": "127.0.0.1",
                            "path": "/",
                        }
                    ],
                    "origins": [],
                },
                session_file,
            )
            page.evaluate("window.location.hash = '#/crawl'")
            page.wait_for_timeout(500)
            page.evaluate("window.location.hash = '#/analyze'")
            page.wait_for_timeout(900)

            # 直接调起弹层(不必先跑一次分析)
            page.evaluate(
                """async () => {
                    const mod = await import('/js/ui/login-panel.js');
                    await mod.openLoginModal('https://example.com/', { state: 'anonymous', confidence: 0.9 });
                }"""
            )
            page.wait_for_timeout(2000)

            visible = page.eval_on_selector(
                "#modal .login-flow__next", "e => !e.hidden"
            )
            print(f"  「第 2 步」区块可见: {visible}")
            if not visible:
                failures.append("已有会话时弹层没有显示「第 2 步」")
            else:
                labels = page.eval_on_selector_all(
                    "#modal .login-flow__next button", "e => e.map(x => x.textContent.trim())"
                )
                print(f"  下一步选项: {labels}")
                for needle in ("重新分析", "抓取"):
                    ok = any(needle in (l or "") for l in labels)
                    print(f"    {'✓' if ok else '✗'} 含「{needle}」入口")
                    if not ok:
                        failures.append(f"第 2 步缺少「{needle}」入口")

                shot3 = OUT / "20_login_next_step.png"
                page.screenshot(path=str(shot3))
                print(f"  截图 -> {shot3.name}")
        finally:
            if backup is not None:
                session_file.write_text(backup, encoding="utf-8")
            else:
                session_file.unlink(missing_ok=True)

        browser.close()

    print(f"\n前端错误: {len(errors)}")
    for e in errors[:6]:
        print(f"  ! {e}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 60)
    if failures:
        print("登录 UI 实测: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("登录 UI 实测: 通过 ✓")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
