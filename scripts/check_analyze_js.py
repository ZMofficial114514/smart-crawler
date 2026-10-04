"""校验 structure.py 里的 _ANALYZE_JS 语法(用 node), 避免运行期才发现写错。"""

import pathlib
import subprocess
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.structure import _ANALYZE_JS  # noqa: E402

out = pathlib.Path(".runtmp/_analyze_check.js")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    "const f = " + _ANALYZE_JS.strip() + ";\n"
    "if (typeof f !== 'function') { throw new Error('not a function'); }\n"
    "console.log('JS OK: ANALYZE_JS 是函数, 长度 ' + f.toString().length);\n",
    encoding="utf-8",
)
print(f"  已导出 {out} ({len(_ANALYZE_JS)} 字符)")

node = next(pathlib.Path(".venv").rglob("node.exe"), None)
if node is None:
    print("  未找到 node, 跳过")
    sys.exit(0)

r = subprocess.run([str(node), str(out)], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", check=False)
print(f"  node exit={r.returncode}")
print(f"  stdout: {(r.stdout or '').strip()[:200]}")
if r.returncode != 0:
    print(f"  stderr: {(r.stderr or '').strip()[:600]}")
sys.exit(r.returncode)
