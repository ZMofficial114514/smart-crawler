"""自检: 往文档里注入一个错误签名, 看 check_plugin_docs.py 能不能抓到。

"检查器永远通过"和"检查器真的在工作"是两回事 —— 所以必须注入缺陷证明它有效。
"""

import pathlib
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DOC = pathlib.Path("docs/plugins.md")
original = DOC.read_text(encoding="utf-8")

BAD = (
    "```python\n"
    "def on_page(self, ctx, page_no):\n"
    "    pass\n"
    "```\n\n"
)
MARK = "## 2. 三条硬性准则"

results = []
try:
    # ---- 1) 未注入时: 应通过 ----
    code = subprocess.run(
        [sys.executable, "scripts/check_plugin_docs.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    ok_before = code.returncode == 0
    results.append(("注入前应通过", ok_before, code.stdout.strip().splitlines()[-2] if code.stdout else ""))

    # ---- 2) 注入错误签名: 应失败 ----
    assert MARK in original, "找不到注入锚点"
    DOC.write_text(original.replace(MARK, BAD + MARK, 1), encoding="utf-8")
    code2 = subprocess.run(
        [sys.executable, "scripts/check_plugin_docs.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    detected = code2.returncode != 0 and "on_page 的参数个数正确" in code2.stdout
    detail = next((l.strip() for l in code2.stdout.splitlines() if "on_page 的参数个数" in l), "")
    results.append(("注入错误签名后应被检出", detected, detail))
finally:
    DOC.write_text(original, encoding="utf-8")

# ---- 3) 恢复后应重新通过 ----
code3 = subprocess.run(
    [sys.executable, "scripts/check_plugin_docs.py"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
)
results.append(("恢复后应重新通过", code3.returncode == 0,
                code3.stdout.strip().splitlines()[-2] if code3.stdout else ""))

print()
for label, ok, detail in results:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail[:90]}" if detail else ""))

failed = [r for r in results if not r[1]]
print("\n" + "=" * 58)
print(f"文档检查器自检: {'通过 ✓' if not failed else '未通过 ✗'}")
print("=" * 58)
sys.exit(1 if failed else 0)
