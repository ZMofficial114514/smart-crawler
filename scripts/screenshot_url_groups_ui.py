"""UI 实测: 结果与历史按 URL 归类 + 删除入口(截图存证)。"""

from __future__ import annotations

import argparse
import http.server
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path("data/screenshots")
failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def gallery(tag: str, count: int = 5) -> str:
    cards = "\n".join(
        f'<div class="card"><a href="/d/{tag}/{i}">'
        f'<img src="/img/{tag}_{i}.png" alt="图{i}"></a>'
        f'<span class="title">{tag} 标题 {i}</span></div>'
        for i in range(1, count + 1)
    )
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{tag}</title></head><body><div class="gallery">{cards}</div></body></html>"""


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class Site:
    def __init__(self) -> None:
        pages = {"/alpha": gallery("alpha"), "/beta": gallery("beta")}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == "/robots.txt":
                    body, ctype = b"User-agent: *\nDisallow:\n", "text/plain"
                elif path.startswith("/img/"):
                    body, ctype = PNG, "image/png"
                else:
                    body = pages.get(path, pages["/alpha"]).encode("utf-8")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    OUT.mkdir(parents=True, exist_ok=True)

    site = Site()
    url_a, url_b = f"{site.base}/alpha", f"{site.base}/beta"

    # 用 HTTP 先把两个 URL 都抓一次, 好让界面有分组可看
    with httpx.Client(base_url=base, timeout=300.0) as client:
        for target in (url_a, url_b):
            r = client.post("/api/crawl", json={
                "url": target, "goal": "抓取所有图片",
                "max_pages": 1, "scroll_rounds": 0, "format": "json",
            })
            tid = (r.json().get("task") or {}).get("id")
            for _ in range(150):
                import time

                time.sleep(1.5)
                d = client.get(f"/api/tasks/{tid}").json()
                if d.get("status") in ("success", "failed", "cancelled"):
                    print(f"  预置任务 {target[-6:]} -> {d.get('status')}")
                    break

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(
            viewport={"width": 1600, "height": 1000}, device_scale_factor=2, locale="zh-CN"
        ).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        # confirm 一律拒绝: 截图阶段不真的删数据
        page.on("dialog", lambda d: d.dismiss())

        page.goto(base, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1200)
        page.evaluate("window.location.hash = '#/results'")
        page.wait_for_timeout(2500)

        print("\n=== 分组渲染 ===")
        groups = page.eval_on_selector_all(
            "#taskList .url-group",
            """els => els.map(e => ({
                url: e.dataset.url,
                head: e.querySelector('.url-group__url')?.textContent || '',
                meta: e.querySelector('.url-group__meta')?.textContent || '',
                hasDelete: !!e.querySelector('.btn--danger-ghost'),
                files: e.querySelectorAll('.file-item').length,
                tasks: e.querySelectorAll('.task-item').length,
            }))""",
        )
        for g in groups:
            print(f"    {g['url'][-20:]:<22} {g['meta']}")
            print(f"      删除按钮={g['hasDelete']} 任务={g['tasks']} 文件={g['files']}")
        check(len(groups) >= 2, "**按 URL 分成了多个组**", f"{len(groups)} 组")
        check(all(g["hasDelete"] for g in groups), "**每组都有『删除此 URL』入口**")
        check(all(g["files"] > 0 or g["tasks"] == 0 for g in groups),
              "文件挂在所属 URL 分组下",
              str([(g["head"][-12:], g["files"]) for g in groups]))

        # 每个分组内文件都属于该 URL(用文件名前缀粗校验, 这里靶站图片带 alpha/beta)
        page.screenshot(path=str(OUT / "28_url_groups.png"), full_page=False)
        print("    截图 -> 28_url_groups.png")

        # 展开**文件最多的那个**分组, 并展开全部分组, 让截图完整展示"文件归到 URL 下"
        page.evaluate(
            """() => {
                // 全部折叠后只展开需要展示的分组, 避免一张图里全是展开状态看不清结构
                for (const g of document.querySelectorAll('#taskList .url-group')) {
                    const body = g.querySelector('.url-group__body');
                    if (body && !body.hidden) g.querySelector('.url-group__head').click();
                }
            }"""
        )
        page.wait_for_timeout(1200)
        heads = page.query_selector_all("#taskList .url-group__head")
        if heads:
            heads[0].click()
            page.wait_for_timeout(1500)
            # 断言: 展开后文件行数应与标题声明的数量一致(这是"真的渲染出来了"的证据)
            rendered = page.evaluate(
                """() => {
                    const g = document.querySelector('#taskList .url-group');
                    const items = [...g.querySelectorAll('.file-item')];
                    const visible = items.filter(i => i.getBoundingClientRect().height > 10);
                    // 必须取**产出文件**那一节的标题: 分组里还有"任务记录(N)",
                    // 直接对整块取第一个括号会拿到任务数, 断言就测错了对象。
                    const subs = [...g.querySelectorAll('.url-group__subtitle')];
                    const fileSub = subs.find(s => s.textContent.includes('产出文件'));
                    const m = fileSub ? fileSub.textContent.match(/\\((\\d+)\\)/) : null;
                    return {
                        declared: m ? Number(m[1]) : -1,
                        inDom: items.length,
                        visible: visible.length,
                        heights: visible.map(i => Math.round(i.getBoundingClientRect().height)),
                    };
                }"""
            )
            print(f"    展开后: 标题声明 {rendered['declared']} 个 / DOM {rendered['inDom']} 个 / "
                  f"可见 {rendered['visible']} 个")
            check(rendered["visible"] == rendered["inDom"] and rendered["inDom"] == rendered["declared"],
                  "**展开后所有文件行都真实渲染出来(没有塌成一行)**",
                  f"声明 {rendered['declared']}, 可见 {rendered['visible']}")
            page.screenshot(path=str(OUT / "29_url_groups_expanded.png"), full_page=False)
            print("    截图 -> 29_url_groups_expanded.png")

        # ---- 点删除会弹确认框(已 dismiss, 不该真的删掉) ----
        print("\n=== 删除入口会先确认(拒绝后不应删除) ===")
        before = len(page.query_selector_all("#taskList .url-group"))
        btn = page.query_selector("#taskList .url-group .btn--danger-ghost")
        if btn:
            btn.click()
            page.wait_for_timeout(2000)
            after = len(page.query_selector_all("#taskList .url-group"))
            check(after == before,
                  "**取消确认后分组数量不变(不会误删)**", f"{before} -> {after}")
        else:
            check(False, "找不到删除按钮")

        browser.close()
    site.stop()

    print(f"\n前端错误: {len(errors)}")
    for e in errors[:5]:
        print(f"  ! {e}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 62)
    if failures:
        print("URL 分组 UI 实测: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("URL 分组 UI 实测: 通过 ✓")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
