"""
瀑布流"多列被当成多个列表"的回归验收。

**这是从用户的 duitang 案例里挖出来的一个隐蔽 bug。** 瀑布流布局的列表项 class 会带上
"列"的信息, 于是 24 张卡片被拆成 ``co0`` / ``co1`` / ``co2`` 三组, 每组都被当成一个**独立
列表**; 规则引擎只挑最大的一组, 最终规则**只覆盖一列**, 另外两列**静默丢失**。

真实测量(duitang 搜索页): 页面 24 张卡片, 修复前规则选中 9 项(一列), 修复后 24 项。

这种 bug 特别危险的地方在于它**不报错**: 抓取"成功"、有数据、看起来正常, 只是少了 2/3。

用法: python scripts/verify_masonry_merge.py [--port 8322]
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


# 复刻 duitang: 一个容器, 24 张卡片, 按列分 co0/co1/co2 三个 class
def masonry_html(per_column: int = 8) -> str:
    cards = []
    idx = 0
    for col in range(3):
        for _ in range(per_column):
            idx += 1
            cards.append(
                f'<div class="woo co{col}">'
                f'<a class="a" href="/detail/{idx}">'
                f'<img src="/img/{idx}.jpg" alt="图{idx}"></a>'
                f'<span class="title">图片标题 {idx}</span></div>'
            )
    joined = "\n".join(cards)
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>瀑布流图库</title></head><body>
<div class="woo-pcont">{joined}</div>
</body></html>"""


class Site:
    def __init__(self) -> None:
        html = masonry_html().encode("utf-8")

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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site()
    expected = 24  # 3 列 x 8 张
    print(f"靶站: {site.base} (共 {expected} 张卡片, 分 3 列)\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=180.0) as client:
            try:
                r = await client.post(
                    "/api/crawl",
                    json={"url": f"{site.base}/", "goal": "抓取所有图片", "max_pages": 1,
                          "scroll_rounds": 0},
                )
            except Exception as exc:  # noqa: BLE001
                check(False, "任务已提交", f"{type(exc).__name__}: {exc}")
                return 1
            check(r.status_code in (200, 202), "任务已提交", str(r.status_code))
            task_id = (r.json().get("task") or {}).get("id")
            result: dict = {}
            status = "unknown"
            for _ in range(150):                      # 最长等 300 秒(批跑时机器更忙)
                await asyncio.sleep(2)
                d = (await client.get(f"/api/tasks/{task_id}")).json()
                status = str(d.get("status"))
                if status in ("success", "failed", "cancelled"):
                    result = d.get("result") or {}
                    break

            # **超时必须单独报出来**: 否则 result 是空字典 -> 条数 0 -> 看起来像
            # "只抓到了一列(或没抓到)", 把"还没跑完"误读成"合并逻辑坏了"。
            # 这个误读真实发生过: 批跑时偶发 1/4 失败, 单独跑却一直是 6/6。
            if status not in ("success", "failed", "cancelled"):
                check(False, "任务在超时前结束", f"状态仍为 {status!r}(可能是机器忙)")
                return 1
            if not result:
                check(False, "任务产出了结果", f"status={status}, result 为空")
                return 1
            print(f"  任务状态      = {status}")

            rule = result.get("rule") or {}
            item_sel = ((rule.get("list_rule") or {}).get("item_selector")) or ""
            count = result.get("item_count") or 0

            print(f"  item_selector = {item_sel}")
            print(f"  抓取条数      = {count} (期望 {expected})")

            check(count == expected,
                  f"**覆盖全部 {expected} 张卡片(不是只抓一列)**", f"{count} 条")
            check(count > 8,
                  "条数明显超过单列(8 张), 说明三列被合并了", f"{count} 条")

            # 界面负载里只有预览(设计如此: 避免几万条塞进 WebSocket 帧), 所以用 items_preview
            items = result.get("items_preview") or []
            print(f"  预览条数      = {len(items)} (items_truncated="
                  f"{result.get('items_truncated')})")
            with_image = [i for i in items if i.get("image")]
            check(len(with_image) == len(items) and len(items) > 0,
                  "**每条都带 image 字段(图片下载插件才有东西可下)**",
                  f"{len(with_image)}/{len(items)}")
            if with_image:
                print(f"  首条 image = {str(with_image[0].get('image'))[:80]}")
                blob = " ".join(str(i.get("image")) for i in with_image)
                check(any(f"/img/{i}.jpg" in blob for i in range(1, 9)), "第一列的图在结果里")
                check(any(f"/img/{i}.jpg" in blob for i in range(17, 25)),
                      "**第三列的图也在结果里**")
    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"瀑布流合并验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"瀑布流合并验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
