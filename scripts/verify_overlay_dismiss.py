"""
"登录浮层挡住了可抓内容"的回归验收(用户报障: 检测到登录框就终止工作)。

真相是: 很多站点(堆糖典型)弹一个登录遮罩, 但**内容其实已经在页面里**, 只是被盖住。
实测 duitang 搜索页: 遮罩在时 55 张图 / 230 链接, 叉掉后一模一样 —— 内容一条没少。

所以正确的行为是:
  - **有内容** → 关掉遮罩后照常提取, 不要因为"看到密码框"就放弃;
  - **真没内容**(纯登录页) → 该跳过还是跳过, 不要抓一堆页脚链接当数据。

这两条都要测 —— 只放松不收紧会让洛谷那种错误页又开始产出假数据。

用法: python scripts/verify_overlay_dismiss.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# A: 登录遮罩 + **下面有真内容**(复刻 duitang)
OVERLAY_WITH_CONTENT = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>搜索结果 - 图站</title></head><body>
<div class="feed">
  <div class="card"><a href="/detail/1"><img src="/img/1_thumb.jpg" data-src="/img/1.jpg" alt="图一"></a></div>
  <div class="card"><a href="/detail/2"><img src="/img/2_thumb.jpg" data-src="/img/2.jpg" alt="图二"></a></div>
  <div class="card"><a href="/detail/3"><img src="/img/3_thumb.jpg" data-src="/img/3.jpg" alt="图三"></a></div>
  <div class="card"><a href="/detail/4"><img src="/img/4_thumb.jpg" data-src="/img/4.jpg" alt="图四"></a></div>
  <div class="card"><a href="/detail/5"><img src="/img/5_thumb.jpg" data-src="/img/5.jpg" alt="图五"></a></div>
  <div class="card"><a href="/detail/6"><img src="/img/6_thumb.jpg" data-src="/img/6.jpg" alt="图六"></a></div>
</div>
<nav class="footer-nav">
  <a href="/about">关于</a><a href="/help">帮助</a><a href="/terms">条款</a><a href="/privacy">隐私</a>
  <a href="/contact">联系</a><a href="/jobs">招聘</a><a href="/blog">博客</a><a href="/app">App</a>
  <a href="/a">A</a><a href="/b">B</a><a href="/c">C</a><a href="/d">D</a>
  <a href="/e">E</a><a href="/f">F</a><a href="/g">G</a><a href="/h">H</a>
  <a href="/i">I</a><a href="/j">J</a><a href="/k">K</a><a href="/l">L</a>
</nav>
<!-- 登录遮罩: 盖在上面, 但内容都在 -->
<div class="mask-body" style="position:fixed;inset:0;background:rgba(0,0,0,.5)"></div>
<div class="mask-cont login-mask" style="position:fixed;left:50%;top:40%;width:660px;height:460px;background:#fff">
  <div class="mask-close" style="width:24px;height:24px">×</div>
  <form action="/login" method="post">
    <input type="text" name="phone" placeholder="手机号">
    <input type="password" name="pwd" placeholder="密码">
    <button type="submit">登录</button>
  </form>
  <p>手机号登录 获取验证码 记住账号 忘记密码</p>
</div>
<script>
document.querySelector('.mask-close').addEventListener('click', () => {
  document.querySelector('.mask-body').remove();
  document.querySelector('.mask-cont').remove();
});
</script>
</body></html>"""

# B: 纯登录页(没有任何内容) —— 该跳过提取
LOGIN_ONLY = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>请先登录</title></head><body>
<h1>请先登录后查看</h1>
<form action="/login" method="post">
  <input type="text" name="u"><input type="password" name="p"><button>登录</button>
</form>
</body></html>"""


class Site:
    def __init__(self) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/robots.txt":
                    body = b"User-agent: *\nDisallow:\n"
                elif path.startswith("/overlay"):
                    body = OVERLAY_WITH_CONTENT.encode("utf-8")
                else:
                    body = LOGIN_ONLY.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # noqa: A002
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


async def crawl(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.post(
        "/api/crawl", json={"url": url, "goal": "抓取所有图片与标题", "max_pages": 1}
    )
    if r.status_code not in (200, 202):
        print(f"    提交失败 HTTP {r.status_code}: {r.text[:150]}")
        return None
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(90):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return d.get("result") or {}
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site()
    print(f"靶站: {site.base}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=180.0) as client:
            # ==============================================================
            print("=== 1) 登录遮罩 + 下面有真内容: 应关掉遮罩并正常抓取 ===")
            res = await crawl(client, f"{site.base}/overlay")
            check(res is not None, "任务完成")
            if res:
                ov = res.get("overlays") or {}
                print(f"    overlays: {ov.get('summary')}")
                print(f"    item_count = {res.get('item_count')}")
                check(ov.get("dismissed") is True, "**自动关掉了登录遮罩**",
                      str(ov.get("summary")))
                check(ov.get("overlays_after", 1) == 0, "遮罩已被清除",
                      f"剩余 {ov.get('overlays_after')}")
                check((res.get("item_count") or 0) > 0,
                      "**抓到了内容(没有因为登录框而终止)**",
                      f"{res.get('item_count')} 条")
                # 抓到的应该是卡片, 而不是页脚导航
                items = res.get("items") or []
                first = items[0] if items else {}
                print(f"    首条: {str(first)[:120]}")
                check("关于" not in str(first) and "帮助" not in str(first),
                      "抓到的是内容卡片, 不是页脚导航")

            # ==============================================================
            print("\n=== 2) 纯登录页(真没内容): 仍应跳过提取 ===")
            res2 = await crawl(client, f"{site.base}/login-only")
            check(res2 is not None, "任务完成")
            if res2:
                issue = res2.get("access_issue") or {}
                print(f"    access_issue={issue.get('issue_type')} "
                      f"item_count={res2.get('item_count')}")
                print(f"    errors={(res2.get('errors') or [])[:1]}")
                check((res2.get("item_count") or 0) == 0,
                      "**没有把登录页的元素当成数据**",
                      f"{res2.get('item_count')} 条")
                check(
                    any("跳过提取" in str(e) for e in (res2.get("errors") or [])),
                    "给出了『已跳过提取』的说明",
                    str((res2.get("errors") or [])[:1]),
                )

    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"登录浮层处理验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"登录浮层处理验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
