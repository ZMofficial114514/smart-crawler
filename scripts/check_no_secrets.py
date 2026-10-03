"""
密钥泄露排查 —— 确认用户的真实 API Key **不会出现在任何会被提交的文件里**。

做法: 先从 .env 读出真实 key, 然后在全项目里搜它(以及它的前缀/片段)。
只要有任何一处出现在未被 .gitignore 排除的文件里, 就判定为泄露。
"""

import pathlib
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(".").resolve()
ENV = ROOT / ".env"

# ---------- 1. 读出真实 key(只用于比对, 不打印完整值) ----------
real_keys: list[str] = []
if ENV.exists():
    for line in ENV.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value and re.search(r"KEY|TOKEN|SECRET|PASSWORD", key, re.I):
            real_keys.append(value)

if not real_keys:
    print("  .env 里没有找到密钥类变量")
else:
    for k in real_keys:
        print(f"  发现密钥变量, 长度 {len(k)}, 前 6 位 {k[:6]}…后 4 位 …{k[-4:]}")

# ---------- 2. 找出会被 git 跟踪的文件 ----------
SKIP_DIRS = {".venv", ".git", ".browsers", ".runtmp", "__pycache__", "node_modules",
             "data", "logs", ".piptmp", ".pytest_cache", ".ruff_cache"}

def tracked_files():
    """优先问 git; 还没 init 就自己按 .gitignore 的常识排除。"""
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30, check=False)
        files = [f for f in (out.stdout or "").splitlines() if f.strip()]
        if files:
            return [ROOT / f for f in files], True
    except Exception:
        pass
    result = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name == ".env" or p.suffix in {".pyc", ".log"}:
            continue
        result.append(p)
    return result, False

files, via_git = tracked_files()
print(f"\n  待检查文件 {len(files)} 个(来源: {'git ls-files' if via_git else '目录扫描'})")

# ---------- 3. 搜索密钥 ----------
leaks: list[tuple[str, str]] = []
for path in files:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".db", ".sqlite"}:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for key in real_keys:
        if key in text:
            leaks.append((str(path.relative_to(ROOT)), "完整密钥"))
            break
        # 片段比对: 前缀 12 位足以确认是同一个 key
        if len(key) >= 12 and key[:12] in text:
            leaks.append((str(path.relative_to(ROOT)), f"密钥片段 {key[:8]}…"))
            break

print("\n  === 结果 ===")
if leaks:
    for f, why in leaks:
        print(f"  ✗ 泄露: {f}  ({why})")
else:
    print("  ✓ **没有任何会被提交的文件包含你的密钥**")

# ---------- 4. 检查 .gitignore 是否真的排除 .env ----------
print("\n  === .gitignore 覆盖检查 ===")
gi = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".gitignore").exists() else ""
for pattern, label in ((".env", ".env(真实密钥)"), ("data/", "data/(会话与产物)"),
                       ("data/session.json", "登录会话"), (".venv/", "虚拟环境")):
    ok = pattern in gi
    print(f"  {'✓' if ok else '✗'} {label} 被 .gitignore 排除" + ("" if ok else "  <== 需要补上!"))

# ---------- 5. 用 git check-ignore 实测(如果已是 git 仓库) ----------
if (ROOT / ".git").exists():
    print("\n  === git check-ignore 实测 ===")
    for candidate in (".env", "data/session.json", "aiAPI.py", "README.md"):
        out = subprocess.run(["git", "check-ignore", "-q", candidate],
                             capture_output=True, text=True, check=False)
        ignored = out.returncode == 0
        print(f"  {'被忽略' if ignored else '会入库'}  {candidate}")
else:
    print("\n  (还不是 git 仓库, 建仓后再用 git check-ignore 复核)")

sys.exit(1 if leaks else 0)
