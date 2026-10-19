"""
验收: 下载数量上限的优先级 —— **任务参数不能被插件配置反向覆盖**。

用户报障: 网易云搜索抓到 30 首, 界面「下载数量」默认 50, 实际只下了 20 首。
根因: 网易云下载器自己算上限, 写的是

    limit = min(插件配置 max_audio(20), 任务参数 media_limit(50))  ->  20

`min` 只会取更小的那个, 于是**插件配置永远压着任务参数**。而框架里其它下载器都走
``resolve_limit()``, 其约定是「任务参数 > 插件专用配置 > 通用配置 > 内置默认」,
任务参数代表本次任务的意图, 应当直接生效。

这个验收**不真的下载**, 只把下载入口换成记录器, 断言"实际会尝试几首"。
同时覆盖三种优先级, 防止以后有人又改回 min():
  1. 任务参数 50 > 插件 20  -> 应尝试 30 首(全部, 因为只有 30 首)
  2. 任务参数 5  < 插件 20  -> 应尝试 5 首(任务参数也能往小压)
  3. 没有任务参数           -> 落到插件配置 20
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def make_items(n: int) -> list[dict[str, Any]]:
    """n 条带 /song?id= 链接的记录, 形态与真实抓取结果一致。"""
    return [{"song_name": f"歌曲 {i}", "link": f"/song?id={100000 + i}"}
            for i in range(1, n + 1)]


def make_ctx(settings: Any, cfg: dict, data: dict, outdir: Path,
             sink: list[tuple[str, str]] | None = None) -> Any:
    """构造**真实的** PluginContext(dataclass), 只覆写 notify/output_dir。

    用真类而不是自造的假对象: 之前用手写 FakeCtx 就漏了 `url` 字段, 一调就
    AttributeError —— 那种失败是测试自己的问题, 会把真实 bug 掩盖掉。
    """
    from smartcrawler.plugins.base import PluginContext  # noqa: PLC0415

    ctx = PluginContext(
        settings=settings,
        url="https://music.163.com/#/search/m/?s=test&type=1",
        config=dict(cfg),
        data=dict(data),
    )
    ctx.output_dir = outdir

    def _notify(level: str, message: str) -> None:
        if sink is not None:
            sink.append((str(level), str(message)))

    ctx.notify = _notify
    return ctx


async def main() -> int:
    import smartcrawler.plugins.builtin.netease_downloader as nd  # noqa: PLC0415
    from smartcrawler.config import get_settings  # noqa: PLC0415

    plugin = nd.NeteaseMusicPlugin() if hasattr(nd, "NeteaseMusicPlugin") else None
    if plugin is None:
        # 类名可能不同, 按 after_extract 定位
        cands = [v for k, v in vars(nd).items()
                 if isinstance(v, type) and hasattr(v, "after_extract")]
        if not cands:
            print("  找不到网易云插件类")
            return 1
        plugin = cands[0]()
    print(f"  插件类: {type(plugin).__name__}")
    settings = get_settings()

    captured: list[int] = []

    # 把取地址这一步换成记录器: 它收到的 song_ids 就是"实际会下载的条目"
    def fake_fetch(ctx, song_ids, jar):  # noqa: ANN001
        captured.append(len(song_ids))
        return {}                    # 空 -> after_extract 会提前返回, 不真的下载

    plugin._fetch_urls = fake_fetch   # type: ignore[method-assign]

    # 会话文件要能读出 MUSIC_U, 否则会在下载前就返回
    nd._cookies_from_storage_state = lambda _p: {"MUSIC_U": "fake"}  # type: ignore[assignment]

    cases = [
        ("任务参数 50 > 插件 20, 共 30 首", {"max_audio": 20}, {"media_limit": 50}, 30),
        ("任务参数 5  < 插件 20", {"max_audio": 20}, {"media_limit": 5}, 5),
        ("没有任务参数 -> 用插件配置 20", {"max_audio": 20}, {}, 20),
        ("任务参数 100, 共 30 首 -> 全要", {}, {"media_limit": 100}, 30),
    ]

    for label, cfg, data, expect in cases:
        captured.clear()
        msgs: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as td:
            ctx = make_ctx(settings, cfg, data, Path(td), sink=msgs)
            try:
                await plugin.after_extract(ctx, make_items(30))
            except Exception as exc:  # noqa: BLE001
                print(f"\n  [{label}] 抛异常: {type(exc).__name__}: {exc}")
                check(False, label, f"{type(exc).__name__}")
                continue
        got = captured[0] if captured else -1
        print(f"\n  [{label}]")
        print(f"      config={cfg} data={data} -> 实际尝试 {got} 首")
        check(got == expect, label, f"期望 {expect}, 实际 {got}")

        # 被截断时必须有一条说明, 且要交代上限来自哪里
        truncated = 30 > expect
        hinted = [m for _l, m in msgs if "按上限下载" in m]
        if truncated:
            check(bool(hinted),
                  f"{label}: **截断有说明**", hinted[0] if hinted else "没有提示")
            if hinted:
                check(f"共 30 首" in hinted[0] and str(expect) in hinted[0],
                      f"{label}: 说明里有总数与上限", hinted[0])
        else:
            check(not hinted, f"{label}: 未截断时不打扰", f"{len(hinted)} 条")

    print("\n" + "=" * 68)
    if failures:
        print(f"下载数量优先级验收: 未通过 ✗ ({len(failures)} 项)")
        for f in failures:
            print(f"  - {f}")
    else:
        print("下载数量优先级验收: 通过 ✓")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
