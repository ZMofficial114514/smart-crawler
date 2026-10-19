"""
验收: 内层 frame 的**内容**很晚才注入时, 结构分析不能提前放弃。

原始故障(用户报障): 抓取任务能正常拿到网易云数据, 但「结构分析」页显示为空。
日志证据:

    14:40:41  frame 规模: (主文档)=288/3候选, contentFrame=39/0候选
    14:40:41  WARNING 内容疑似未到位 —— 重新加载页面后再分析一次
    14:40:44  frame 规模: (主文档)=288/3候选, contentFrame=39/0候选   <- 只等了 2.4s
    14:40:44  WARNING 重载后内容仍未到位, 按当前结果继续               <- 放弃
    14:41:02  frame 规模: (主文档)=336/3候选, contentFrame=909/5候选  <- 18s 后内容才到

39 个元素是「外壳已加载、内容还没注入」。而 `_wait_for_frames_to_settle` 等的判据是
**元素数不再变化** —— 39 一直不变它就认为"稳定了", 于是提前返回; 判为不完整后重载,
重载同样只看"稳不稳", 又是 39, 于是两次加起来 2.4 秒就放弃了。

这里用本地靶站注入"内容延迟 N 秒注入"的确定性缺陷 —— 不依赖网易云当时的限流状态。

判据:
  1. 延迟 12s(远超旧的 2.4s 放弃点) -> 仍要最终拿到内层内容;
  2. 耗时要**有上限**(不能无限等);
  3. 单文档页面不能被这个改动拖慢(回归保护)。
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import sys
import threading
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def make_inner(delay: float) -> str:
    """内层页面: 延迟 delay 秒后才注入列表(模拟异步填内容)。"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>inner</title></head>
<body><div id="app">加载中…</div>
<script>
setTimeout(function () {{
  var items = '';
  for (var i = 1; i <= 15; i++) {{
    items += '<li class="item"><a href="/song?id=' + (2000 + i) + '">'
           + '<span class="name">歌曲 ' + i + '</span></a></li>';
  }}
  var extra = '';
  for (var j = 1; j <= 80; j++) {{ extra += '<div class="row"><span>单元 ' + j + '</span></div>'; }}
  document.getElementById('app').innerHTML =
      '<ul class="song-list">' + items + '</ul><div class="grid">' + extra + '</div>';
}}, {int(delay * 1000)});
</script></body></html>"""


#: 外壳: 有规模(才谈得上"内层比外壳小"), 且带一个命名 iframe。
_NAV = "".join(f'<li><a href="/n{i}">导航{i}</a></li>' for i in range(1, 21))
_FILLER = "".join(f'<div class="m"><span class="t">栏目{i}</span></div>'
                  for i in range(1, 41))
OUTER = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>shell</title></head>
<body>
<nav><ul class="m-nav">{_NAV}</ul></nav>
<div class="mods">{_FILLER}</div>
<iframe id="g_iframe" name="contentFrame" src="/inner"
        style="width:900px;height:600px"></iframe>
<footer><p>页脚</p></footer>
</body></html>"""

PLAIN = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>plain</title></head>
<body><ul class="products">
<li class="product"><a href="/p/1"><span class="title">商品一</span></a></li>
<li class="product"><a href="/p/2"><span class="title">商品二</span></a></li>
<li class="product"><a href="/p/3"><span class="title">商品三</span></a></li>
</ul></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    delay = 1.0

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/inner"):
            body = make_inner(Handler.delay).encode()
        elif self.path.startswith("/plain"):
            body = PLAIN.encode()
        else:
            body = OUTER.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: ANN002
        pass


def start_server() -> tuple[socketserver.TCPServer, int]:
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


async def main() -> int:
    from smartcrawler.config import get_settings  # noqa: PLC0415
    from smartcrawler.crawler import SmartCrawler  # noqa: PLC0415

    srv, port = start_server()
    base = f"http://127.0.0.1:{port}"
    print(f"  本地靶站: {base}\n")

    settings = get_settings()
    crawler = SmartCrawler(settings)
    try:
        await crawler.start()

        # --- 1) 内容延迟 12s 才注入 ---
        print("=== 1) 内层内容延迟 12s 注入(远超旧的放弃点)===")
        Handler.delay = 12.0
        t0 = time.time()
        rep, _ = await crawler.analyze_only(f"{base}/", scroll_rounds=0)
        dt = time.time() - t0
        el = (rep.dom_stats or {}).get("total_elements") if rep else None
        sels = [c.item_selector for c in (rep.candidate_lists if rep else [])]
        print(f"    耗时 {dt:.1f}s   frame={rep.content_frame!r}   元素={el}")
        print(f"    候选: {sels[:3]}")
        check(rep is not None and rep.content_frame == "contentFrame",
              "**最终定位到内层内容 frame**",
              repr(rep.content_frame if rep else None))
        # 内容是否真注入, 以"选中了内层列表"为准(靶站内层约 214 个元素, 外壳约 131)。
        # 注意别用真实网易云的 500 当阈值 —— 那是另一个页面的规模, 照抄会误判。
        check(bool(el) and el > 150,
              "**DOM 规模来自内层(内容确实注入了)**",
              f"{el}(外壳约 131)")
        check(any("song-list" in s for s in sels),
              "**识别出内层列表(内容注入的实证)**", f"{sels[:2]}")
        # 上限: 不能为了等而无限等
        check(dt < 90, "耗时在合理上限内(<90s)", f"{dt:.1f}s")

        # --- 2) 单文档页面不能被拖慢 ---
        print("\n=== 2) 单文档页面(无 iframe)不能变慢 ===")
        t0 = time.time()
        rep2, _ = await crawler.analyze_only(f"{base}/plain", scroll_rounds=0)
        dt2 = time.time() - t0
        print(f"    耗时 {dt2:.1f}s   候选 {len(rep2.candidate_lists) if rep2 else 0} 个")
        check(rep2 is not None and rep2.candidate_lists,
              "**普通页面照常识别出列表**",
              f"{len(rep2.candidate_lists) if rep2 else 0} 个候选")
        check(dt2 < 12, "普通页面没有变慢(<12s)", f"{dt2:.1f}s")
    finally:
        await crawler.close()
        srv.shutdown()

    print("\n" + "=" * 68)
    if failures:
        print(f"iframe 慢内容验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("iframe 慢内容验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
