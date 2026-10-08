"""扫描**整个 git 历史**, 确认没有任何提交泄露过 API 密钥。

为什么不能只看当前工作区: 密钥一旦进过历史, 就算后来删掉, 它仍然留在 git 对象里,
任何人 clone 之后都能翻出来。所以必须逐提交扫。
"""

import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: 常见密钥形态。只报前 9 位与后 4 位, 绝不打印完整值。
PATTERNS = [
    ("OpenAI/DeepSeek 风格 sk-", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("Google API key", re.compile(r"AIza[A-Za-z0-9_\-]{30,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

#: 已知的占位/示例值, 不算泄露
ALLOW = re.compile(
    r"sk-(?:xxx|your|test|demo|fake|placeholder|abcd|1234)|"
    r"your[_-]?api[_-]?key|API_KEY_HERE|sk-\.\.\.",
    re.I,
)


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout


def main() -> int:
    revs = git("rev-list", "--all").split()
    print(f"  扫描 {len(revs)} 个提交的全部文件内容…")

    hits: dict[tuple[str, str], list[str]] = {}
    checked = 0
    for rev in revs:
        # 列出该提交的所有 blob 及其路径
        out = git("ls-tree", "-r", "-z", rev)
        for entry in out.split("\0"):
            if not entry.strip():
                continue
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue
            blob = parts[2]
            checked += 1
            content = git("cat-file", "-p", blob)
            if not content:
                continue
            for label, pat in PATTERNS:
                for m in pat.finditer(content):
                    value = m.group(0)
                    if ALLOW.search(value):
                        continue
                    masked = f"{value[:9]}…{value[-4:]}"
                    hits.setdefault((path, label), []).append(
                        f"{rev[:8]} {masked} (len={len(value)})")

    print(f"  共检查 {checked} 个文件版本")
    if not hits:
        print("\n  ✓ **整个 git 历史里没有任何密钥泄露**")
        return 0

    print(f"\n  ✗ 发现 {len(hits)} 处疑似泄露:")
    for (path, label), items in sorted(hits.items()):
        print(f"\n    {path}  [{label}]")
        for it in items[:6]:
            print(f"        {it}")
        if len(items) > 6:
            print(f"        … 另有 {len(items) - 6} 处")
    return 1


if __name__ == "__main__":
    sys.exit(main())
