"""
启动器 —— 环境自检 + 启动 Web 控制台。

**为什么用 Python 而不是全写在 .bat 里**: ``cmd.exe`` 按控制台的 OEM 代码页(简体中文
Windows 上是 GBK)**逐字节**解析 ``.bat``, 文件里的多字节中文会把解析器搞乱 —— 实测会
报出一堆 ``'-----------------------' is not recognized`` 之类的错误。所以 ``start.bat``
刻意只用 ASCII, 把全部中文界面与检查逻辑放在这里(Python 按 UTF-8 读取, 天然安全)。

本脚本负责:
1. 找到合适的 Python 解释器(优先项目内 ``.venv``);
2. 缺虚拟环境时创建;
3. 检查依赖并补齐;
4. 检查浏览器内核(装在项目内 ``.browsers/``), 缺了就调用 bootstrap 脚本下载;
5. 检查端口占用;
6. 启动服务并(可选)自动打开浏览器。

单独运行也可以::

    python scripts/launcher.py [--port 8322] [--no-browser] [--check] [--reload]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 先把控制台切成 UTF-8 并准备好可写临时目录, 再做其它事情。
# 顺序很重要: 后面要 import smartcrawler.*, 它们会读取环境变量决定浏览器路径。
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

from smartcrawler.web.__main__ import (  # noqa: E402
    BROWSERS_DIR,
    RUNTIME_TMP,
    ensure_browsers_path,
    port_available,
    prepare_temp_dir,
)

#: 运行服务所必需的依赖(与应用内一致的检查列表)
REQUIRED_IMPORTS = (
    "fastapi",
    "uvicorn",
    "playwright",
    "pydantic",
    "pydantic_settings",
    "httpx",
    "loguru",
    "yaml",
    "dotenv",
)

BANNER = """
  ╔══════════════════════════════════════════════════════════╗
  ║           SmartCrawler · 智能爬虫控制台                  ║
  ╚══════════════════════════════════════════════════════════╝
"""


def info(message: str) -> None:
    print(f"  {message}")


def step(index: int, total: int, message: str) -> None:
    print(f"\n  [{index}/{total}] {message}")


def fail(title: str, *hints: str) -> int:
    print(f"\n  ✗ {title}\n")
    for hint in hints:
        print(f"    {hint}")
    print()
    return 1


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------
def ensure_interpreter() -> tuple[bool, str]:
    """确认当前解释器可用, 并在需要时创建 .venv。

    注意: 正常情况下 ``start.bat`` 已经选好了解释器 —— 它优先用项目内的 ``.venv``,
    没有才去找系统 Python。这里只负责"如果用的是系统 Python, 就顺手建一个 venv,
    把依赖装进项目内, 不污染全局环境"。
    """
    version = ".".join(str(v) for v in sys.version_info[:3])
    inside_project_venv = Path(sys.prefix).resolve() == (PROJECT_ROOT / ".venv").resolve()

    if inside_project_venv:
        info(f"解释器: 项目虚拟环境 ({version})")
        return True, sys.executable

    info(f"解释器: {sys.executable}")
    info(f"版本  : {version}")
    if sys.version_info < (3, 10):
        fail(
            "Python 版本过低, 需要 3.10 或更高。",
            "请安装 3.12(推荐): https://www.python.org/downloads/",
        )
        return False, sys.executable

    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        # 已存在别的 venv, 但本次不是用它启动的; 交给调用方决定, 这里不强行切换
        info("提示: 检测到已存在的 .venv, 但本次未使用它")
        return True, sys.executable

    info("未使用项目虚拟环境。为保持依赖隔离, 建议创建 .venv:")
    info("    python -m venv .venv")
    info("    .venv\\Scripts\\python -m pip install -r requirements.txt")
    info("或者直接重新运行 start.bat —— 它会自动完成上述步骤。")
    # 不阻塞: 全局环境里若已装好依赖, 照样能跑
    return True, sys.executable


def ensure_dependencies(python: str) -> bool:
    """检查必需依赖, 缺失则用 pip 安装。"""
    missing = _missing_modules(python)
    if not missing:
        info("依赖完整")
        return True

    info(f"缺少依赖: {', '.join(missing)}")
    info("正在安装(首次约需 1-3 分钟, 请稍候)...\n")
    result = subprocess.run(
        [python, "-m", "pip", "install", "--disable-pip-version-check", "-r",
         str(PROJECT_ROOT / "requirements.txt")],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        return bool(
            fail(
                "依赖安装失败。",
                "可尝试手动安装:",
                f'    "{python}" -m pip install -r requirements.txt',
                "网络较慢时可用国内镜像:",
                f'    "{python}" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple',
            )
        )

    still_missing = _missing_modules(python)
    if still_missing:
        return bool(
            fail(
                f"安装后仍无法导入: {', '.join(still_missing)}",
                "请查看上方 pip 的输出定位原因。",
            )
        )
    info("依赖安装完成")
    return True


def _missing_modules(python: str) -> list[str]:
    """逐个 import 检查(逐个检查比一次全导入更容易定位缺了哪个)。"""
    missing: list[str] = []
    for module in REQUIRED_IMPORTS:
        result = subprocess.run(
            [python, "-c", f"import {module}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            missing.append(module)
    return missing


def ensure_browser(python: str) -> bool:
    """确认 Chromium 内核存在, 缺失则调用 bootstrap 脚本下载。"""
    executable = _chromium_executable(python)
    if executable and Path(executable).exists():
        info("内核就绪")
        info(f"位置: {BROWSERS_DIR}")
        return True

    info(f"未找到 Chromium 内核(预期位置: {BROWSERS_DIR})")
    bootstrap = PROJECT_ROOT / "scripts" / "bootstrap_browsers.py"
    if not bootstrap.exists():
        return bool(
            fail(
                "缺少 scripts/bootstrap_browsers.py, 无法自动下载内核。",
                "可手动执行:",
                f'    "{python}" -m playwright install chromium',
            )
        )

    info("开始下载(约 200MB, 支持断点续传; 中断后重新运行即可继续)...\n")
    result = subprocess.run([python, str(bootstrap)], cwd=str(PROJECT_ROOT))
    if result.returncode != 0:
        return bool(
            fail(
                "浏览器内核下载失败。",
                "脚本支持断点续传, 重新运行 start.bat 即可继续。",
                "也可尝试官方安装器:",
                f'    "{python}" -m playwright install chromium',
            )
        )

    executable = _chromium_executable(python)
    if not executable or not Path(executable).exists():
        return bool(fail("内核下载后仍未找到可执行文件, 请检查上方输出。"))
    info("内核安装完成")
    return True


def _chromium_executable(python: str) -> str:
    """向子进程问一次 Chromium 的可执行路径(沿用项目内的 .browsers)。"""
    code = (
        "from playwright.sync_api import sync_playwright\n"
        "p = sync_playwright().start()\n"
        "print(p.chromium.executable_path)\n"
        "p.stop()\n"
    )
    try:
        result = subprocess.run(
            [python, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            cwd=str(PROJECT_ROOT),
        )
        return (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    except (subprocess.SubprocessError, IndexError, OSError):
        return ""


def resolve_port(cli_port: int | None) -> int:
    """端口优先级: 命令行 > .env 的 SC_API__PORT > 8322。"""
    if cli_port:
        return cli_port

    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped.startswith("SC_API__PORT") and "=" in stripped:
                    _, _, value = stripped.partition("=")
                    value = value.strip().strip('"').strip("'")
                    if value.isdigit():
                        return int(value)
        except OSError:
            pass
    return 8322


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="SmartCrawler 启动器: 环境自检 + 启动 Web 控制台",
    )
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--check", action="store_true", help="只做环境自检, 不启动服务")
    parser.add_argument("--reload", action="store_true", help="代码变更自动重载(开发用)")
    args = parser.parse_args()

    prepare_temp_dir()  # 设置 TMP/TEMP 与 PLAYWRIGHT_BROWSERS_PATH
    ensure_browsers_path()

    print(BANNER)
    info(f"项目目录: {PROJECT_ROOT}")
    info(f"临时目录: {RUNTIME_TMP}")

    total = 4
    step(1, total, "检查 Python 解释器...")
    ok, python = ensure_interpreter()
    if not ok:
        return 1

    step(2, total, "检查依赖...")
    if not ensure_dependencies(python):
        return 1

    step(3, total, "检查浏览器内核...")
    if not ensure_browser(python):
        return 1

    port = resolve_port(args.port)
    step(4, total, f"检查端口 {port}...")
    if not port_available("127.0.0.1", port):
        return fail(
            f"端口 {port} 已被占用。",
            "换一个端口重试:",
            f"    start.bat --port {port + 1}",
            "或排查占用进程:",
            f"    netstat -ano | findstr :{port}",
        )
    info("端口可用")

    if args.check:
        print(
            "\n  ══════════════════════════════════════════════════════════"
            "\n   ✓ 环境自检通过, 一切就绪。"
            "\n     运行 start.bat 即可启动控制台。"
            "\n  ══════════════════════════════════════════════════════════\n"
        )
        return 0

    # ---- 启动服务 ----
    url = f"http://127.0.0.1:{port}"
    print(
        "\n  ══════════════════════════════════════════════════════════"
        f"\n   控制台地址: {url}"
        f"\n   接口文档  : {url}/api/docs"
        "\n\n   按 Ctrl+C 停止服务"
        "\n  ══════════════════════════════════════════════════════════\n"
    )

    if not args.no_browser:
        _open_browser_when_ready(url, port)

    command = [python, "-m", "smartcrawler", "web", "--port", str(port)]
    if args.reload:
        command.append("--reload")

    # 启动一个"守望"线程: 一旦父控制台没了, 就把服务的整棵进程树带走。
    #
    # 为什么光靠服务自己的清理钩子不够: Windows 上用户直接点窗口的 × 关闭时, 系统给的是
    # CTRL_CLOSE_EVENT —— 进程只有几秒钟, Python 不保证跑完 atexit/finally, 于是
    # Playwright 的浏览器会留在后台(表现为"明明关了窗口, 浏览器还在跑、端口还占着")。
    # 守望线程从**外部**监控父进程, 父进程一消失就动手, 不依赖被通知者配合。
    if os.name == "nt":
        _start_parent_watchdog()

    try:
        result = subprocess.run(command, cwd=str(PROJECT_ROOT))
        code = result.returncode
    except KeyboardInterrupt:
        code = 0
    finally:
        _kill_leftover_processes()

    if code != 0:
        return fail(
            f"服务异常退出(代码 {code})。",
            "若提示端口被占用, 换端口重试:",
            f"    start.bat --port {port + 1}",
        )

    info("服务已正常停止。")
    return 0


def _kill_leftover_processes() -> None:
    """清掉本项目遗留的**孤儿**浏览器进程(尽力而为, 失败只提示不报错)。

    用 ``kill_orphan_processes`` 而不是无差别清理: 启动器退出时不该把同一个项目下
    **另一个仍在运行**的服务实例的浏览器一起杀掉。
    """
    script = (
        "import sys; sys.path.insert(0, r'%s');"
        "from smartcrawler.runtime import kill_orphan_processes;"
        "n = kill_orphan_processes(reason='启动器收尾');"
        "print(f'  已清理 {n} 个遗留浏览器进程' if n else '')" % PROJECT_ROOT
    )
    try:
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(PROJECT_ROOT),
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _start_parent_watchdog(poll_seconds: float = 2.0) -> None:
    """守护线程: 父控制台进程消失后, 清理并退出。

    用"父进程是否还在"作为信号, 而不是监听信号本身 —— 因为点 × 关闭窗口时
    **根本收不到可用的信号**(CTRL_CLOSE_EVENT 的窗口期太短)。
    """
    import threading
    import time

    parent = os.getppid()

    def _watch() -> None:
        while True:
            time.sleep(poll_seconds)
            if os.getppid() != parent:
                # 父进程没了: 说明控制台被关掉了
                _kill_leftover_processes()
                os._exit(0)

    thread = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    thread.start()


def _open_browser_when_ready(url: str, port: int, timeout: float = 40.0) -> None:
    """等服务真正开始监听后再打开浏览器。

    直接 ``start <url>`` 会因为服务还没就绪而显示"无法访问此网站" —— 用户会以为启动
    失败了。这里在一个子进程里轮询端口, 通了才打开。
    """
    code = (
        "import socket, sys, time, webbrowser\n"
        "url, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])\n"
        "deadline = time.time() + timeout\n"
        "while time.time() < deadline:\n"
        "    with socket.socket() as s:\n"
        "        s.settimeout(0.5)\n"
        "        if s.connect_ex(('127.0.0.1', port)) == 0:\n"
        "            webbrowser.open(url)\n"
        "            break\n"
        "    time.sleep(0.5)\n"
    )
    try:
        subprocess.Popen(
            [sys.executable, "-c", code, url, str(port), str(timeout)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass  # 打不开浏览器不影响服务启动


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已中断。")
        sys.exit(130)
