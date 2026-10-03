"""
验收: "关闭服务"真的能关掉, 而且会把框架子进程一起带走。

用户要求: 关掉命令行之后, 爬虫框架要自动关闭(不能留下浏览器在后台跑)。

这里用一个**独立端口**起一个临时服务来验, 不动正在用的那个:
  1. 启动服务 -> 让它拉一个浏览器出来(否则没有子进程可清);
  2. 调 /api/shutdown -> 服务应真的退出, 且框架浏览器进程数归零;
  3. 再验"进程检测"本身是精确的: 不能把用户自己的 Chrome / 其它 node 进程算进来。

用法: python scripts/verify_shutdown.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

from smartcrawler.runtime import (  # noqa: E402
    find_our_processes,
    kill_orphan_processes,
)

PORT = 8399
BASE = f"http://127.0.0.1:{PORT}"
#: 如果这个端口上有另一个服务在跑, 就顺便验证"不会误伤它"(见第 6 节)
SURVIVOR_PORT = 8322
failures: list[str] = []


def survivor_browser_count() -> int:
    """数一下"不属于本脚本"的框架浏览器进程有多少个。"""
    import urllib.request as _u

    try:
        with _u.urlopen(f"http://127.0.0.1:{SURVIVOR_PORT}/api/health", timeout=3) as resp:
            if resp.status != 200:
                return -1
    except Exception:  # noqa: BLE001
        return -1
    return len(find_our_processes())


def survivor_analyze(timeout: float = 90.0) -> tuple[bool, str]:
    """让另一个实例跑一次分析, 用它**还能不能干活**判断有没有被误伤。"""
    import json as _json
    import urllib.request as _u

    data = _json.dumps({"url": "https://books.toscrape.com/", "scroll_rounds": 0}).encode()
    req = _u.Request(
        f"http://127.0.0.1:{SURVIVOR_PORT}/api/analyze", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _u.urlopen(req, timeout=25) as resp:
            task_id = (_json.loads(resp.read()).get("task") or {}).get("id")
    except Exception as exc:  # noqa: BLE001
        return False, f"提交失败 {type(exc).__name__}: {exc}"
    if not task_id:
        return False, "没拿到 task_id"

    end = time.time() + timeout
    while time.time() < end:
        time.sleep(3)
        try:
            with _u.urlopen(f"http://127.0.0.1:{SURVIVOR_PORT}/api/tasks/{task_id}", timeout=10) as resp:
                d = _json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            return False, f"查询失败 {type(exc).__name__}: {exc}"
        if d.get("status") in ("success", "failed", "cancelled"):
            title = ((d.get("result") or {}).get("report") or {}).get("title") or ""
            return d.get("status") == "success", f"status={d.get('status')} title={title[:28]!r}"
    return False, "超时"


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def wait_health(timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.8)
    return False


def post(path: str, timeout: float = 15.0) -> tuple[int, str]:
    req = urllib.request.Request(f"{BASE}{path}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 服务可能在响应前就退出了
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print(f"临时服务端口: {PORT}\n")

    # 先记录"另一个服务"的浏览器数量, 供第 6 节对比
    survivor_before = survivor_browser_count()
    survivor_expected = survivor_before > 0
    if survivor_expected:
        print(f"  检测到 {SURVIVOR_PORT} 端口上还有一个服务, 它有 {survivor_before} 个浏览器进程")
        print("  -> 第 6 节会验证本次关闭不会误伤它\n")

    # 清掉**上次本脚本留下的**残留。
    # ⚠️ 这里绝不能做全局清理: 那会把正在使用的另一个服务实例的浏览器一起杀掉,
    # 第 6 节正是要防这件事 —— 测量手段本身不能破坏被测量的对象。
    # 临时服务此刻还没启动, 所以最干净的做法是: 什么都不清, 直接开始。
    time.sleep(1)

    print("=== 1) 启动临时服务 ===")
    env = dict(os.environ)
    proc = subprocess.Popen(
        [sys.executable, "-m", "smartcrawler", "web", "--port", str(PORT)],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    up = wait_health()
    check(up, "临时服务已就绪", BASE)
    if not up:
        proc.kill()
        return 1

    # 让它真的拉一个浏览器出来 —— 没有子进程就测不出"清理"有没有用
    print("\n=== 2) 让服务拉起一个浏览器(制造待清理的子进程) ===")
    body = b'{"url": "https://books.toscrape.com/", "scroll_rounds": 0}'
    req = urllib.request.Request(
        f"{BASE}/api/analyze", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"    (启动分析请求失败, 不影响后续: {exc})")

    spawned = []
    for _ in range(40):
        time.sleep(1.5)
        spawned = find_our_processes()
        if spawned:
            break
    print(f"    检测到框架子进程: {len(spawned)} 个")
    for pid, name in spawned[:5]:
        print(f"      pid={pid} {name}")
    check(bool(spawned), "**服务确实拉起了浏览器子进程(有东西可清)**", f"{len(spawned)} 个")

    print("\n=== 3) 调 /api/shutdown ===")
    status, text = post("/api/shutdown")
    print(f"    HTTP {status}  {text[:150]}")

    # 服务应退出: 健康检查开始失败
    gone = False
    for _ in range(30):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2):
                continue
        except Exception:  # noqa: BLE001
            gone = True
            break
    check(gone, "**服务已停止(健康检查不再响应)**")

    # 进程本身也要退出
    exited = False
    for _ in range(20):
        if proc.poll() is not None:
            exited = True
            break
        time.sleep(0.5)
    check(exited, "**服务进程本身已退出**", f"退出码={proc.poll()}")
    if not exited:
        proc.kill()

    print("\n=== 4) 框架子进程是否被清理 ===")
    time.sleep(2)
    leftover = find_our_processes()
    print(f"    残留: {[f'{p}:{n}' for p, n in leftover]}")
    check(not leftover, "**浏览器子进程已随服务一起清理(没有留在后台)**",
          f"残留 {len(leftover)} 个")
    # 收尾也**不能全局清理**: 那会把正在使用的另一个实例的浏览器一起带走,
    # 后面的 UI 用例会跟着失败 —— 测量手段不能破坏被测对象。
    # 临时服务此刻已经退出, 所以它留下的只能是孤儿, 用 kill_orphan_processes 精确收尾。
    if leftover:
        kill_orphan_processes(reason="验收收尾")

    print("\n=== 5) 清理必须是精确的(不能误杀) ===")
    # 关键: 检测只按"项目内路径"匹配, 所以用户自己的 Chrome / 别的 node 不该被算进来
    all_procs = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process chrome,node -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, timeout=20, check=False,
    ).stdout.strip()
    ours = len(find_our_processes())
    print(f"    系统里的 chrome/node 进程数 ≈ {all_procs}, 被识别为『我们的』= {ours}")
    # 注意: 这里**可能不为 0** —— 如果另一个服务实例正在运行, 它有自己的浏览器,
    # 那是正常的, 不该算作"残留"。所以判定条件要排除掉"主服务仍活着"的情况。
    if survivor_expected:
        print(f"    (另一个服务实例仍在运行, 它的 {ours} 个浏览器属于正常存在, 不算残留)")
    else:
        check(ours == 0,
              "**本框架已无进程, 且不会把用户的 chrome/node 当成自己的**",
              f"识别到 {ours} 个")

    print("\n=== 6) 不能误伤**另一个**服务实例(功能判据) ===")
    # 这条来自真实故障: 早期实现是无差别清理"所有属于本框架的进程", 于是验收脚本起了个
    # 临时服务并调 /api/shutdown, 把**正在用的那个服务**的浏览器一起清掉了, 后续用例随即失败。
    #
    # **判据用"另一个实例还能不能干活", 不用"数进程"**: 实测发现 Chromium 会不断回收
    # 并重建自己的辅助进程(PID 一批批地换), 光看数量根本分不清"被杀了"和"自己换了"。
    # 功能是否正常才是用户真正在意的结论。
    if not survivor_expected:
        print("    (主服务未在运行, 跳过跨实例断言)")
    else:
        ok, detail = survivor_analyze()
        check(ok, "**关掉本服务后, 另一个实例依然能正常分析**", detail)

    print("\n" + "=" * 62)
    if failures:
        print(f"关闭服务验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("关闭服务验收: 通过 ✓")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
