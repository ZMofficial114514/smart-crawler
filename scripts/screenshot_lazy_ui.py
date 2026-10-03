"""UI 实测: 无限流页面的"继续向下滚动"提示(截图存证)。

用户要求: "若本身无加载上限, 则提示用户是否需要继续向下滚动"。
这里用受控的无限流靶站走真实分析链路, 验证:
  1. 提示条出现, 并说明"内容没有加载上限";
  2. 有「继续向下滚动」按钮;
  3. 点击后真的滚了更多轮、加载到更多内容。
"""

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir

prepare_temp_dir()

from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "http://127.0.0.1:8322"
OUT = Path("data/screenshots")

TEMPLATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>无限流靶站</title>
<style>
  body { margin: 0; height: 100vh; overflow: hidden; }
  #sidebar { position: fixed; left: 0; top: 0; width: 200px; height: 100vh;
             overflow-y: auto; background: #f2f2f2; }
  #scroller { position: absolute; left: 200px; right: 0; top: 0; height: 100vh; overflow-y: auto; }
  .work { height: 110px; border-bottom: 1px solid #ddd; padding: 6px; }
</style></head><body>
<div id="sidebar"><a href="/logout.php">退出登录</a><a href="/settings">设置</a></div>
<div id="scroller"><section class="contents"><div class="grid" id="grid"></div></section></div>
<script>
const MAX = 0, SIZE = 6;
let batch = 0, loading = false;
const grid = document.getElementById('grid'), scroller = document.getElementById('scroller');
function append() {
  if (MAX > 0 && batch >= MAX) return false;
  batch++;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < SIZE; i++) {
    const d = document.createElement('div');
    d.className = 'work';
    d.innerHTML = '<a href="/artworks/' + batch + '_' + i + '">作品 ' + batch + '-' + i + '</a>';
    frag.appendChild(d);
  }
  grid.appendChild(frag);
  return true;
}
// 首屏填满视口, 否则不出现滚动条 -> scroll 永不触发 -> 内容不增长(死锁)
let guard = 0;
while (scroller.scrollHeight <= scroller.clientHeight + 10 && guard++ < 50) {
  if (!append()) break;
}
scroller.addEventListener('scroll', () => {
  if (loading) return;
  if (scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 200) return;
  loading = true;
  setTimeout(() => { append(); loading = false; }, 100);
});
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"User-agent: *\nDisallow:\n" if self.path.startswith("/robots") else TEMPLATE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: A002
        pass


def wait_idle(page, timeout=300000):
    try:
        page.wait_for_function(
            "() => { const b = document.querySelector('#btnAnalyzeStart'); return b && !b.classList.contains('is-loading'); }",
            timeout=timeout,
        )
    except Exception:
        pass


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{port}/"
    print(f"无限流靶站: {target}")

    failures: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1500, "height": 900}, locale="zh-CN").new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(BASE, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        page.evaluate("window.location.hash = '#/analyze'")
        page.wait_for_timeout(800)
        page.fill("#analyzeUrl", target)

        # ---- 抓取任务里的"阻塞式询问"链路 ----
        # 分析页默认是**非阻塞**的(只滚一轮就出报告 + 一个「继续向下滚动」按钮);
        # 抓取页才是阻塞式的, 后端会停下来等用户回话, 可以一直点"继续"。
        # 这里重点验后者, 因为它才是"直到用户选择否才停止"的落点。
        page.evaluate("window.location.hash = '#/crawl'")
        page.wait_for_timeout(800)
        page.fill("#crawlUrl", target)
        page.fill("#crawlGoal", "抓取所有作品标题")
        page.fill("#crawlScrollRounds", "3")
        page.fill("#crawlScrollContinue", "4")
        page.check("#crawlAskScroll")
        page.click("#btnCrawlStart")

        # 等"是否继续滚动"的交互条出现(说明后端正在等我们回话)
        try:
            page.wait_for_selector("button:has-text('继续向下滚动')", timeout=180000)
            appeared = True
        except Exception:
            appeared = False

        print(f"\n无限流提示条: {'✓ 出现' if appeared else '✗ 未出现'}")
        if not appeared:
            failures.append("无限流提示条没有出现")
            heads = page.eval_on_selector_all("#crawlOutput h2, #crawlOutput .card__title",
                                               "e => e.map(x => x.textContent.trim())")
            print(f"  当前卡片: {heads}")
            cards = page.eval_on_selector_all("#scrollPrompt", "e => e.map(x => x.hidden)")
            print(f"  scrollPrompt hidden: {cards}")
        else:
            text = page.eval_on_selector("#scrollPrompt", "e => e.textContent")
            print(f"  文案: {(text or '')[:200]}")
            for needle, label in (
                ("是否继续向下滚动", "明确询问是否继续"),
                ("停止滚动", "提供『停止滚动』出口"),
            ):
                ok = needle in (text or "")
                print(f"    {'✓' if ok else '✗'} {label}")
                if not ok:
                    failures.append(label)

            shot = OUT / "23_lazy_infinite_scroll.png"
            page.screenshot(path=str(shot))
            print(f"  截图 -> {shot.name}")

            # ---- 反复点"继续", 验证是"一直问到用户说不" ----
            import re

            def rounds_in_prompt() -> int:
                blob = page.eval_on_selector("#scrollPrompt", "e => e.textContent") or ""
                match = re.search(r"滚动 (\d+) 轮", blob)
                return int(match.group(1)) if match else -1

            history: list[int] = []
            for attempt in range(3):
                before = rounds_in_prompt()
                if before < 0:
                    break
                history.append(before)
                print(f"  第 {attempt + 1} 次询问: 已滚 {before} 轮 -> 回答『继续』")
                page.click("button:has-text('继续向下滚动')")
                # 等后端再滚一段并**再次询问**
                grew = False
                for _ in range(60):
                    page.wait_for_timeout(1500)
                    now = rounds_in_prompt()
                    if now > before:
                        grew = True
                        break
                    # 交互条可能已被下一轮询问替换
                    if not page.is_visible("button:has-text('继续向下滚动')"):
                        continue
                if not grew:
                    print(f"    ✗ 轮次没有增长")
                    failures.append(f"第 {attempt + 1} 次继续后轮次没有增长")
                    break

            print(f"  轮次变化: {history}")
            check_again = len(history) >= 2
            print(f"  {'✓' if check_again else '✗'} 被反复询问(说明不是一刀切的上限)")
            if not check_again:
                failures.append(f"只询问了 {len(history)} 次, 没有反复询问")

            shot2 = OUT / "24_lazy_deeper.png"
            page.screenshot(path=str(shot2))
            print(f"  截图 -> {shot2.name}")

            # ---- 最后点"停止滚动", 任务应正常出结果 ----
            if page.is_visible("button:has-text('停止滚动')"):
                page.click("button:has-text('停止滚动')")
                print("  点了『停止滚动』")
                stopped = False
                for _ in range(90):
                    page.wait_for_timeout(2000)
                    if not page.is_visible("button:has-text('停止滚动')"):
                        stopped = True
                        break
                print(f"  {'✓' if stopped else '✗'} 停止后交互条消失, 任务收尾")
                if not stopped:
                    failures.append("点停止后交互条没有消失")

        browser.close()
    server.shutdown()

    print(f"\n前端错误: {len(errors)}")
    for e in errors[:6]:
        print(f"  ! {e}")
    if errors:
        failures.append(f"{len(errors)} 条前端错误")

    print("\n" + "=" * 62)
    if failures:
        print("懒加载 UI 实测: 未通过 ✗")
        for f in failures:
            print(f"  - {f}")
    else:
        print("懒加载 UI 实测: 通过 ✓")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
