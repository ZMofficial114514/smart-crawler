"""
懒加载滚动的回归验收。

用户报的场景(pixiv 登录后首页): 分析拿到的是一条"看起来完整的空页面" ——
导航/侧边栏都在, 主内容区(作品网格)是空容器。实测证据: 走框架抓到的简化 DOM 树里
``/artworks/`` 链接 **0** 个、空容器 **118** 个; 而真实页面有 **70** 个作品链接。

根因: 页面懒加载, 没滚。本脚本用受控靶站固化两件事:
  1. **有上限**的懒加载 -> 滚动后被加载出来, 且 ``reason=settled``(滚完即止);
  2. **无上限**的无限流 -> 如实报告 ``infinite=True``(界面据此询问用户是否继续),
     而不是假装"已经抓全了"。

靶站刻意复刻 pixiv 的两个特征: 真正的滚动容器是**内部 div**(不是 body)、内容分批追加。

用法: python scripts/verify_lazy_load.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import sys
import threading
import time
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


# 复刻 pixiv: 侧边栏立即渲染(所以"看起来完整"), 主内容区懒加载且滚动容器是内部 div
TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>懒加载靶站</title>
<style>
  body {{ margin: 0; height: 100vh; overflow: hidden; }}
  #sidebar {{ position: fixed; left: 0; top: 0; width: 220px; height: 100vh;
              overflow-y: auto; background: #f2f2f2; }}
  #sidebar a {{ display: block; padding: 6px; }}
  #scroller {{ position: absolute; left: 220px; right: 0; top: 0; height: 100vh;
               overflow-y: auto; }}
  .work {{ height: 110px; border-bottom: 1px solid #ddd; }}
</style></head><body>
<div id="sidebar">
  <a href="/users/1">我的主页</a>
  <a href="/bookmarks">我的收藏</a>
  <a href="/settings">设置</a>
  <a href="/logout.php">退出登录</a>
</div>
<div id="scroller">
  <section class="contents"><div class="grid" id="grid"></div></section>
</div>
<script>
const MAX = {max_batches};
const SIZE = {size};
let batch = 0, loading = false;
const grid = document.getElementById('grid');
const scroller = document.getElementById('scroller');

function append() {{
  if (MAX > 0 && batch >= MAX) return false;
  batch++;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < SIZE; i++) {{
    const d = document.createElement('div');
    d.className = 'work';
    d.innerHTML = '<a href="/artworks/' + batch + '_' + i + '">作品 ' + batch + '-' + i + '</a>';
    frag.appendChild(d);
  }}
  grid.appendChild(frag);
  return true;
}}

// **首屏必须填满视口**, 否则不会出现滚动条, 页面自己的 scroll 监听永远不触发 ——
// 形成死锁: 内容少 -> 没滚动条 -> 不触发 scroll -> 内容不增加。
// 真实站点首屏都是填满的, 靶站必须复刻这一点, 否则测的是靶站的缺陷而不是框架行为。
let guard = 0;
while (scroller.scrollHeight <= scroller.clientHeight + 10 && guard++ < 50) {{
  if (!append()) break;
}}

scroller.addEventListener('scroll', () => {{
  if (loading) return;
  if (scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 200) return;
  loading = true;
  setTimeout(() => {{ append(); loading = false; }}, 100);
}});
</script></body></html>"""


class Site:
    def __init__(self, max_batches: int, size: int = 6) -> None:
        html = TEMPLATE.format(max_batches=max_batches, size=size).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"User-agent: *\nDisallow:\n" if self.path.startswith("/robots") else html
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


async def analyze(client: httpx.AsyncClient, url: str, deep_scroll: int = 0) -> dict | None:
    payload = {"url": url}
    if deep_scroll:
        payload["deep_scroll"] = deep_scroll
    r = await client.post("/api/analyze", json=payload)
    if r.status_code not in (200, 202):
        print(f"    启动失败 HTTP {r.status_code}: {r.text[:150]}")
        return None
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(90):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return (d.get("result") or {}).get("report")
    return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"目标服务: {base}\n")

    async with httpx.AsyncClient(base_url=base, timeout=180.0) as client:
        # ==============================================================
        print("=== 1) 有上限的懒加载: 滚动后应把内容加载出来 ===")
        site = Site(max_batches=5)
        try:
            report = await analyze(client, f"{site.base}/")
            check(report is not None, "分析完成")
            if report:
                lazy = report.get("lazy_load") or {}
                tree = report.get("simplified_tree") or ""
                artworks = tree.count("/artworks/")
                print(f"    lazy_load: {lazy.get('summary')}")
                print(f"    reason={lazy.get('reason')} rounds={lazy.get('rounds')} "
                      f"新增节点={lazy.get('node_gain')}")
                print(f"    树里的作品链接 = {artworks}")
                check(lazy.get("rounds", 0) > 0, "确实执行了滚动加载",
                      str(lazy.get("rounds")))
                check(lazy.get("node_gain", 0) > 0, "滚动把新内容加载了出来",
                      f"+{lazy.get('node_gain')} 节点")
                check(artworks > 0,
                      "**简化 DOM 树里能看到懒加载出来的作品链接**", f"{artworks} 个")
                check(lazy.get("infinite") is False,
                      "有上限时如实报告『已滚到底』(不谎报无限)",
                      str(lazy.get("reason")))
        finally:
            site.stop()

        # ==============================================================
        print("\n=== 2) 无上限(无限流): 应报告『仍在增长』供界面询问用户 ===")
        site2 = Site(max_batches=0)
        try:
            report2 = await analyze(client, f"{site2.base}/")
            check(report2 is not None, "分析完成")
            if report2:
                lazy2 = report2.get("lazy_load") or {}
                tree2 = report2.get("simplified_tree") or ""
                print(f"    lazy_load: {lazy2.get('summary')}")
                print(f"    reason={lazy2.get('reason')} rounds={lazy2.get('rounds')} "
                      f"仍在增长={lazy2.get('still_growing')}")
                print(f"    高度序列: {lazy2.get('height_series')}")
                check(lazy2.get("infinite") is True,
                      "**如实报告无限流(界面据此询问用户是否继续)**",
                      str(lazy2.get("reason")))
                check(lazy2.get("still_growing") is True, "标记为仍在增长")
                check(tree2.count("/artworks/") > 0, "同样加载出了内容",
                      f"{tree2.count('/artworks/')} 个链接")
        finally:
            site2.stop()

        # ==============================================================
        print("\n=== 3) deep_scroll 能加载更多(用户选择『继续向下滚动』) ===")
        site3 = Site(max_batches=0)
        try:
            normal = await analyze(client, f"{site3.base}/")
            deep = await analyze(client, f"{site3.base}/", deep_scroll=12)
            n1 = (normal or {}).get("lazy_load") or {}
            n2 = (deep or {}).get("lazy_load") or {}
            # 用 dom_stats 而不是数简化树里的链接: 简化树对每个节点只取前 20 个子节点,
            # 内容一多就会被截断 —— 拿它比"加载了多少"会得到恒定的 20, 测的是截断不是内容。
            s1 = (normal or {}).get("dom_stats") or {}
            s2 = (deep or {}).get("dom_stats") or {}
            print(f"    默认:   rounds={n1.get('rounds')} 新增节点={n1.get('node_gain')} "
                  f"DOM 节点={s1.get('total_elements')} 链接={s1.get('links')}")
            print(f"    加深后: rounds={n2.get('rounds')} 新增节点={n2.get('node_gain')} "
                  f"DOM 节点={s2.get('total_elements')} 链接={s2.get('links')}")
            check(n2.get("rounds", 0) > n1.get("rounds", 0),
                  "deep_scroll 带来了更多滚动轮次",
                  f"{n1.get('rounds')} -> {n2.get('rounds')}")
            check(
                (s2.get("total_elements") or 0) > (s1.get("total_elements") or 0)
                or (s2.get("links") or 0) > (s1.get("links") or 0),
                "**加载到了更多内容**",
                f"节点 {s1.get('total_elements')} -> {s2.get('total_elements')}, "
                f"链接 {s1.get('links')} -> {s2.get('links')}",
            )
        finally:
            site3.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"懒加载滚动验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"懒加载滚动验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
