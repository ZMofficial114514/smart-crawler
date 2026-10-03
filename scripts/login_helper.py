"""
手动登录 / 手动过人机验证的辅助进程 —— 打开**可见**浏览器让用户操作, 完成后保存会话。

**为什么必须是独立进程**: Playwright 的浏览器对象绑定在创建它的 asyncio 事件循环上,
不可能"这个 HTTP 请求打开浏览器、下一个请求继续用"。所以这些流程放在独立进程里跑,
主服务通过状态文件观察进度、通过信号文件下达"确认保存/取消"。

**两种模式**:

- ``--mode login``(默认): 等用户登录;
- ``--mode challenge``: 等用户完成**人机验证**(reCAPTCHA / Turnstile / 滑块等)。会主动
  检测挑战是否还在, 挑战消失即认为已过; 用户点确认时若挑战仍在, 会如实体现在状态里 ——
  不假装成功。

**为什么不让框架自动填账号密码或绕验证**: 凭据管理的风险远超框架边界, 而人机验证本来
就是设计来拦自动化的(绕过它既不可靠也不合规)。这里只做两件事: 给用户一个能操作的
浏览器、把服务端签发的会话保存下来。框架自始至终**不接触用户密码**。

与主进程的约定(都在 .runtmp/login_flow/ 下):
- ``status.json``  本进程持续写入的进度(主服务读它来展示 UI)
- ``confirm``      主服务写入此文件表示"用户已点确认, 请保存并退出"
- ``cancel``       主服务写入此文件表示"用户取消了"
- ``state.json``   保存下来的 storage_state(主服务再合并进正式会话文件)

用法(由主服务调用, 一般不手工执行)::

    python scripts/login_helper.py --url https://www.pixiv.net/ --out data/session.json
    python scripts/login_helper.py --mode challenge --url https://x.com/ --out data/session.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from smartcrawler.config import get_settings  # noqa: E402
from smartcrawler.challenge import detect_challenge_sync  # noqa: E402
from smartcrawler.login_state import detect_login_state_sync  # noqa: E402
from smartcrawler.session import merge_storage_state, save_storage_state  # noqa: E402

FLOW_DIR = PROJECT_ROOT / ".runtmp" / "login_flow"
STATUS_FILE = FLOW_DIR / "status.json"
CONFIRM_FILE = FLOW_DIR / "confirm"
CANCEL_FILE = FLOW_DIR / "cancel"
STATE_FILE = FLOW_DIR / "state.json"

#: 自动确认的等待上限(秒)。到点仍未确认就保存并退出 —— 用户可能只是忘了点按钮,
#: 而已经登录成功, 保存下来比白白丢掉有用。
AUTO_SAVE_SECONDS = 900
POLL_INTERVAL = 1.0


def write_status(**fields: object) -> None:
    """写进度文件(原子替换, 避免主服务读到半个文件)。"""
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **fields}
    tmp = STATUS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartCrawler 手动登录辅助")
    parser.add_argument("--url", required=True, help="要登录的站点地址")
    parser.add_argument("--out", required=True, help="会话文件保存路径")
    parser.add_argument("--timeout", type=float, default=AUTO_SAVE_SECONDS, help="最长等待秒数")
    parser.add_argument(
        "--mode",
        default="login",
        choices=("login", "challenge"),
        help="login=等用户登录(默认); challenge=等用户完成人机验证",
    )
    parser.add_argument(
        "--engine", default=None, help="浏览器引擎(chromium/firefox/webkit), 默认取配置"
    )
    parser.add_argument(
        "--pre-auth-url",
        default=None,
        help=(
            "在打开目标站点前先访问这个地址(用于验收测试模拟『用户完成登录』这一步)。"
            "正常使用不需要它。"
        ),
    )
    args = parser.parse_args()
    return _run(args)


def _run(args: argparse.Namespace) -> int:
    """真正的工作主体(包一层便于把未预期异常也写进状态文件)。"""
    # 清掉可能残留的信号文件, 避免上一次的确认被误读
    for path in (CONFIRM_FILE, CANCEL_FILE, STATE_FILE):
        with contextlib.suppress(OSError):
            path.unlink()

    settings = get_settings()
    engine_name = args.engine or settings.browser.engine
    out_path = Path(args.out)

    write_status(state="launching", step="正在启动浏览器…", url=args.url, cookies=0)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        engine = getattr(p, engine_name)
        try:
            browser = engine.launch(headless=False)
        except Exception as exc:  # noqa: BLE001
            write_status(
                state="error",
                step="浏览器启动失败",
                error=f"{type(exc).__name__}: {exc}",
                hint="该环境可能没有图形界面; 请改用命令行登录, 或手动准备 session.json",
            )
            return 1

        context = browser.new_context(
            viewport={
                "width": settings.browser.viewport_width,
                "height": settings.browser.viewport_height,
            },
            locale=settings.browser.locale,
            timezone_id=settings.browser.timezone,
        )

        # 若已有会话, 先带上 —— 用户可能只是要"续期"而不是重新登录
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8"))
                if existing.get("cookies") or existing.get("origins"):
                    context.add_cookies(existing.get("cookies") or [])
                    write_status(
                        state="launching",
                        step="已载入既有会话, 正在打开页面…",
                        url=args.url,
                        cookies=len(existing.get("cookies") or []),
                    )
            except (json.JSONDecodeError, OSError):
                pass

        page = context.new_page()
        # 验收测试用: 先访问一个入口(例如站点自己的"登录"动作), 让浏览器拿到会话
        # Cookie。真实使用时用户是在窗口里手动操作的, 不需要这个参数。
        if args.pre_auth_url:
            try:
                page.goto(args.pre_auth_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(1)
            except Exception:  # noqa: BLE001 - 预置失败不影响主流程
                pass

        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            write_status(
                state="error",
                step="页面打开失败",
                error=f"{type(exc).__name__}: {exc}",
                hint="请检查地址是否正确、网络是否可用",
            )
            browser.close()
            return 1

        is_challenge_mode = args.mode == "challenge"

        write_status(
            state="waiting",
            mode=args.mode,
            step=(
                "浏览器已打开 —— 请在窗口中完成人机验证, 然后回到控制台点『验证已完成』"
                if is_challenge_mode
                else "浏览器已打开 —— 请在窗口中完成登录, 然后回到控制台点『我已登录, 保存会话』"
            ),
            url=args.url,
            cookies=0,
        )

        # ---- 轮询等待用户操作 ----
        deadline = time.time() + args.timeout
        last_state = ""
        last_report = 0.0
        auto_saved = False
        challenge_cleared = False

        while time.time() < deadline:
            if CANCEL_FILE.exists():
                write_status(state="cancelled", step="用户已取消")
                browser.close()
                return 0

            if CONFIRM_FILE.exists():
                break

            # 每 2 秒探测一次, 让界面上能实时看到"还没过/已登录"
            now = time.time()
            if now - last_report >= 2.0:
                last_report = now
                try:
                    state = detect_login_state_sync(page)
                    payload = {
                        "login_state": state.state,
                        "login_confidence": state.confidence,
                        "login_reasons": state.reasons[:6],
                        "page_title": state.page_title,
                        "current_url": page.url,
                    }

                    if is_challenge_mode:
                        # 人机验证模式: 关键信号是"挑战还在不在"
                        challenge = detect_challenge_sync(page)
                        challenge_cleared = not challenge.detected
                        payload.update(
                            challenge_detected=challenge.detected,
                            challenge_kind=challenge.kind,
                            challenge_reasons=challenge.reasons[:4],
                        )

                    if is_challenge_mode:
                        step = (
                            "验证已通过 —— 可以点『验证已完成, 保存会话』了"
                            if challenge_cleared
                            else f"请在浏览器窗口中完成验证({payload.get('challenge_kind') or '人机验证'})"
                        )
                        marker = f"challenge:{challenge_cleared}"
                    else:
                        step = (
                            "检测到已登录 —— 可以点『保存会话』了"
                            if state.logged_in
                            else "请在浏览器窗口中完成登录"
                        )
                        marker = state.state

                    if marker != last_state:
                        last_state = marker
                        write_status(state="waiting", mode=args.mode, step=step,
                                     url=args.url, cookies=0, **payload)
                    else:
                        # 只更新探测结果, 保留原来的 step 文案
                        write_status(state="waiting", mode=args.mode, url=args.url,
                                     cookies=0, **payload)
                except Exception:  # noqa: BLE001 - 探测失败不影响主流程
                    pass

            time.sleep(POLL_INTERVAL)
        else:
            auto_saved = True  # 超时: 保存已有会话后退出

        # ---- 保存会话 ----
        write_status(state="saving", step="正在保存会话…", url=args.url, cookies=0)
        try:
            captured = context.storage_state()
        except Exception as exc:  # noqa: BLE001
            write_status(state="error", step="读取会话失败", error=f"{type(exc).__name__}: {exc}")
            browser.close()
            return 1

        cookies = captured.get("cookies") or []
        origins = captured.get("origins") or []

        if not cookies and not origins:
            write_status(
                state="error",
                step="没有捕获到任何会话数据",
                error="浏览器里没有 Cookie 或 localStorage",
                hint="请确认已在浏览器窗口中完成登录, 然后再点保存",
            )
            browser.close()
            return 1

        try:
            # 与既有会话合并, 避免只登录了新站点却把旧站点的登录态冲掉
            base = None
            if out_path.exists():
                with contextlib.suppress(json.JSONDecodeError, OSError):
                    base = json.loads(out_path.read_text(encoding="utf-8"))
            merged = merge_storage_state(base, captured)
            save_storage_state(merged, out_path)
        except OSError as exc:
            write_status(state="error", step="写入会话文件失败", error=str(exc))
            browser.close()
            return 1

        # 挑战模式下若用户提前点了确认、但挑战其实还在, 如实说明: 不假装成功。
        challenge_still = False
        if is_challenge_mode and not auto_saved:
            with contextlib.suppress(Exception):
                challenge_still = detect_challenge_sync(page).detected

        step = (
            "已自动保存(等待超时)"
            if auto_saved
            else ("会话已保存(注意: 检测到验证可能还没过完)" if challenge_still else "会话已保存")
        )
        write_status(
            state="saved",
            mode=args.mode,
            step=step,
            url=args.url,
            cookies=len(cookies),
            origins=len(origins),
            output=str(out_path),
            auto_saved=auto_saved,
            challenge_detected=challenge_still,
        )

        # 留 2 秒让主服务把状态读走, 再关窗口
        time.sleep(2)
        with contextlib.suppress(Exception):
            browser.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        # 未预期异常也要落到状态文件里 —— 否则主服务只能看到一个"进程没了",
        # 用户界面上的提示会变成毫无信息量的"窗口可能已关闭"。
        import traceback

        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        write_status(
            state="error",
            step=f"登录辅助进程异常退出: {type(exc).__name__}",
            error=str(exc)[:400],
            hint="这是未预期的错误, 请把下面的堆栈反馈给开发者",
            traceback=detail[-2000:],
        )
        time.sleep(1)
        sys.exit(1)
