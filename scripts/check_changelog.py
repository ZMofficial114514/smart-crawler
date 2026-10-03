"""核实 CHANGELOG.md 里的关键事实性声明是否与代码一致。

变更日志最容易出的问题是"写了但代码里并不是这样"。这个脚本把 CHANGELOG 里可验证的
断言逐条对着代码/文件核对。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

failures: list[str] = []
total = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def main() -> int:
    text = CHANGELOG.read_text(encoding="utf-8")
    import smartcrawler

    current = smartcrawler.__version__

    print("=== 1) 结构 ===")
    check(Path(CHANGELOG).exists(), "CHANGELOG.md 存在")
    versions = re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\] — (\d{4}-\d{2}-\d{2})", text, re.M)
    check(len(versions) >= 3, "至少记录了 3 个版本", str([v[0] for v in versions]))
    # 期望值从包版本推导, 不写死 —— 写死的话每次升版本这条都会误报,
    # 而"误报"和"真的忘了写 CHANGELOG"很难区分。
    check(
        bool(versions) and versions[0][0] == current,
        f"最新版本条目是 {current}(与 __version__ 一致)",
        versions[0][0] if versions else "无",
    )
    for section in ("### 新增", "### 修复", "### 变更"):
        check(section in text, f"含小节 {section}")

    print("\n=== 2) 版本号与代码一致 ===")
    check(
        bool(versions) and versions[0][0] == current,
        "CHANGELOG 最新版本 == smartcrawler.__version__",
        f"{versions[0][0] if versions else '无'} vs {current}",
    )

    print("\n=== 3) 声称新增的文件确实存在 ===")
    claimed_files = [
        "smartcrawler/access_control.py",
        "smartcrawler/page_diagnostics.py",
        "smartcrawler/session.py",
        "smartcrawler/login_state.py",
        "smartcrawler/web/login_flow.py",
        "scripts/login_helper.py",
        "scripts/launcher.py",
        "scripts/check_version.py",
        "scripts/verify_version_check.py",
        "scripts/verify_access_control.py",
        "scripts/verify_luogu_case.py",
        "scripts/verify_session.py",
        "scripts/verify_login_flow.py",
        "start.bat",
        "smartcrawler/web/frontend/js/ui/login-panel.js",
        "smartcrawler/web/frontend/js/ui/task-owner.js",
        "smartcrawler/plugins/builtin/image_downloader.py",
        "smartcrawler/plugins/builtin/music_downloader.py",
        "smartcrawler/plugins/builtin/anti_bot.py",
        "smartcrawler/plugins/builtin/declarative.py",
    ]
    for rel in claimed_files:
        check((ROOT / rel).exists(), f"存在 {rel}")

    print("\n=== 4) 声称删除/重命名的确实如此 ===")
    check(not (ROOT / "smartcrawler/login_wall.py").exists(), "login_wall.py 已删除(被 access_control 取代)")

    models = (ROOT / "smartcrawler/models.py").read_text(encoding="utf-8")
    check("class AccessIssue" in models, "models.py 定义了 AccessIssue")
    check("class LoginWallInfo" not in models, "models.py 不再有 LoginWallInfo")
    check("access_issue" in models, "TaskResult/Report 用 access_issue 字段")
    check("login_state" in models, "login_state 字段存在")

    print("\n=== 5) 版本号唯一来源的说法成立 ===")
    init = (ROOT / "smartcrawler/__init__.py").read_text(encoding="utf-8")
    check(f'__version__ = "{current}"' in init, f"包根定义 __version__ = {current}")
    for rel in ("smartcrawler/web/service.py", "smartcrawler/web/api.py", "smartcrawler/web/api_legacy.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        check("__version__" in src, f"{rel} 从包根读取版本")
        check(
            not re.search(r'version\s*=\s*"[0-9]+\.[0-9]+\.[0-9]+"', src)
            and not re.search(r'"version":\s*"[0-9]+\.[0-9]+\.[0-9]+"', src),
            f"{rel} 没有硬编码版本号",
        )

    print("\n=== 6) 声称的修复确实落在代码里 ===")
    browser = (ROOT / "smartcrawler/browser.py").read_text(encoding="utf-8")
    check("RETRYABLE_STATUS" in browser, "browser.py 有可重试状态码集合")
    check("import json" in browser, "browser.py 已导入 json")
    check("storage_state" in browser and "new_context(**context_kwargs)" in browser,
          "会话恢复走 new_context(storage_state=)")
    check("_session_restored" in browser, "BrowserManager 记录会话是否恢复")

    state = (ROOT / "smartcrawler/web/state.py").read_text(encoding="utf-8")
    check("_history" in state and "replay" in state, "EventBus 保留历史并支持补发")

    ws = (ROOT / "smartcrawler/web/ws.py").read_text(encoding="utf-8")
    check("replay=True" in ws, "WebSocket 订阅时启用补发")

    utils = (ROOT / "smartcrawler/utils.py").read_text(encoding="utf-8")
    check("def force_utf8_output" in utils, "utils.py 暴露 force_utf8_output")
    cli = (ROOT / "smartcrawler/cli.py").read_text(encoding="utf-8")
    check("force_utf8_output()" in cli, "cli.py 在打印横幅前先切 UTF-8")

    owner = (ROOT / "smartcrawler/web/frontend/js/ui/task-owner.js").read_text(encoding="utf-8")
    check("isOwnTaskEvent" in owner, "task-owner.js 提供 isOwnTaskEvent")
    for rel in ("analyze", "crawl", "requests"):
        src = (ROOT / f"smartcrawler/web/frontend/js/pages/{rel}.js").read_text(encoding="utf-8")
        check("isOwnTaskEvent" in src, f"{rel}.js 使用统一归属判断")

    u = (ROOT / "smartcrawler/web/frontend/js/utils.js").read_text(encoding="utf-8")
    check("renderPlainText" in u and "unescapeDisplay" in u, "utils.js 有纯文本渲染与转义还原")

    print("\n=== 7) 声称的默认值确实如此 ===")
    config = (ROOT / "smartcrawler/config.py").read_text(encoding="utf-8")
    check('storage_state: str = "data/session.json"' in config,
          "storage_state 默认指向 data/session.json")

    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("data/session.json" in gi, ".gitignore 忽略 data/session.json")
    check("data/sessions/" in gi, ".gitignore 忽略 data/sessions/")

    image = (ROOT / "smartcrawler/plugins/builtin/image_downloader.py").read_text(encoding="utf-8")
    check("default_enabled = True" in image, "图片下载器默认启用")

    print("\n=== 8) 声称的接口端点存在 ===")
    session_routes = (ROOT / "smartcrawler/web/routes/session.py").read_text(encoding="utf-8")
    for path in ('@router.get(""', '@router.post("/login"', '@router.get("/login/status"',
                 '@router.post("/login/confirm"', '@router.post("/login/cancel"',
                 '@router.post("/login/reset"', '@router.delete(""'):
        check(path in session_routes, f"session 路由含 {path}")

    print("\n=== 9) 验收脚本清单与实际文件一致 ===")
    listed = set(re.findall(r"`(scripts/[\w_]+\.py|tests/[\w_]+\.py)`", text))
    for rel in sorted(listed):
        check((ROOT / rel).exists(), f"清单里的 {rel} 存在")

    print("\n" + "=" * 62)
    if failures:
        print(f"CHANGELOG 事实核查: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"CHANGELOG 事实核查: 通过 ✓ ({total}/{total})")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
