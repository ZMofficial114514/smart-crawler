"""
提取规则里必须带上图片/音频/视频的键值对(用户要求)。

背景: 图片下载与音频下载插件都是按**字段名**去记录里取地址的 —— 规则里没有
``image`` / ``audio`` 这个键, 插件就什么都下不到。而默认的字段推断只看文本与
``img[src]``, 于是:

- 懒加载图站(堆糖典型)的 ``<img src="占位" data-src="真图">`` 会取到占位图;
- 用 ``background-image`` 的图站一个字段都推不出来;
- 音频/视频元素完全被忽略。

本脚本用受控页面固化这几种情况。

用法: python scripts/verify_media_fields.py [--port 8322]
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
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


# 复刻图站: 懒加载图片(data-src 才有真地址) + 部分背景图
LAZY_IMAGES = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>图库 - 懒加载</title></head><body>
<div class="feed">
  <div class="card" data-id="1"><a href="/detail/1">
    <img src="/img/placeholder.gif" data-src="/img/real_1.jpg" alt="图一">
    <span class="title">图片标题一</span></a></div>
  <div class="card" data-id="2"><a href="/detail/2">
    <img src="/img/placeholder.gif" data-src="/img/real_2.jpg" alt="图二">
    <span class="title">图片标题二</span></a></div>
  <div class="card" data-id="3"><a href="/detail/3">
    <img src="/img/placeholder.gif" data-src="/img/real_3.jpg" alt="图三">
    <span class="title">图片标题三</span></a></div>
  <div class="card" data-id="4"><a href="/detail/4">
    <img src="/img/placeholder.gif" data-src="/img/real_4.jpg" alt="图四">
    <span class="title">图片标题四</span></a></div>
</div></body></html>"""

# 背景图实现(部分图站这么干)
BG_IMAGES = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>图库 - 背景图</title></head><body>
<div class="feed">
  <div class="card"><a href="/d/1" class="thumb"
      style="background-image:url('/img/bg_1.jpg')"><span class="title">背景图一</span></a></div>
  <div class="card"><a href="/d/2" class="thumb"
      style="background-image:url('/img/bg_2.jpg')"><span class="title">背景图二</span></a></div>
  <div class="card"><a href="/d/3" class="thumb"
      style="background-image:url('/img/bg_3.jpg')"><span class="title">背景图三</span></a></div>
  <div class="card"><a href="/d/4" class="thumb"
      style="background-image:url('/img/bg_4.jpg')"><span class="title">背景图四</span></a></div>
</div></body></html>"""

# 音频/视频列表
MEDIA = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>音乐列表</title></head><body>
<div class="tracks">
  <div class="track"><span class="name">歌曲一</span>
    <audio src="/audio/a1.mp3" controls></audio>
    <a href="/download/a1.mp3" class="dl">下载</a></div>
  <div class="track"><span class="name">歌曲二</span>
    <audio src="/audio/a2.mp3" controls></audio>
    <a href="/download/a2.mp3" class="dl">下载</a></div>
  <div class="track"><span class="name">歌曲三</span>
    <audio src="/audio/a3.mp3" controls></audio>
    <a href="/download/a3.mp3" class="dl">下载</a></div>
</div></body></html>"""

# 缩略图/原图: 列表页给缩略图, 下载时应换成原图
THUMB_ORIGINAL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>图库 - 缩略图</title></head><body>
<div class="feed">
  <div class="card"><a href="/d/1"><img src="/img/photo1.thumb.400_0.jpg" alt="图一"></a>
    <span class="title">标题一</span></div>
  <div class="card"><a href="/d/2"><img src="/img/photo2.thumb.400_0.jpg" alt="图二"></a>
    <span class="title">标题二</span></div>
  <div class="card"><a href="/d/3"><img src="/img/photo3.thumb.400_0.jpg" alt="图三"></a>
    <span class="title">标题三</span></div>
  <div class="card"><a href="/d/4"><img src="/img/photo4.thumb.400_0.jpg" alt="图四"></a>
    <span class="title">标题四</span></div>
</div></body></html>"""


TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffc00011080001000103012200021101031101ffc4001f00000105010101010101000000"
    "00000000000102030405060708090a0bffc400b5100002010303020403050504040000017d"
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282"
    "090a161718191a25262728292a3435363738393a434445464748494a535455565758595a63"
    "6465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6"
    "a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5"
    "e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda000c03010002110311003f00fefa28a2803fff"
    "d9"
)


class Site:
    def __init__(self) -> None:
        import urllib.parse

        pages = {
            "/lazy": LAZY_IMAGES,
            "/bg": BG_IMAGES,
            "/media": MEDIA,
            "/thumb": THUMB_ORIGINAL,
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/robots.txt":
                    body, ctype = b"User-agent: *\nDisallow:\n", "text/plain"
                elif path.startswith("/img/"):
                    body, ctype = TINY_JPEG, "image/jpeg"
                else:
                    body = pages.get(path, LAZY_IMAGES).encode("utf-8")
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


async def analyze(client: httpx.AsyncClient, url: str) -> dict | None:
    r = await client.post("/api/analyze", json={"url": url, "scroll_rounds": 0})
    if r.status_code not in (200, 202):
        print(f"    提交失败 HTTP {r.status_code}")
        return None
    task_id = (r.json().get("task") or {}).get("id")
    for _ in range(60):
        await asyncio.sleep(2)
        d = (await client.get(f"/api/tasks/{task_id}")).json()
        if d.get("status") in ("success", "failed", "cancelled"):
            return (d.get("result") or {}).get("report") or {}
    return None


def field_map(report: dict) -> dict[str, dict]:
    """把最佳候选的字段整理成 {字段名: 字段定义}。"""
    cands = report.get("candidate_lists") or []
    if not cands:
        return {}
    best = max(cands, key=lambda c: c.get("count") or 0)
    return {f.get("name"): f for f in (best.get("sample_fields") or [])}


async def crawl(client: httpx.AsyncClient, payload: dict) -> dict:
    """跑一次真实抓取, 等它结束并返回结果(用于验证下载产物)。"""
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
            print("=== 1) 懒加载图片: 应取 data-src 而不是占位图 ===")
            rep = await analyze(client, f"{site.base}/lazy")
            check(rep is not None, "分析完成")
            if rep:
                fields = field_map(rep)
                print(f"    字段: {json.dumps(fields, ensure_ascii=False)[:400]}")
                check("image" in fields, "**规则里有 image 字段**", str(list(fields)))
                if "image" in fields:
                    attr = fields["image"].get("attribute")
                    check(attr == "data-src",
                          "**image 取的是 data-src(真图), 不是 src(占位图)**",
                          f"attribute={attr!r}")

            # ==============================================================
            print("\n=== 2) 背景图: 应能推出 image 字段 ===")
            rep2 = await analyze(client, f"{site.base}/bg")
            check(rep2 is not None, "分析完成")
            if rep2:
                fields2 = field_map(rep2)
                print(f"    字段: {json.dumps(fields2, ensure_ascii=False)[:400]}")
                check("image" in fields2, "**背景图也推出了 image 字段**",
                      str(list(fields2)))

            # ==============================================================
            print("\n=== 3) 音频: 规则里应有 audio 字段 ===")
            rep3 = await analyze(client, f"{site.base}/media")
            check(rep3 is not None, "分析完成")
            if rep3:
                fields3 = field_map(rep3)
                print(f"    字段: {json.dumps(fields3, ensure_ascii=False)[:400]}")
                check("audio" in fields3, "**规则里有 audio 字段**", str(list(fields3)))

            # ==============================================================
            print("\n=== 4) 缩略图 -> 原图: 下载到的应是原图而不是缩略图 ===")
            # 配置图片下载器的"缩略图|原图"对照, 程序自动推规则
            shot = f"{site.base}/img/photo9.thumb.400_0.jpg|{site.base}/img/photo9.thumb.1000_0.jpg"
            cfg = await client.post(
                "/api/plugins/image-downloader/config",
                json={"config": {"original_pairs": shot, "prefer_original": True}},
            )
            check(cfg.status_code == 200, "写入图片下载器配置", str(cfg.status_code))

            out = await crawl(client, {
                "url": f"{site.base}/thumb",
                "goal": "抓取所有图片",
                "max_pages": 1,
                "scroll_rounds": 0,
                "format": None,
            })
            if out.get("_error"):
                check(False, "抓取完成", str(out["_error"]))
            else:
                # 用记录里的 ``url`` 判断"究竟下的是哪一张": 它就是实际去下载的地址,
                # 比看文件名更直接。字段名必须是 filename/url —— 按错键会静默拿到 0 个,
                # 让"0 个里 0 个是缩略图"这种断言假绿。
                urls = [str(d.get("url") or "") for d in (out.get("downloads") or [])]
                files = [str(d.get("filename") or "") for d in (out.get("downloads") or [])]
                print(f"    下载 {len(urls)} 个: {files[:6]}")
                for u in urls[:3]:
                    print(f"      -> {u}")
                original = [u for u in urls if "1000_0" in u]
                thumb = [u for u in urls if "400_0" in u]
                check(len(urls) > 0, "**确实下载了图片**", f"{len(urls)} 个")
                check(len(original) == len(urls) and not thumb and len(urls) > 0,
                      "**下载的是原图(1000_0), 不再是缩略图(400_0)**",
                      f"原图 {len(original)} / 缩略图 {len(thumb)}")

            # 还原配置, 避免影响后续测试
            await client.post("/api/plugins/image-downloader/reset")

    finally:
        site.stop()

    print("\n" + "=" * 66)
    if failures:
        print(f"媒体字段验收: 未通过 ✗ ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"媒体字段验收: 通过 ✓ ({total}/{total})")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
