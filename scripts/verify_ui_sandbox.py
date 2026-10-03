"""
UI 沙箱验收 —— **不需要任何靶站**, 几秒跑完。

为什么单独做这一套: 之前每个 UI 改动都去拉真实/合成靶站跑一遍分析, 一次几十秒到几分钟,
而绝大多数 UI 问题(样式没生效、按钮没渲染、缓存导致旧界面)根本不需要抓取就能发现。
这里只做两类检查:

1. **静态类**: 直接请求服务发出的 CSS/JS/HTML 字节, 断言新样式与新元素确实在里面。
   这一类能抓住最坑的情况 —— 服务发的是旧文件(或浏览器拿到旧文件);
2. **页面类**: 打开界面, 断言元素存在、可见、可点, 以及几何(不重叠)。

用法: python scripts/verify_ui_sandbox.py [--port 8322]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = Path("data/screenshots")
failures: list[str] = []
total = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8322)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    OUT.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(base_url=base, timeout=30.0)

    # ======================================================================
    print("=== 1) 服务发出的文件里有没有新东西(静态断言) ===")
    assets = {
        "/styles/tokens.css": [
            "--bg-image", "--warm-1", "--warm-glass", "bg-main.jpg",
            "--bg-image-blur", "--bg-image-position",
        ],
        "/styles/base.css": [
            ".aurora::before", "blur(var(--bg-image-blur))", "--warm-glass",
            "background-size: cover",   # 等比: 只裁切不拉伸
        ],
        "/styles/components.css": [".url-group", "btn--danger-ghost", "chip--danger" if False else "segmented__thumb"],
        "/styles/pages.css": [".alert-slot", ".scroll-prompt__actions"],
        "/styles/layout.css": [".chip--danger", ".shutdown-banner"],
        "/js/pages/results.js": ["tasksByUrl", "deleteUrlGroup", "removeUrlGroup", "urlGroupNode"],
        "/js/main.js": ["shutdownService", "chipShutdown"],
        "/js/api.js": ["deleteTask", "deleteUrlGroup", "shutdown"],
        "/": ["analyzeAlertSlot", "chipShutdown", "按 URL 归类"],
    }
    for path, needles in assets.items():
        try:
            body = client.get(path).text
        except Exception as exc:  # noqa: BLE001
            check(False, f"能取到 {path}", str(exc)[:60])
            continue
        missing = [n for n in needles if n not in body]
        check(not missing, f"{path} 含全部新内容",
              f"缺少 {missing}" if missing else f"{len(body)} 字节")

    # ======================================================================
    print("\n=== 2) 缓存头: 改了前端必须能立刻看到(这是 UI 没变化的根因) ===")
    for path, expect in (
        ("/styles/tokens.css", "no-cache"),
        ("/js/main.js", "no-cache"),
        ("/", "no-store"),
        ("/styles/layout.css", "no-cache"),
    ):
        resp = client.get(path)
        cache = resp.headers.get("cache-control", "")
        check(expect in cache, f"{path} 的 Cache-Control 含 {expect!r}", cache or "(缺失)")

    # index.html 必须 no-store: 它是入口, 引用的资源名可能变
    idx = client.get("/")
    check("no-store" in idx.headers.get("cache-control", ""),
          "index.html 是 no-store(入口最不该被留用)", idx.headers.get("cache-control", ""))

    # ======================================================================
    print("\n=== 3) 关闭服务的能力 ===")
    spec = client.get("/api/openapi.json").json()
    paths = spec.get("paths", {})
    check("/api/shutdown" in paths, "**有 /api/shutdown 端点**",
          str([p for p in paths if "shutdown" in p]))
    check("post" in {m.lower() for m in paths.get("/api/shutdown", {})},
          "shutdown 是 POST")

    from smartcrawler.runtime import find_our_processes  # noqa: PLC0415

    check(callable(find_our_processes), "能枚举本框架的子进程(用于清理)")
    check(isinstance(find_our_processes(), list), "枚举返回列表")

    # ======================================================================
    print("\n=== 4) 页面元素(沙箱, 不抓任何站) ===")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        page.goto(base, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)

        # ---- 背景图 ----
        bg = page.evaluate(
            """async () => {
                const url = getComputedStyle(document.documentElement).getPropertyValue('--bg-image').trim();
                const m = url.match(/url\\(["']?(.+?)["']?\\)/);
                const aurora = document.querySelector('.aurora');
                const before = aurora ? getComputedStyle(aurora, '::before') : null;
                let img = { ok: false, w: 0, h: 0 };
                if (m) img = await new Promise(r => {
                    const i = new Image();
                    i.onload = () => r({ ok: true, w: i.naturalWidth, h: i.naturalHeight });
                    i.onerror = () => r({ ok: false, w: 0, h: 0 });
                    i.src = m[1];
                });
                const rect = aurora ? aurora.getBoundingClientRect() : null;
                return {
                    url,
                    img,
                    filter: before ? before.filter : '',
                    size: before ? before.backgroundSize : '',
                    position: before ? before.backgroundPosition : '',
                    inset: before ? before.inset : '',
                    transform: before ? before.transform : '',
                    viewport: rect ? { w: Math.round(rect.width), h: Math.round(rect.height) } : null,
                };
            }"""
        )
        check(bg["img"]["ok"], "**背景图能加载**", bg["url"])
        check("blur" in bg["filter"], "背景图有模糊", bg["filter"])

        # ---- 比例: 必须是等比缩放(cover), 不能拉伸 ----
        # 这一条是用户明确反馈过的: "图片不要随意扩大比例"。cover 只裁切不拉伸;
        # 100% 100% 或非等比 size 才会把画面拉变形, 所以这里把这三种写法区分开断言。
        check(bg["size"] == "cover",
              "**背景用 cover 等比缩放(不拉伸变形)**", bg["size"])
        check(bg["size"] not in ("100% 100%", "stretch"),
              "没有使用会拉伸的 100% 100%", bg["size"])

        # 模糊要"轻": 太重会把原画糊成色块。8~14px 是可用区间。
        import re as _re

        blur_match = _re.search(r"blur\(([\d.]+)px\)", bg["filter"] or "")
        blur_px = float(blur_match.group(1)) if blur_match else -1
        check(0 < blur_px <= 14,
              f"**模糊度较轻({blur_px}px ≤ 14px)**", bg["filter"])

        # 放大比例: 兜住 blur 的边只需要极小余量, 大比例会让画面"被放大"
        scale_match = _re.search(r"scale\(([\d.]+)\)", bg["transform"] or "")
        scale = float(scale_match.group(1)) if scale_match else 1.0
        check(scale <= 1.05, f"**几乎没有额外放大(scale={scale})**", bg["transform"])

        # 原图比例 vs 视口比例: 只要不是"恰好被硬拉到视口比例"就说明没拉伸。
        # cover 的裁切是正常的, 这里只做提示性输出, 便于人工核对构图。
        if bg["img"]["ok"] and bg["viewport"]:
            img_ratio = bg["img"]["w"] / max(bg["img"]["h"], 1)
            vp_ratio = bg["viewport"]["w"] / max(bg["viewport"]["h"], 1)
            print(f"    原图 {bg['img']['w']}x{bg['img']['h']} (比例 {img_ratio:.2f}) / "
                  f"视口 {bg['viewport']['w']}x{bg['viewport']['h']} (比例 {vp_ratio:.2f})"
                  f" -> cover 按 {min(img_ratio, vp_ratio) == img_ratio and '宽' or '高'} 铺满")
        print(f"    filter={bg['filter']}  size={bg['size']}  position={bg['position']}  "
              f"inset={bg['inset']}")

        # ---- 暖色 ----
        warm = page.evaluate(
            """() => {
                const card = document.querySelector('.card.glass');
                const thumb = document.querySelector('.segmented__thumb');
                return {
                    card: card ? getComputedStyle(card).backgroundColor : '',
                    border: card ? getComputedStyle(card).borderTopColor : '',
                    thumb: thumb ? getComputedStyle(thumb).backgroundImage : '',
                };
            }"""
        )
        check("246, 217, 168" in warm["border"] or "255, 236, 205" in warm["card"],
              "**卡片用暖色令牌**", f"bg={warm['card']} border={warm['border']}")
        check("246, 217, 168" in warm["thumb"], "**选项卡滑块是暖色**", warm["thumb"][:60])

        # ---- 关闭服务按钮 ----
        shut = page.evaluate(
            """() => {
                const b = document.querySelector('#chipShutdown');
                if (!b) return { found: false };
                const r = b.getBoundingClientRect();
                return { found: true, w: Math.round(r.width), h: Math.round(r.height),
                         visible: r.width > 10 && r.height > 10,
                         text: b.textContent.trim() };
            }"""
        )
        check(shut.get("found") and shut.get("visible"),
              "**顶栏有「关闭服务」按钮且可见**", str(shut))

        # ---- 结果页: URL 分组与删除入口(无任务时应给出空态而不是报错) ----
        page.evaluate("window.location.hash = '#/results'")
        page.wait_for_timeout(2000)
        result_ui = page.evaluate(
            """() => ({
                hasList: !!document.querySelector('#taskList'),
                hasFileList: !!document.querySelector('#fileList'),
                hasRefresh: !!document.querySelector('#btnRefreshTasks'),
                empty: (document.querySelector('#taskList')?.textContent || '').trim().slice(0, 40),
                title: (document.querySelector('#page-results h2')?.textContent || '').trim(),
                fileHint: (document.querySelector('#fileList')?.textContent || '').trim().slice(0, 40),
            })"""
        )
        check(result_ui["hasList"] and result_ui["hasFileList"], "结果页两个面板都在")
        check("URL" in result_ui["title"], "左面板标题已改为按 URL 归类", result_ui["title"])
        check("产出文件" in page.evaluate(
            "() => (document.querySelectorAll('#page-results h2')[1]?.textContent || '')"
        ), "右面板标题是『该 URL 的产出文件』")

        # ---- 提示文字对比度 ----
        # 这条来自用户反馈"灰色提示字看不清"。当时 --text-3 只有 50% 不透明度,
        # 实测压在最亮背景上只有 **1.91:1**(WCAG AA 要求 ≥4.5), 确实读不清。
        # 这里把"半透明文字合成后的实际颜色"算出来再测对比度, 而不是拿 CSS 里的
        # color 值直接比 —— 后者是未合成的颜色, 算出来会偏乐观。
        contrast = page.evaluate(
            """() => {
                const srgb = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
                const lum = ([r, g, b]) => 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
                const ratio = (a, b) => { const la = lum(a), lb = lum(b);
                    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05); };
                const parse = (css, fallback) => {
                    const n = (css.match(/[\\d.]+/g) || []).map(Number);
                    if (n.length >= 3 && /rgb/.test(css)) return { rgb: n.slice(0, 3), a: n.length > 3 ? n[3] : 1 };
                    // #rrggbb 形式(例如 --text-1: #ffffff)也要能解析,
                    // 否则会退化成黑色, 把对比度算成一个毫无意义的数
                    const hex = css.match(/^#([0-9a-f]{6})$/i);
                    if (hex) {
                        const v = parseInt(hex[1], 16);
                        return { rgb: [(v >> 16) & 255, (v >> 8) & 255, v & 255], a: 1 };
                    }
                    return fallback;
                };
                const compose = (fg, bg) => fg.rgb.map((c, i) => c * fg.a + bg[i] * (1 - fg.a));

                const root = getComputedStyle(document.documentElement);
                // 关键点: **文字先按自身不透明度合成到背景上**, 得到真实呈现的颜色,
                // 再拿它和背景比。直接把 alpha 混进背景色是错的 —— 会和像素实测差 1 左右。
                // **最不利背景**: 取"原画最亮的云/天空 + 暗罩"合成后的实测值。
                // 这个数来自像素级实测(有字/无字两版截图做差), 不是拍脑袋估的:
                // 用纯图片颜色 rgb(84,119,182) 会偏亮、把结论算得过严。
                const bg = [70, 102, 165];
                const readVar = (name) => {
                    const tok = parse(root.getPropertyValue(name).trim(), { rgb: [128, 128, 128], a: 1 });
                    const composited = compose(tok, bg);
                    return { css: root.getPropertyValue(name).trim(),
                             composited: composited.map(Math.round),
                             ratio: +ratio(composited, bg).toFixed(2) };
                };

                const out = {};
                for (const name of ['--text-1', '--text-2', '--text-3', '--text-4']) out[name] = readVar(name);
                return out;
            }"""
        )
        for name, info in contrast.items():
            print(f"    {name}: {info['css']}  ->  合成后 rgb{info['composited']}  "
                  f"对比度 {info['ratio']}")
        check(contrast["--text-3"]["ratio"] >= 4.5,
              "**提示文字对比度达标(≥4.5, WCAG AA)**",
              f"text-3 = {contrast['--text-3']['ratio']}")
        check(contrast["--text-2"]["ratio"] >= 4.5,
              "正文对比度达标", f"text-2 = {contrast['--text-2']['ratio']}")

        # ---- 列表项不能被压扁 ----
        # 这条来自一个真实缺陷: 分组多了以后, flex 会把每个标题压到十几像素高,
        # 一整列叠起来看着像"横向条纹", 既点不准也看不清。
        squeeze = page.evaluate(
            """() => {
                const items = [...document.querySelectorAll('#taskList .url-group')];
                if (!items.length) return { n: 0, minH: null };
                const heights = items.map(e => Math.round(e.getBoundingClientRect().height));
                return { n: items.length, minH: Math.min(...heights),
                         sample: heights.slice(0, 6) };
            }"""
        )
        if squeeze["n"]:
            check(squeeze["minH"] >= 40,
                  "**列表项没有被压扁(每项 ≥40px)**",
                  f"{squeeze['n']} 项, 最小高度 {squeeze['minH']}px")
        else:
            print(f"    (当前没有分组可检查, 跳过压扁断言)")

        page.screenshot(path=str(OUT / "31_sandbox_overview.png"))
        print("    截图 -> 31_sandbox_overview.png")

        check(not errors, "全程无前端错误", "; ".join(errors[:3]))
        browser.close()

    client.close()

    print("\n" + "=" * 62)
    if failures:
        print(f"UI 沙箱验收: 未通过 ✗ ({len(failures)}/{total})")
        for f in failures:
            print(f"  - {f}")
    else:
        print(f"UI 沙箱验收: 通过 ✓ ({total}/{total})")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
