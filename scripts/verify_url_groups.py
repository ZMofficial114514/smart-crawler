"""
"按 URL 归类 + 按 URL 删除并清理缓存"的回归验收。

用户要求:
  1. 结果与历史要**按具体 URL** 分类产出文件, 不要混在一起;
  2. 提供删除入口, 删除某个 URL 时**它对应的产出文件缓存也要一起清掉**。

第 2 条是最容易做错的地方: 只从列表里移除记录、磁盘上的文件却留着, 就是"假删除"。
所以这里**直接对文件系统断言** —— 删除前文件存在, 删除后必须不存在。

用法: python scripts/verify_url_groups.py [--port 8322]
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


def gallery(count: int, tag: str) -> str:
    cards = "\n".join(
        f'<div class="card"><a href="/d/{tag}/{i}">'
        f'<img src="/img/{tag}_{i}.png" alt="图{i}"></a>'
        f'<span class="title">{tag} 标题 {i}</span></div>'
        for i in range(1, count + 1)
    )
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{tag} 图库</title></head><body><div class="gallery">{cards}</div></body></html>"""


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class Site:
    """两个"不同站点路径", 用来验证分组确实按 URL 分开了。"""

    def __init__(self) -> None:
        pages = {"/site-a": gallery(6, "a"), "/site-b": gallery(4, "b")}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/robots.txt":
                    body, ctype = b"User-agent: *\nDisallow:\n", "text/plain"
                elif path.startswith("/img/"):
                    body, ctype = PNG, "image/png"
                else:
                    body = pages.get(path, pages["/site-a"]).encode("utf-8")
                    ctype = "text/html; charset=utf-8"
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


async def crawl(client: httpx.AsyncClient, url: str, fmt: str = "json") -> str:
    r = await client.post(
        "/api/crawl",
        json={"url": url, "goal": "抓取所有图片", "max_pages": 1, "scroll_rounds": 0, "format": fmt},
    )
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(120):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return task_id
    return task_id


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    site = Site()
    url_a = f"{site.base}/site-a"
    url_b = f"{site.base}/site-b"
    print(f"靶站: A={url_a}  B={url_b}\n")

    try:
        async with httpx.AsyncClient(base_url=base, timeout=300.0) as client:
            # ==============================================================
            print("=== 1) 两个 URL 各抓两次(应分成两组) ===")
            task_a1 = await crawl(client, url_a)
            task_a2 = await crawl(client, url_a)
            task_b1 = await crawl(client, url_b)
            print(f"    task: a1={task_a1} a2={task_a2} b1={task_b1}")

            groups = (await client.get("/api/tasks/by-url")).json().get("groups") or []
            by_url = {g["url"]: g for g in groups}
            for g in groups:
                print(f"    [{g['host']}] {g['url'][:52]} 任务={g['task_count']} "
                      f"文件={g['file_count']} 条目={g['item_count']}")

            check(url_a in by_url and url_b in by_url,
                  "**两个 URL 各自成组**", f"{len(groups)} 组")
            check(by_url.get(url_a, {}).get("task_count") == 2,
                  "A 组下有 2 条任务", str(by_url.get(url_a, {}).get("task_count")))
            check(by_url.get(url_b, {}).get("task_count") == 1,
                  "B 组下有 1 条任务", str(by_url.get(url_b, {}).get("task_count")))

            # ---- 产出文件确实挂在各自的 URL 下 ----
            files_a = by_url.get(url_a, {}).get("files") or []
            files_b = by_url.get(url_b, {}).get("files") or []
            print(f"    A 组文件: {[f['name'] for f in files_a][:5]}")
            print(f"    B 组文件: {[f['name'] for f in files_b][:5]}")
            check(len(files_a) >= 2, "**A 组的产出文件归到了 A 组**", f"{len(files_a)} 个")
            check(len(files_b) >= 1, "**B 组的产出文件归到了 B 组**", f"{len(files_b)} 个")

            a_paths = {f["path"] for f in files_a}
            b_paths = {f["path"] for f in files_b}
            check(not (a_paths & b_paths), "两组文件没有交叉(没有混在一起)")

            # ==============================================================
            print("\n=== 2) 磁盘上这些文件确实存在(删除前) ===")
            a_existing = [Path(p) for p in a_paths if Path(p).is_file()]
            b_existing = [Path(p) for p in b_paths if Path(p).is_file()]
            print(f"    A 组实际存在 {len(a_existing)} 个")
            for p in a_existing[:4]:
                print(f"      {p.name}  {p.stat().st_size} 字节")
            check(len(a_existing) >= 2, "**A 组的文件真实落盘了**", f"{len(a_existing)} 个")

            # ==============================================================
            print("\n=== 3) 删除 A 组: 记录与产出文件都应消失 ===")
            res = (await client.request("DELETE", "/api/tasks/by-url",
                                        params={"url": url_a})).json()
            print(f"    {res.get('message')}")
            print(f"    removed_tasks={res.get('removed_tasks')} "
                  f"removed_files={res.get('removed_files')} "
                  f"freed={res.get('freed_bytes')} 字节")
            check(res.get("ok") is True, "删除接口返回成功")
            check(res.get("removed_tasks") == 2, "**删掉了 A 的 2 条记录**",
                  str(res.get("removed_tasks")))
            check((res.get("removed_files") or 0) >= 2, "**清理了产出文件**",
                  str(res.get("removed_files")))

            still = [p for p in a_existing if p.exists()]
            print(f"    A 组文件残留: {[p.name for p in still]}")
            check(not still,
                  "**A 组的产出文件确实从磁盘上删掉了(不是只从列表移除)**",
                  f"残留 {len(still)} 个")

            # ---- B 组必须不受影响 ----
            b_still = [p for p in b_existing if p.is_file()]
            print(f"    B 组文件仍在: {[p.name for p in b_still]}")
            check(len(b_still) == len(b_existing),
                  "**B 组的文件没有被误删**", f"{len(b_still)}/{len(b_existing)}")

            groups2 = (await client.get("/api/tasks/by-url")).json().get("groups") or []
            urls2 = {g["url"] for g in groups2}
            print(f"    剩余分组: {[u[:46] for u in urls2]}")
            check(url_a not in urls2, "**A 分组从列表里消失了**")
            check(url_b in urls2, "B 分组仍在")
            check((by_url.get(url_b, {}).get("task_count") or 0) >= 1, "B 组任务数不受影响")

            # ==============================================================
            print("\n=== 4) 删除单条任务: 只清它自己的文件 ===")
            b_files = (await client.get("/api/tasks/by-url")).json()["groups"]
            b_group = next(g for g in b_files if g["url"] == url_b)
            b_paths_now = [Path(f["path"]) for f in b_group["files"] if f["exists"]]
            res2 = (await client.request("DELETE", f"/api/tasks/{task_b1}")).json()
            print(f"    {res2.get('message')}")
            check(res2.get("ok") is True, "删除单条任务成功")
            gone = [p for p in b_paths_now if not p.exists()]
            check(len(gone) >= 1, "**该任务的产出文件被清理**", f"{len(gone)} 个")

            after = (await client.get("/api/tasks/by-url")).json().get("groups") or []
            check(not any(g["url"] == url_b for g in after),
                  "B 组随之消失(它只有那一条任务)")

            # ==============================================================
            print("\n=== 5) 边界: 不存在 / 运行中 ===")
            r3 = await client.request("DELETE", "/api/tasks/by-url",
                                      params={"url": "https://never-crawled.example/"})
            check(r3.status_code == 400, "删除不存在的 URL 返回 400", str(r3.status_code))
            check("没有找到" in r3.text, "给出了明确原因", r3.json().get("detail", "")[:40])

            r4 = await client.request("DELETE", "/api/tasks/no-such-task")
            check(r4.status_code == 400, "删除不存在的任务返回 400", str(r4.status_code))

    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"URL 归类与清理验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"URL 归类与清理验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
