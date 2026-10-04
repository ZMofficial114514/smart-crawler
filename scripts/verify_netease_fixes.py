"""
三项验收:
  1. iframe 站点通过 Web 接口能抓到内容(此前为 0 条)
  2. 音频数量上限: 专用键 max_audio 的优先级正确
  3. 字段语义命名: 能由 URL 路径/文案认出 artist / album / duration 等,
     而不是留下 s-fc7 这类无语义的 class 名
"""

import asyncio
import json
import sys
import urllib.request

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8322"
URL = "https://music.163.com/#/search/m/?s=on%20my%20way&type=1"

failures: list[str] = []
#: 这些是无语义的构建产物类名, 不应出现在"能判断出语义"的字段上
MEANINGLESS = {"s-fc7", "s-fc8", "td", "em", "sep", "tt", "text"}


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def api(path: str, payload: dict | None = None, method: str = "GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def wait(task_id: str, rounds: int = 120) -> dict:
    import time
    for _ in range(rounds):
        time.sleep(1.5)
        d = api(f"/api/tasks/{task_id}")
        if d.get("status") in ("success", "failed", "cancelled"):
            return d
    return {"status": "timeout"}


async def part1() -> None:
    print("\n=== 1) iframe 站点经 Web 接口抓取 ===")
    tid = (api("/api/crawl", {"url": URL, "goal": "抓取歌曲名称与链接",
                              "max_pages": 1}, "POST").get("task") or {}).get("id")
    d = wait(tid)
    res = d.get("result") or {}
    print(f"    状态={d.get('status')}  message={d.get('message')}")
    items = res.get("items_preview") or []
    print(f"    item_count={res.get('item_count')}  预览={len(items)} 条")
    for it in items[:2]:
        print(f"      {json.dumps(it, ensure_ascii=False)[:120]}")
    check(res.get("item_count", 0) > 0, "**Web 接口抓到内容**",
          f"{res.get('item_count')} 条")
    check("music.163.com" in json.dumps(items, ensure_ascii=False),
          "记录是网易云的内容(不是空外壳)")


async def part2() -> None:
    print("\n=== 2) 音频数量上限优先级 ===")
    from smartcrawler.plugins.builtin._media_transform import resolve_limit

    class Ctx:
        def __init__(self, data, config):
            self.data = data
            self.config = config

    cases = [
        # (说明, ctx.data, ctx.config, 期望)
        ("任务参数最高(下载数量=3)", {"media_limit": 3}, {"max_audio": 50}, 3),
        ("插件专用键 max_audio", {}, {"max_audio": 5}, 5),
        ("专用键优先于通用键 max_items",
         {}, {"max_audio": 5, "max_items": 200}, 5),
        ("只有通用键时用它", {}, {"max_items": 7}, 7),
        ("都没有则用内置默认", {}, {}, 50),
        ("max_audio=0 视为未设置, 退回通用键", {}, {"max_audio": 0, "max_files": 9}, 9),
    ]
    for label, data, config, expect in cases:
        got = resolve_limit(Ctx(data, config), 50, dedicated_keys=("max_audio",))
        check(got == expect, label, f"期望 {expect} 实得 {got}")

    # 音乐插件确实把 max_audio 声明进了配置表
    from smartcrawler.plugins.builtin.music_downloader import MusicDownloaderPlugin

    keys = [c["key"] for c in MusicDownloaderPlugin.config_schema]
    print(f"    音乐下载器配置项 = {keys}")
    check("max_audio" in keys, "插件配置表里有独立的音频数量项 max_audio")
    labels = {c["key"]: c.get("label", "") for c in MusicDownloaderPlugin.config_schema}
    check("音频" in labels.get("max_audio", ""), "该项名称对用户可读",
          repr(labels.get("max_audio")))


async def part3() -> None:
    print("\n=== 3) 字段语义命名 ===")
    tid = (api("/api/analyze", {"url": URL, "scroll_rounds": 0}, "POST").get("task") or {}).get("id")
    d = wait(tid)
    rep = ((d.get("result") or {}).get("report") or {})
    cands = rep.get("candidate_lists") or []
    check(bool(cands), "分析出候选列表", f"{len(cands)} 个")

    all_names: set[str] = set()
    for c in cands:
        all_names |= {f.get("name") for f in (c.get("sample_fields") or [])}

    print(f"    全部字段名 = {sorted(all_names)}")
    semantic = {"artist", "album", "user", "duration", "date", "play_count",
                "comment_count", "category"}
    hit = all_names & semantic
    check(bool(hit), "**识别出语义字段名**", f"{sorted(hit)}")
    check("artist" in all_names, "歌手字段被命名为 artist(不再是 s-fc7)",
          f"s-fc7 是否仍存在: {'s-fc7' in all_names}")

    # 语义名必须指向真实有内容的选择器(/artist 路径或文案含歌手)
    main = next((c for c in cands if "srchsongst" in str(c.get("item_selector"))), None)
    if main:
        names = [f.get("name") for f in main.get("sample_fields") or []]
        print(f"    歌曲列表字段 = {names}")
        check("artist" in names, "歌曲列表里有 artist 字段")
        check("album" in names, "歌曲列表里有 album 字段")


async def main() -> int:
    try:
        api("/api/health")
    except Exception as e:  # noqa: BLE001
        print(f"  服务不可用: {e}")
        return 1
    await part1()
    await part2()
    await part3()

    print("\n" + "=" * 70)
    if failures:
        print(f"三项验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("三项验收: 通过 ✓")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
