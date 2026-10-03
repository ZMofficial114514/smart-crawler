"""
下载数量约束的回归验收(用户报障)。

原始场景: 抓取目标填"爬取前三张", 结果下载了滚动后的**全部 24 张**(堆糖)。
根因: AI 生成的提取规则里只有"怎么取", 没有"取几个" —— **数量约束从来没有传到下载环节**,
而图片下载器只看自己配置里的 `max_items`(默认 200), 于是有多少下多少。

修复后的优先级: **任务参数(抓取页填的数量) > 抓取目标里的数量 > 插件配置 > 内置默认**。

用法: python scripts/verify_media_limit.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import sys
import threading
import urllib.parse
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


# 一个图库列表页: 30 张卡片, 每张一个图片链接。图片本身由同一个服务提供。
def gallery_html(count: int = 30) -> str:
    cards = "\n".join(
        f'<div class="card"><a href="/detail/{i}">'
        f'<img src="/img/{i}.png" alt="图{i}"></a>'
        f'<span class="title">图片标题 {i}</span></div>'
        for i in range(1, count + 1)
    )
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>图库列表</title></head><body>
<div class="gallery">{cards}</div>
</body></html>"""


# 1x1 PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001od".replace("od", "0d")
    + "0a2db40000000049454e44ae426082"
)


class Site:
    def __init__(self, count: int = 30) -> None:
        html = gallery_html(count).encode("utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/robots.txt":
                    body, ctype = b"User-agent: *\nDisallow:\n", "text/plain"
                elif path.startswith("/img/"):
                    body, ctype = PNG, "image/png"
                else:
                    body, ctype = html, "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
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


async def crawl(client: httpx.AsyncClient, payload: dict) -> dict:
    r = await client.post("/api/crawl", json=payload)
    if r.status_code not in (200, 202):
        return {"_error": f"HTTP {r.status_code}: {r.text[:120]}"}
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(120):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return d.get("result") or {}
    return {"_error": "超时"}


def downloaded_images(result: dict) -> list[str]:
    """本次任务实际下载的图片文件名列表。

    注意字段名是 ``filename``(不是 name/path) —— 按错键会**静默拿到 0 个**,
    于是断言"下载了 0 张"也能"通过", 测出一个假绿。
    """
    out = []
    for item in result.get("downloads") or []:
        name = str(item.get("filename") or item.get("path") or "")
        if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            out.append(name)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site(count=30)
    print(f"图库靶站: {site.base} (30 张卡片)\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=300.0) as client:
            # ==============================================================
            print("=== 1) 抓取目标写'爬取前三张' -> 只应下载 3 张 ===")
            res = await crawl(client, {
                "url": f"{site.base}/",
                "goal": "爬取前三张图片",
                "max_pages": 1,
                "scroll_rounds": 0,
                "format": None,
            })
            if res.get("_error"):
                check(False, "任务完成", str(res["_error"]))
            else:
                got = downloaded_images(res)
                print(f"    提取条数 = {res.get('item_count')}")
                print(f"    下载图片 = {len(got)} 张: {got[:6]}")
                check(len(got) == 3,
                      "**目标里的数量约束生效(下载 3 张, 不是 30 张)**",
                      f"{len(got)} 张")

            # ==============================================================
            print("\n=== 2) 抓取页填'下载数量 = 5' -> 只应下载 5 张 ===")
            res2 = await crawl(client, {
                "url": f"{site.base}/",
                "goal": "抓取所有图片",
                "max_pages": 1,
                "scroll_rounds": 0,
                "media_limit": 5,
                "format": None,
            })
            if res2.get("_error"):
                check(False, "任务完成", str(res2["_error"]))
            else:
                got2 = downloaded_images(res2)
                print(f"    提取条数 = {res2.get('item_count')}")
                print(f"    下载图片 = {len(got2)} 张: {got2[:8]}")
                check(len(got2) == 5,
                      "**任务参数优先(下载 5 张)**", f"{len(got2)} 张")

            # ==============================================================
            print("\n=== 3) 两者都填 -> 任务参数优先 ===")
            res3 = await crawl(client, {
                "url": f"{site.base}/",
                "goal": "爬取前两张图片",
                "max_pages": 1,
                "scroll_rounds": 0,
                "media_limit": 7,
                "format": None,
            })
            if res3.get("_error"):
                check(False, "任务完成", str(res3["_error"]))
            else:
                got3 = downloaded_images(res3)
                print(f"    下载图片 = {len(got3)} 张")
                check(len(got3) == 7,
                      "**显式填写的数量覆盖目标里的数量**", f"{len(got3)} 张")

            # ==============================================================
            print("\n=== 4) 都不填 -> 不设人为上限(按插件默认), 应远多于 3 张 ===")
            res4 = await crawl(client, {
                "url": f"{site.base}/",
                "goal": "抓取所有图片",
                "max_pages": 1,
                "scroll_rounds": 0,
                "format": None,
            })
            if res4.get("_error"):
                check(False, "任务完成", str(res4["_error"]))
            else:
                got4 = downloaded_images(res4)
                print(f"    下载图片 = {len(got4)} 张")
                check(len(got4) > 3,
                      "没填数量时不受'前三张'影响", f"{len(got4)} 张")
    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"下载数量约束验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"下载数量约束验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
