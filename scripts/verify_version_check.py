"""自检: 证明版本一致性检查真的能发现问题(注入缺陷 -> 检出 -> 恢复)。

"检查器本身也要被验证" —— 一个永远说 OK 的检查器毫无价值。这里故意把 pyproject 的
版本改错, 确认 check_version.py 会失败, 然后恢复并确认它重新通过。
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
BACKUP = ROOT / ".runtmp" / "_pyproject_backup.toml"
CHECKER = ROOT / "scripts" / "check_version.py"
PYTHON = sys.executable


def run_checker() -> tuple[int, str]:
    result = subprocess.run(
        [PYTHON, str(CHECKER)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main() -> int:
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    original = PYPROJECT.read_text(encoding="utf-8")
    shutil.copy(PYPROJECT, BACKUP)

    failures: list[str] = []
    try:
        # ---- 1) 正常状态应通过 ----
        code, out = run_checker()
        ok_before = code == 0
        print(f"  1) 未注入时: {'✓ 通过' if ok_before else '✗ 意外失败'}")
        if not ok_before:
            failures.append("注入前检查器就失败了")
            print(out[-400:])

        # ---- 2) 注入缺陷后应失败 ----
        # **从当前 pyproject 里读出真实版本再篡改**, 不要写死版本号 ——
        # 写死的话每次升版本这个自检都会失效(报"没找到目标字符串"),
        # 而它失效的样子和"检查器真的坏了"很难区分。
        import re

        match = re.search(r'^version\s*=\s*"([^"]+)"', original, flags=re.MULTILINE)
        if not match:
            failures.append("pyproject 里没有 version 字段")
            print("  ✗ 未能注入缺陷: pyproject 里找不到 version = \"...\"")
        else:
            current = match.group(1)
            fake = "9.9.9" if current != "9.9.9" else "8.8.8"
            broken = original[: match.start(1)] + fake + original[match.end(1) :]
            PYPROJECT.write_text(broken, encoding="utf-8")
            code2, out2 = run_checker()
            detected = code2 != 0
            print(f"  2) 注入 pyproject={fake}(真实为 {current}) 后: "
                  f"{'✓ 已检出' if detected else '✗ 漏检!'}")
            if not detected:
                failures.append("检查器漏检了版本不一致")
            else:
                for line in out2.splitlines():
                    if "✗" in line:
                        print(f"       {line.strip()}")

        # ---- 3) 注入硬编码版本也应失败 ----
        PYPROJECT.write_text(original, encoding="utf-8")
        service = ROOT / "smartcrawler" / "web" / "service.py"
        service_orig = service.read_text(encoding="utf-8")
        shutil.copy(service, ROOT / ".runtmp" / "_service_backup.py")
        service.write_text(
            service_orig.replace('"version": __version__,', '"version": "9.9.9",', 1),
            encoding="utf-8",
        )
        code3, out3 = run_checker()
        detected3 = code3 != 0
        print(f"  3) 注入 service.py 硬编码 9.9.9 后: {'✓ 已检出' if detected3 else '✗ 漏检!'}")
        if not detected3:
            failures.append("检查器漏检了硬编码版本")
        service.write_text(service_orig, encoding="utf-8")

    finally:
        PYPROJECT.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)
        for leftover in (ROOT / ".runtmp" / "_service_backup.py",):
            leftover.unlink(missing_ok=True)

    # ---- 4) 恢复后应重新通过 ----
    code4, _ = run_checker()
    print(f"  4) 恢复后: {'✓ 重新通过' if code4 == 0 else '✗ 仍然失败'}")
    if code4 != 0:
        failures.append("恢复后检查器仍然失败(可能没还原干净)")

    print("\n" + "=" * 60)
    if failures:
        print("检查器自检: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("检查器自检: 通过 ✓ (注入缺陷能检出, 恢复后能重新通过)")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
