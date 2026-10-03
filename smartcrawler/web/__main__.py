"""
Web 服务启动引导。

**为什么需要这个文件**: Playwright 启动浏览器时会在系统临时目录下创建
``playwright-artifacts-XXXXXX`` 用于存放用户数据目录与截图等中间产物。在受限环境
(沙箱、只读 TEMP、企业策略)下这一步会直接抛 ``EPERM: operation not permitted,
mkdtemp``。这里在导入 Playwright 之前把 ``TMP/TEMP/TMPDIR`` 指向项目内的可写目录,
从根上绕开该问题; 用户无需手动设置任何环境变量。

同时负责: 端口占用时的友好报错、启动横幅打印。
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

from ..config import PROJECT_ROOT
from ..utils import force_utf8_output

# 浏览器临时产物目录(已在 .gitignore 中忽略)
RUNTIME_TMP = PROJECT_ROOT / ".runtmp"
# 浏览器内核的落地目录(项目内, 可写; 避免受系统级目录权限/杀软限制)
BROWSERS_DIR = PROJECT_ROOT / ".browsers"


def ensure_browsers_path() -> Path:
    """把 Playwright 的浏览器缓存指到项目内的可写目录。

    默认位置是 ``%LOCALAPPDATA%\\ms-playwright``(Windows)。在受限环境里该目录
    可能被安全策略/杀软拦截写入, 表现为"内核下载完了却解压不出文件"。项目内目录
    总是可写的, 因此统一指向这里 —— 用户无需配置任何环境变量。

    若用户已经显式设置了 ``PLAYWRIGHT_BROWSERS_PATH``, 则尊重其选择。
    """
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    return Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])


def prepare_temp_dir() -> Path:
    """把临时目录指向项目内的 .runtmp(可写), 返回该目录。

    若系统临时目录本来就可用, 也依然统一到这里 —— 便于排查与清理。
    同时把控制台输出切成 UTF-8(复用 :func:`smartcrawler.utils.force_utf8_output`),
    否则启动横幅里的中文与框线字符在 Windows 上会乱码。
    """
    force_utf8_output()

    RUNTIME_TMP.mkdir(parents=True, exist_ok=True)
    os.environ["TMP"] = str(RUNTIME_TMP)
    os.environ["TEMP"] = str(RUNTIME_TMP)
    os.environ["TMPDIR"] = str(RUNTIME_TMP)
    tempfile.tempdir = str(RUNTIME_TMP)
    ensure_browsers_path()
    return RUNTIME_TMP


def port_available(host: str, port: int) -> bool:
    """检查端口是否可绑定(用于给出"端口被占用"的清晰提示)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


BANNER = r"""
   _____                 __   ______                 __
  / ___/____ ___  ____ _/ /_ / ____/______ __      __/ /___ ______
  \__ \/ __ `__ \/ __ `/ __// /   / ___/ __ \ | /| / / / __ `/ ___/
 ___/ / / / / / / /_/ / /_ / /___/ /  / /_/ / |/ |/ / / /_/ / /
/____/_/ /_/ /_/\__,_/\__/ \____/_/   \____/|__/|__/_/\__,_/_/
"""


def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> int:
    """启动 Web 控制台。"""
    try:
        import uvicorn
    except ImportError:
        print("未安装 fastapi/uvicorn, 请先执行: pip install fastapi 'uvicorn[standard]'", file=sys.stderr)
        return 2

    from ..config import get_settings

    settings = get_settings()
    host = host or settings.api.host
    port = port or settings.api.port

    prepare_temp_dir()

    if not port_available(host, port):
        print(
            f"\n[!] 端口 {host}:{port} 已被占用。\n"
            f"    换个端口: python -m smartcrawler serve --port {port + 1}\n"
            f"    或查看占用进程: netstat -ano | findstr :{port}\n",
            file=sys.stderr,
        )
        return 1

    print(f"  SmartCrawler 控制台  ->  http://{host}:{port}")
    print(f"  接口文档             ->  http://{host}:{port}/api/docs")
    print(f"  运行临时目录         ->  {RUNTIME_TMP}")
    print("  合规提示: 默认遵守 robots.txt 并限速, 仅限合法授权的数据采集")
    print("  停止服务: 关掉本窗口(或按 Ctrl+C)即可, 框架会把浏览器子进程一并清理\n")

    # 自己建 Server 实例(而不是 uvicorn.run), 这样能把实例注册给"界面关闭服务"用。
    # uvicorn.run 内部创建的 Server 我们拿不到引用, 界面上的关闭按钮就只能干瞪眼。
    config = uvicorn.Config(
        "smartcrawler.web.api:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",  # 业务日志由 loguru 总线接管, uvicorn 只留警告
        access_log=False,
    )
    server = uvicorn.Server(config)

    from ..runtime import install_shutdown_cleanup, kill_our_processes, shutdown_controller

    install_shutdown_cleanup()

    def _bind_controller() -> None:
        """等事件循环起来后把 Server 实例交给控制器(供 /api/system/shutdown 使用)。"""
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 理论上不会发生
            return
        shutdown_controller.bind(server, loop)

    # 在 uvicorn 启动前拿不到循环, 所以用启动回调挂上去
    original_startup = server.startup

    async def _startup_with_binding(*args: object, **kwargs: object) -> None:
        _bind_controller()
        await original_startup(*args, **kwargs)  # type: ignore[misc]

    server.startup = _startup_with_binding  # type: ignore[method-assign]

    try:
        server.run()
    finally:
        # 无论正常退出还是被信号打断, 都再兜一次底。
        #
        # **必须限定 root_pid=自己的 pid**: 不限定就等于"清掉所有属于本框架的进程",
        # 于是同一个项目下另一个仍在运行的服务实例的浏览器会被一起杀掉。
        # 这个越界真实发生过 —— 而且是靠日志里这一行的 `root_pid=None` 才定位到的:
        # 之前只给 atexit 与信号处理器加了限定, 漏了这里, 结果每次退出都全清。
        kill_our_processes(reason="serve 返回", root_pid=os.getpid())
    return 0
