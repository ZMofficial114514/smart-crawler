"""
"一直滚到用户说不滚为止"的回归验收。

用户要求: 滚动轮数可由用户设置; 停止时询问是否继续, **直到用户选择否才停止**。

这与其他测试的区别在于它是**阻塞式交互**: 后端的滚动循环会停下来等前端回话。
因此这里要验证:
  1. 内容无上限时, 后端确实发出 ``confirm_scroll`` 事件并**等待**;
  2. 回"继续"→ 后端继续滚、加载更多, 并**再次询问**(可以反复);
  3. 回"停止"→ 循环结束, 任务正常出结果(而不是被硬截断);
  4. 用户设定的轮数生效。

用法: python scripts/verify_scroll_interactive.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
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


TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无限流靶站</title>
<style>
  body {{ margin: 0; height: 100vh; overflow: hidden; }}
  #scroller {{ position: absolute; left: 0; right: 0; top: 0; height: 100vh; overflow-y: auto; }}
  .work {{ height: 110px; border-bottom: 1px solid #ddd; }}
</style></head><body>
<div id="scroller"><div class="grid" id="grid"></div></div>
<script>
const MAX = {max_batches}, SIZE = 6;
let batch = 0, loading = false;
const grid = document.getElementById('grid'), scroller = document.getElementById('scroller');
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
// 首屏填满视口, 否则不会出现滚动条 -> scroll 永不触发 -> 内容不增长
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
    def __init__(self, max_batches: int = 0) -> None:
        html = TEMPLATE.format(max_batches=max_batches).encode("utf-8")

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


async def drive_scroll_prompts(
    client: httpx.AsyncClient, task_id: str, answers: list[bool], timeout: float = 180.0
) -> list[dict]:
    """轮询任务; 每看到一次"等待确认"就按 ``answers`` 回一次话。

    返回收到过的所有 confirm_scroll 事件 payload(用于断言"反复询问")。
    """
    seen: list[dict] = []
    replies = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(1.5)
        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        # **先看终态**: 任务失败时不该继续回话(否则会空转上百次, 把真正的错误盖掉)
        if detail.get("status") in ("success", "failed", "cancelled"):
            return seen
        if detail.get("waiting_scroll"):
            payload = detail.get("scroll_prompt") or {}
            seen.append(payload)
            cont = answers[replies] if replies < len(answers) else False
            await client.post(
                f"/api/tasks/{task_id}/scroll", json={"continue": cont, "rounds": 4}
            )
            print(f"    第 {replies + 1} 次询问 -> 回答{'继续' if cont else '停止'}"
                  f" (已滚 {payload.get('rounds')} 轮)")
            replies += 1
    return seen


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site(0)  # 无上限
    print(f"无限流靶站: {site.base}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=180.0) as client:
            # ==============================================================
            print("=== 1) 发起抓取(用户设定滚动 3 轮, 允许询问) ===")
            r = await client.post(
                "/api/crawl",
                json={
                    "url": f"{site.base}/",
                    "goal": "抓取所有作品标题",
                    "max_pages": 1,
                    "scroll_rounds": 3,
                    "scroll_continue_rounds": 4,
                    "ask_scroll": True,
                },
            )
            check(r.status_code in (200, 202), "任务已提交", str(r.status_code))
            task_id = (r.json().get("task") or {}).get("id")
            check(bool(task_id), "拿到 task_id")

            # ==============================================================
            print("\n=== 2) 询问 -> 继续 -> 再询问 -> 继续 -> 停止 ===")
            # 先回两次"继续", 第三次回"停止" —— 验证"反复询问直到用户说不"
            prompts = await drive_scroll_prompts(client, task_id, [True, True, False])
            print(f"    总共收到 {len(prompts)} 次询问")

            check(len(prompts) >= 2,
                  "**反复询问直到用户选择停止**(至少问了 2 次)", f"{len(prompts)} 次")

            detail = (await client.get(f"/api/tasks/{task_id}")).json()
            check(detail.get("status") in ("success", "failed"),
                  "回停止后任务正常结束", str(detail.get("status")))

            result = detail.get("result") or {}
            lazy = result.get("lazy_load") or {}
            print(f"    lazy_load: {lazy.get('summary')}")
            print(f"    reason={lazy.get('reason')} rounds={lazy.get('rounds')} "
                  f"续滚次数={lazy.get('continuations')}")
            check(lazy.get("reason") == "user_stopped",
                  "**结束原因标记为『用户选择停止』**", str(lazy.get("reason")))
            check(lazy.get("continuations", 0) >= 1,
                  "记录到用户续滚过", str(lazy.get("continuations")))
            # 用户设定 3 轮 + 续滚 2 次 x 4 轮 = 期望 > 3 轮
            check(lazy.get("rounds", 0) > 3,
                  "**实际滚动轮数超过了初始设定**(因为用户续滚了)",
                  f"{lazy.get('rounds')} 轮")
            check(result.get("items") is not None or result.get("item_count") is not None,
                  "仍然正常产出了抓取结果")

    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"交互式续滚验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"交互式续滚验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
