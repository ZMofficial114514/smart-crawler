"""版本一致性自检: 包版本、pyproject、Web 接口三处必须一致。"""

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import smartcrawler  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


pkg_version = smartcrawler.__version__
print(f"包版本 smartcrawler.__version__ = {pkg_version}\n")

pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
check(
    pyproject["project"]["version"] == pkg_version,
    "pyproject.toml 与包版本一致",
    f"{pyproject['project']['version']} vs {pkg_version}",
)

# 代码里不应再有硬编码的版本号(排除包根的唯一定义)
hardcoded: list[str] = []
pattern = re.compile(r"""["']([0-9]+\.[0-9]+\.[0-9]+)["']""")
skip_parts = (".venv", ".browsers", "__pycache__", ".runtmp", "node_modules", ".piptmp", "data", "logs")
for path in Path("smartcrawler").rglob("*.py"):
    if any(part in str(path) for part in skip_parts):
        continue
    if path.name == "__init__.py":
        continue
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # 只看像"版本赋值"的行, 忽略插件自身的 version="1.0.0"
        if "version" in stripped.lower() and pattern.search(stripped) and "plugin" not in str(path):
            hardcoded.append(f"{path}:{i}: {stripped[:80]}")

check(not hardcoded, "Web 层没有硬编码版本号", "; ".join(hardcoded[:3]))

# Web 层能正常导入并报告同一版本
from smartcrawler.web.service import CrawlService  # noqa: E402

source = Path("smartcrawler/web/service.py").read_text(encoding="utf-8")
check(
    "from .. import __version__" in source,
    "service.py 从包根导入版本",
)
check("__version__" in Path("smartcrawler/web/api.py").read_text(encoding="utf-8"),
      "api.py 从包根导入版本")

print("\n" + "=" * 56)
print("版本一致性: " + ("未通过 ✗" if failures else f"通过 ✓ (包/打包/接口 均为 {pkg_version})"))
print("=" * 56)
sys.exit(1 if failures else 0)
