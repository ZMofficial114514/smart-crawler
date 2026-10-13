"""
校验 README 里的脚本数量与仓库实际一致。

为什么需要: README 的目录树里写着"共 N 个脚本", 而这个 N 是手写的 —— 加一个脚本它就过期,
而且**没有任何东西会提醒你**。实测它就漂过一次: 某处写 49(43 verify + 6 check),
而当时仓库里实际是 43 verify + 7 check = 50。

更麻烦的是: 这个数字还被渲染进了介绍视频的字幕里。数字一过期, 视频里的话就成了错的,
而视频不像 README 那样改一行就行 —— 得整体重渲染。所以这个校验的价值不只是"文档整齐"。

它检查 README 里所有形如「共 N 个脚本」「N 个 verify」「N 个 check」的表述, 与实测对比。
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
READMES = [ROOT / "README.md", ROOT / "scripts" / "intro_video" / "README.md"]


def real_counts() -> dict[str, int]:
    """统计 scripts/ **顶层**的脚本。

    **只数顶层**: README 目录树里写的 "共 N 个脚本" 指的是这一层。
    我第一版用了 ``SCRIPTS.glob``, 结果把 ``scripts/intro_video/*.py`` 那些子目录脚本也数进来,
    连"校验脚本自己是否被算进总数"都成了问题 —— 数字每次加文件都跳, 校验永远报错。
    """
    py = [p for p in SCRIPTS.iterdir() if p.is_file() and p.suffix == ".py"]
    return {
        "total": len(py),
        "verify": sum(1 for p in py if p.name.startswith("verify_")),
        "check": sum(1 for p in py if p.name.startswith("check_")),
    }


#: 形如「共 49 个脚本」/「N 个 verify」/「N 个 check」/「N 个 verify 脚本」
PATTERNS = [
    (re.compile(r"共\s*(\d+)\s*个脚本"), "total", "共 N 个脚本"),
    (re.compile(r"(\d+)\s*个\s*verify"), "verify", "N 个 verify"),
    (re.compile(r"(\d+)\s*个\s*check"), "check", "N 个 check"),
]


def main() -> int:
    counts = real_counts()
    print(f"  实测: 脚本总数 {counts['total']}, "
          f"verify {counts['verify']}, check {counts['check']}")

    problems: list[str] = []
    found = 0
    for path in READMES:
        if not path.exists():
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            for pat, key, label in PATTERNS:
                for m in pat.finditer(line):
                    found += 1
                    claimed = int(m.group(1))
                    if claimed != counts[key]:
                        rel = path.relative_to(ROOT).as_posix()
                        problems.append(
                            f"{rel}:{lineno} 写的是「{label} = {claimed}」, "
                            f"实际 {counts[key]}  ({m.group(0)})")

    print(f"  检查了 {found} 处数量表述")
    if problems:
        print(f"\n  ✗ 有 {len(problems)} 处与实际不符:")
        for x in problems:
            print(f"      {x}")
        print("\n  脚本数量校验: 未通过 ✗")
        print("  提示: 改了脚本就同步 README; 若这个数字进了视频字幕, 也要重渲染。")
        return 1

    print("\n  脚本数量校验: 通过 ✓(README 里的数量与仓库一致)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
