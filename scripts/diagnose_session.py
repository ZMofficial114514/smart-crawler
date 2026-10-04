"""
诊断: pixiv 会话为什么会"登录无效"。

先做静态检查(不联网), 只看证据:
  1. 会话文件里到底存了哪些域名的 Cookie —— 是否被别的站点污染;
  2. pixiv 的关键登录 Cookie 是否在、是否已过期;
  3. User-Agent 在"保存时"与"恢复时"是否一致(storage_state 里根本没有 UA 字段)。
"""

import json
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

SESSION = pathlib.Path("data/session.json")

#: pixiv 维持登录态必需的 Cookie。PHPSESSID 是会话主键; device_token 是设备信任令牌,
#: 它们同时构成"这台设备"的身份, 换浏览器指纹极易被判为无效会话。
PIXIV_KEY_COOKIES = ("PHPSESSID", "device_token", "p_ab_id", "p_ab_id_2", "login_ever")

if not SESSION.exists():
    print(f"  {SESSION} 不存在 —— 还没有保存过任何登录会话")
    sys.exit(1)

state = json.loads(SESSION.read_text(encoding="utf-8"))
cookies = state.get("cookies") or []
origins = state.get("origins") or []

print(f"  会话文件: {SESSION}  ({SESSION.stat().st_size} 字节)")
print(f"  Cookie 总数 = {len(cookies)}   localStorage 站点数 = {len(origins)}")
print(f"  顶层键 = {sorted(state.keys())}")

# ---------- 1. 域名分布: 是否被别的站点污染 ----------
from collections import Counter

by_domain = Counter(c.get("domain", "?") for c in cookies)
print(f"\n  === Cookie 域名分布(共 {len(by_domain)} 个域名) ===")
for dom, n in by_domain.most_common(15):
    flag = "  <== pixiv" if "pixiv" in dom else ""
    print(f"    {dom:<28} {n:>4}{flag}")

pixiv = [c for c in cookies if "pixiv" in (c.get("domain") or "")]
other = len(cookies) - len(pixiv)
print(f"\n  pixiv Cookie = {len(pixiv)} 个")
print(f"  其它站点 Cookie = {other} 个  ({other / max(len(cookies),1) * 100:.0f}%)")

# ---------- 2. pixiv 关键 Cookie ----------
print("\n  === pixiv 关键登录 Cookie ===")
now = time.time()
names = {c.get("name") for c in pixiv}
for want in PIXIV_KEY_COOKIES:
    hit = next((c for c in pixiv if c.get("name") == want), None)
    if hit is None:
        print(f"    {want:<16} 缺失")
        continue
    exp = hit.get("expires")
    if exp in (None, -1, 0):
        life = "会话级(关浏览器即失效)"
    else:
        left = exp - now
        life = f"剩余 {left/86400:.1f} 天" if left > 0 else f"**已过期 {-left/3600:.1f} 小时**"
    print(f"    {want:<16} 存在   {life}")

# ---------- 3. UA 一致性 ----------
print("\n  === User-Agent 一致性(这是最可疑的一点) ===")
print(f"    storage_state 是否记录了 UA: {'有' if any('user_agent' in k.lower() or 'ua' == k.lower() for k in state) else '没有'}")

from smartcrawler.anti_spider import random_user_agent

print("    每次启动浏览器时, 代码会重新随机一个 UA:")
seen = {}
for _ in range(6):
    ua = random_user_agent()
    # 抽取"浏览器+系统"特征, 便于看出是否真的在换身份
    key = "Firefox" if "Firefox" in ua else ("Chrome" if "Chrome" in ua else "其它")
    os_key = ("Mac" if "Macintosh" in ua else
              "Linux" if "Linux" in ua else
              "Win" if "Windows" in ua else "?")
    seen.setdefault(f"{key}/{os_key}", 0)
    seen[f"{key}/{os_key}"] += 1
print(f"    6 次抽样得到的身份组合: {seen}")
print(f"    不同身份数量 = {len(seen)}  -> " +
      ("**每次都在换浏览器身份**" if len(seen) > 1 else "身份稳定"))

print("\n  === 结论 ===")
verdict = []
if other > len(pixiv):
    verdict.append(f"会话文件被其它站点污染({other}/{len(cookies)} 是别的站)")
if "PHPSESSID" not in names:
    verdict.append("pixiv 的 PHPSESSID 不在会话里(等于没登录)")
if len(seen) > 1:
    verdict.append("每次启动随机换 UA —— 登录时与恢复时是**两个不同的浏览器身份**")
if not verdict:
    verdict.append("静态检查未发现明显问题, 需要联网实测")
for v in verdict:
    print(f"    - {v}")
