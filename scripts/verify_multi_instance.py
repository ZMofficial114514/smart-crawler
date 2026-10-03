"""
跨实例验收: 关掉一个服务, **另一个服务必须还能正常干活**。

**为什么不用"数进程"来判定**: 先前的版本比较浏览器 PID 数量, 结果反复误判 ——
Chromium 会不断回收/重建它的辅助进程, PID 一批批地换(实测同一服务连续三次清理
选中了三组完全不同的 PID), 光看数量根本分不清"被杀了"和"自己换了"。

改用**功能判据**: 关掉临时服务后, 再让主服务跑一次分析。
它还能跑通 => 它的浏览器与资源都没被动过。这才是用户真正在意的结论。
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TMP_PORT = 8406
MAIN_PORT = 8322
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def wait_health(port: int, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.8)
    return False


def analyze(port: int, timeout: float = 90.0) -> tuple[bool, str]:
    """让指定端口的服务跑一次分析; 返回 (是否成功, 说明)。"""
    data = json.dumps({"url": "https://books.toscrape.com/", "scroll_rounds": 0}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/analyze", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            task_id = (json.loads(r.read()).get("task") or {}).get("id")
    except Exception as exc:
        return False, f"提交失败 {type(exc).__name__}: {exc}"
    if not task_id:
        return False, "没拿到 task_id"

    end = time.time() + timeout
    while time.time() < end:
        time.sleep(3)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tasks/{task_id}", timeout=10) as r:
                d = json.loads(r.read())
        except Exception as exc:
            return False, f"查询失败 {type(exc).__name__}: {exc}"
        if d.get("status") in ("success", "failed", "cancelled"):
            title = ((d.get("result") or {}).get("report") or {}).get("title") or ""
            return d.get("status") == "success", f"status={d.get('status')} title={title[:32]!r}"
    return False, "超时"


def main() -> int:
    if not wait_health(MAIN_PORT, 8):
        print(f"  主服务({MAIN_PORT})未运行, 无法验证")
        return 1

    print(f"=== 1) 主服务({MAIN_PORT})能干活吗 ===")
    ok, detail = analyze(MAIN_PORT)
    check(ok, "关闭别的服务之前, 主服务工作正常", detail)

    print(f"\n=== 2) 起一个临时服务({TMP_PORT})并让它也干活 ===")
    abs_py = str(Path(sys.executable))
    proc = subprocess.Popen(
        [abs_py, "-m", "smartcrawler", "web", "--port", str(TMP_PORT)],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    check(wait_health(TMP_PORT), "临时服务已就绪")
    ok_tmp, detail_tmp = analyze(TMP_PORT)
    check(ok_tmp, "临时服务也能干活", detail_tmp)

    print(f"\n=== 3) 关掉临时服务 ===")
    req = urllib.request.Request(f"http://127.0.0.1:{TMP_PORT}/api/shutdown", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read())
        check(r.status == 200, "shutdown 返回 200", str(body.get("message", ""))[:40])
    except Exception as exc:  # noqa: BLE001
        check(False, "shutdown 请求成功", f"{type(exc).__name__}: {exc}")
    time.sleep(5)

    gone = not wait_health(TMP_PORT, 6)
    check(gone, "临时服务确实停了")

    print(f"\n=== 4) 关键: 主服务**还能不能干活** ===")
    ok2, detail2 = analyze(MAIN_PORT)
    check(ok2, "**关掉另一个服务后, 主服务依然能正常分析**", detail2)

    if proc.poll() is None:
        proc.kill()

    print("\n" + "=" * 62)
    if failures:
        print(f"跨实例互不干扰验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("跨实例互不干扰验收: 通过 ✓")
        print("  (关掉一个实例, 另一个实例的功能完全不受影响)")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
