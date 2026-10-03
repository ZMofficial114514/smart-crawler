"""
进程与服务的收尾管理 —— "关掉命令行就该把爬虫一起带走"。

## 为什么需要它

Playwright 自己会拉起一棵**独立进程树**:

```
python -m smartcrawler web        <- 服务本体
  └─ .venv/.../playwright/driver/node.exe   <- Playwright 驱动
       └─ .browsers/chromium_headless_shell-*/chrome-headless-shell.exe
            └─ (若干 --type=renderer / gpu-process 子进程)
```

直接关掉终端窗口时, 服务进程收到的是 Windows 的"控制台关闭"事件 —— Python 不保证
执行 ``finally``/``atexit``, 于是这棵树会**留在后台**: 表现为"明明关了窗口, 浏览器
还在跑、端口还占着、下次启动报端口被占用"。实测就是这样。

## 怎么做到"精确清理"

只按项目自己的特征匹配, **绝不按进程名乱杀**:

- ``.browsers`` —— Playwright 内核装在项目内(见 ``web/__main__.ensure_browsers_path``),
  所以"命令行里出现本项目的 .browsers 路径"的进程一定是我们拉起来的;
- ``.venv/.../playwright/driver`` —— 同理, 驱动也在项目内。

这样既不会误杀用户自己的 Chrome, 也不会动到 IDE/编辑器里的 Python 进程 ——
后者在"按名字杀 python"的写法里是最常见的误伤。
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from .config import PROJECT_ROOT

#: 本项目内、只有我们自己的进程才会用的路径特征
_SIGNATURES = (
    str(PROJECT_ROOT / ".browsers").lower(),
    str(Path(__file__).resolve().parent.parent / "driver").lower(),  # .../playwright/driver
)

#: 需要清理的可执行文件名。
#: **刻意不含 node.exe** —— 驱动进程确实叫 node.exe, 但 DSH 控制台、其它工具也用
#: node.exe; 靠名字匹配太危险。驱动本身没有浏览器时毫无意义(会自行退出), 所以只清
#: 浏览器进程就够, 而且"只按自己项目路径匹配"这条底线要守住。
_PROCESS_NAMES = (
    "chrome-headless-shell.exe",
    "headless_shell.exe",
    "chrome.exe",       # 有头模式下的 Chromium(路径仍落在项目 .browsers 里)
    "firefox.exe",
    "webkit.exe",
    "msedgewebview2.exe",
)


def _iter_candidates() -> list[tuple[int, str, str, int]]:
    """返回 [(pid, name, commandline, parent_pid)], 仅限 Windows。

    用 CIM 而不是 ``tasklist``: 后者拿不到命令行与父进程号, 就没法按路径特征精确筛选,
    也做不到"只清自己这一支"。
    """
    if os.name != "nt":  # pragma: no cover - 其它平台的清理逻辑不同
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            # 必须显式指定: PowerShell 输出含中文(路径/用户名)时, 默认的 GBK 解码会
            # 直接抛 UnicodeDecodeError, 整个清理就此失效。errors="replace" 保证即使
            # 有非法字节也能拿到 JSON —— 我们要匹配的是 ASCII 路径, 替换掉中文不影响。
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(f"进程枚举失败, 跳过清理: {exc}")
        return []

    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        import json

        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]

    out: list[tuple[int, str, str, int]] = []
    for entry in data or []:
        try:
            pid = int(entry.get("ProcessId") or 0)
            ppid = int(entry.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
        name = str(entry.get("Name") or "")
        cmd = str(entry.get("CommandLine") or "")
        if pid and name:
            out.append((pid, name, cmd, ppid))
    return out


def _looks_like_ours(command_line: str) -> bool:
    """命令行里是否出现本项目的私有路径特征。"""
    lowered = (command_line or "").lower()
    return any(sig in lowered for sig in _SIGNATURES)


def _live_pids() -> set[int]:
    """一次性拿到系统上所有活着的 PID。

    **为什么必须真的去问系统**: 枚举进程时可能出现"子进程被列出、父进程没被列出"的
    情况(权限/时序/被过滤)。此时若把"不在枚举表里"当成"父进程已死", 就会把**正在被
    使用的**浏览器误判成孤儿并清掉 —— 实测就是这样误杀另一个实例的浏览器的。
    用一次 ``tasklist`` 拿到活跃 PID 集合(只解析 PID 列, 不受中文编码影响), 再判断。
    """
    if os.name != "nt":  # pragma: no cover
        return set()
    try:
        completed = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    live: set[int] = set()
    for line in (completed.stdout or "").splitlines():
        # 形如: "chrome.exe","1234","Console","1","123,456 K"
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            live.add(int(parts[1]))
        except ValueError:
            continue
    return live


def _trace_owner(
    ppid: int,
    entries: dict[int, tuple[int, str, str]],
    live: set[int],
    *,
    root_pid: Optional[int],
) -> tuple[bool, bool]:
    """沿父进程链往上追, 返回 ``(是否属于 root_pid, 是否孤儿)``。

    链条的真实形状::

        python(服务) -> node.exe(playwright 驱动) -> chrome-headless-shell -> renderer...

    **关键点: 遇到 node.exe 这类"非本框架"的中间节点时必须继续往上, 不能就地判定。**
    驱动进程名是 ``node.exe``, 而 ``_PROCESS_NAMES`` **刻意不含 node.exe**
    (否则会误伤 DSH 控制台等其它工具的 node)。早先的实现每次都在驱动这一层停下并
    判成"不算自己人", 于是两个服务的进程得到**完全一样**的结论 —— 这不可能是对的。
    实测日志把这一点暴露得很清楚::

        临时服务浏览器 -> chrome#32696 -> node.exe#37484 [遇到非本框架祖先 -> 不算]
        主服务浏览器   -> chrome#30288 -> node.exe#26712 [遇到非本框架祖先 -> 不算]

    正确判据: **一直往上走到 root_pid 命中为止**; 只有当链条走到一个"活着且不像自家
    人"的终点(真正的 owner 是别人)时, 才判定不属于自己。
    """
    cursor, hops, seen_ours = ppid, 0, False
    while cursor and cursor > 0 and hops < 16:
        if root_pid is not None and cursor == root_pid:
            return True, False
        if cursor not in live:
            # 祖先确实已退出: 链条到此为止, 没有活着的 owner
            return (seen_ours, True) if root_pid is None else (False, True)
        node = entries.get(cursor)
        if node is None:
            # 祖先活着但没被枚举进来(权限/过滤)。无法确认归属 -> 保守判为"有人管"
            return (seen_ours, False) if root_pid is None else (False, False)
        parent_ppid, _pn, pcmd = node
        if _looks_like_ours(pcmd):
            seen_ours = True
        cursor = parent_ppid
        hops += 1
    return (seen_ours, False) if root_pid is None else (False, False)


def find_orphan_processes() -> list[tuple[int, str]]:
    """只找出**孤儿**框架进程 —— 父进程链上已经没有活着的 owner 的那些。

    **为什么不能无差别清"所有属于本框架的进程"**: 同一个项目下同时跑两个服务实例
    (主服务 + 调试实例)时, 新实例一启动就会把老实例的浏览器全部杀掉。实测精确复现过。

    判据: 沿父进程链往上追溯, 只要遇到**一个活着且不属于本框架特征**的祖先
    (例如某个 python.exe 服务进程), 就说明这支有人管, 不是孤儿; 只有整条链都追不到
    活着的 owner 时, 才认定是上次崩溃留下的残留。
    """
    if os.name != "nt":  # pragma: no cover
        return []

    entries: dict[int, tuple[int, str, str]] = {}
    for pid, name, cmd, ppid in _iter_candidates():
        entries[pid] = (ppid, name, cmd)
    live = _live_pids()

    me = os.getpid()
    out: list[tuple[int, str]] = []
    for pid, (ppid, name, cmd) in entries.items():
        if pid == me or name.lower() not in _PROCESS_NAMES:
            continue
        if not _looks_like_ours(cmd):
            continue
        _owned, orphan = _trace_owner(ppid, entries, live, root_pid=None)
        if orphan:
            out.append((pid, name))
    return out


def kill_orphan_processes(*, reason: str = "") -> int:
    """清理上次崩溃留下的孤儿浏览器进程(启动时的兜底)。"""
    targets = find_orphan_processes()
    if not targets:
        return 0
    if os.name != "nt":  # pragma: no cover
        for pid, _name in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        return len(targets)

    killed = 0
    for pid, _name in targets:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
            killed += 1
        except (OSError, subprocess.SubprocessError):
            continue
    if killed:
        logger.info(f"已清理 {killed} 个孤儿浏览器进程{'(' + reason + ')' if reason else ''}")
    return killed


def find_our_processes(*, root_pid: Optional[int] = None) -> list[tuple[int, str]]:
    """找出属于本框架的浏览器/驱动进程。返回 ``[(pid, name)]``。

    ``root_pid``: 只保留"进程链上挂着这个 pid"的那些进程。**同时跑多个实例时必须用它** ——
    否则一个实例关闭会把另一个实例的浏览器也杀掉。实测踩过: 验收脚本起了个临时服务并调
    ``/api/shutdown``, 结果把正在用的那个服务的浏览器一起清掉了, 后续 UI 用例随即失败。

    不传 ``root_pid`` 时退化为"所有属于本框架的进程"(仅用于启动/退出时的兜底清扫)。
    """
    me = os.getpid()
    entries: dict[int, tuple[int, str, str]] = {}
    for pid, name, cmd, ppid in _iter_candidates():
        entries[pid] = (ppid, name, cmd)

    found: list[tuple[int, str]] = []
    for pid, (ppid, name, cmd) in entries.items():
        if pid == me:
            continue
        if name.lower() not in _PROCESS_NAMES:
            continue
        if not _looks_like_ours(cmd):
            continue
        if root_pid is None:
            found.append((pid, name))
            continue
        # 传 live=set(): 只关心"能否走到 root_pid", 不关心祖先是否已退出
        owned, _orphan = _trace_owner(ppid, entries, set(), root_pid=root_pid)
        if owned:
            found.append((pid, name))
    return found


def kill_our_processes(*, reason: str = "", root_pid: Optional[int] = None) -> int:
    """结束属于本框架的浏览器/驱动进程, 返回结束的进程数。

    先温和终止, 再对残留的强制结束 —— 浏览器有时会忽略第一次请求。
    ``root_pid`` 的语义见 :func:`find_our_processes`。
    """
    targets = find_our_processes(root_pid=root_pid)
    # 把"谁在什么范围下清了哪些进程"记进日志。排障时这一行能直接指出越界的那次调用 ——
    # 没有它就只能猜, 而这类"关一个实例顺手杀了另一个"的问题极难靠猜定位。
    logger.debug(
        f"kill_our_processes(reason={reason!r}, root_pid={root_pid}, "
        f"caller_pid={os.getpid()}) 选中 {len(targets)} 个: "
        f"{[f'{n}#{p}' for p, n in targets][:8]}"
    )
    if not targets:
        return 0

    if os.name != "nt":
        for pid, _name in targets:  # pragma: no cover
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        return len(targets)

    killed = 0
    for pid, name in targets:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            killed += 1
        except (OSError, subprocess.SubprocessError):
            continue

    # 给它们一点时间退出, 再对没走掉的强制清掉
    leftover = find_our_processes(root_pid=root_pid)
    for pid, _name in leftover:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue

    if killed:
        logger.info(f"已结束 {killed} 个框架子进程{'(' + reason + ')' if reason else ''}")
    return killed


# ---------------------------------------------------------------------------
# 注册收尾钩子
# ---------------------------------------------------------------------------
_hooks: list[Callable[[], Any]] = []
_registered = False


def register_cleanup(hook: Callable[[], Any]) -> None:
    """登记一个收尾回调(例如"关掉服务实例")。"""
    if hook not in _hooks:
        _hooks.append(hook)


def _run_hooks(reason: str) -> None:
    for hook in list(_hooks):
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - 收尾阶段的异常不该再抛出去
            logger.debug(f"收尾回调失败({reason}): {exc}")


def install_shutdown_cleanup(*, on_close: Optional[Callable[[], Any]] = None) -> None:
    """装好"进程退出就清理"的三道保险。

    只装一次。三道保险覆盖不同退出方式, 缺一不可:

    1. ``atexit`` —— 正常 return / ``sys.exit``; 最干净的一条路;
    2. ``SIGINT`` / ``SIGTERM`` / ``SIGBREAK`` —— Ctrl+C、被 ``kill``、父进程请求终止;
    3. ``SIGBREAK``(仅 Windows) —— 控制台窗口上的"关闭"按钮与 Ctrl+Break。

    **清理范围限定为"自己这一支"** (``root_pid=os.getpid()``)。这一点很容易写错:
    最早这里是无差别清理"所有属于本框架的进程", 于是同一个项目下跑两个实例时,
    关掉其中一个会把另一个的浏览器也杀掉 —— 验收里真实复现过(临时服务退出后,
    主服务的 4 个浏览器全没了)。退出时清理的语义应该是"收拾自己的摊子", 不是"全清"。

    **诚实说明**: Windows 上用户直接点窗口右上角的 × 时, 系统给的是
    ``CTRL_CLOSE_EVENT``, 进程只有几秒钟, Python 不保证跑完清理。真正可靠的兜底是
    "下次启动时先清掉上一次的残留"(见 :func:`kill_our_processes` 在启动时也被调用),
    以及 ``start.bat`` 退出时执行清理。所以这里三道保险 + 启动兜底一起上, 而不是
    只赌某一种。
    """
    global _registered
    if on_close is not None:
        register_cleanup(on_close)
    if _registered:
        return
    _registered = True

    def _scope() -> int:
        return os.getpid()

    def _atexit_cleanup() -> None:
        kill_our_processes(reason="atexit", root_pid=_scope())
        _run_hooks("atexit")

    atexit.register(_atexit_cleanup)

    def _handler(signum: int, _frame: Any) -> None:  # pragma: no cover - 依赖信号
        logger.info(f"收到信号 {signum}, 正在清理并退出…")
        _run_hooks(f"signal {signum}")
        kill_our_processes(reason=f"signal {signum}", root_pid=_scope())
        # 恢复默认处理并重发, 让退出码保持"被信号终止"的语义
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:  # noqa: BLE001
            sys.exit(0)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):  # pragma: no cover - 非主线程等情况
            continue


class ShutdownController:
    """让服务能被"从界面上关掉"。

    uvicorn 的 ``Server`` 实例在启动时注册进来, 之后任意线程都能请求它优雅退出 ——
    界面上的"关闭服务"按钮走的就是这条路, 比让用户去任务管理器杀进程友好得多。
    """

    def __init__(self) -> None:
        self._server: Any = None
        self._loop: Any = None
        self._requested = threading.Event()

    def bind(self, server: Any, loop: Any) -> None:
        self._server = server
        self._loop = loop

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def request(self) -> bool:
        """请求服务优雅退出。返回是否真的提交了请求。"""
        if self._requested.is_set():
            return False
        self._requested.set()
        if self._server is None or self._loop is None:
            # 还没绑定(极早期就被请求): 直接结束进程, 但先做清理
            _run_hooks("shutdown-early")
            kill_our_processes(reason="shutdown-early")
            threading.Timer(0.2, lambda: os._exit(0)).start()
            return True

        def _stop() -> None:
            self._server.should_exit = True

        try:
            self._loop.call_soon_threadsafe(_stop)
            return True
        except RuntimeError:
            return False


#: 全局单例: web 服务与路由共享同一个控制器
shutdown_controller = ShutdownController()

__all__ = [
    "install_shutdown_cleanup",
    "kill_our_processes",
    "kill_orphan_processes",
    "find_our_processes",
    "find_orphan_processes",
    "register_cleanup",
    "shutdown_controller",
    "ShutdownController",
]
