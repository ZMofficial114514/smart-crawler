"""
端到端验收脚本: 通过 Web API 发起一次真实抓取, 全程观察 WebSocket 事件与实时日志。

覆盖链路: POST /api/crawl -> 任务登记 -> 浏览器启动 -> robots 检查 -> 导航 ->
结构分析 -> AI 生成规则(或规则引擎降级) -> 提取 -> 去重 -> 落盘 -> 任务结束 ->
结果下载。这是对"界面能用"最有说服力的验证。

用法: python scripts/e2e_acceptance.py [目标URL]
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.web.__main__ import prepare_temp_dir  # noqa: E402

prepare_temp_dir()

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8322"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://books.toscrape.com"
TIMEOUT = 300.0
TERMINAL = {"success", "failed", "cancelled"}


async def stream_task(task_id: str, result_box: dict, log_lines: list[str]) -> None:
    """订阅任务事件流, 打印里程碑, 把 result 事件收集下来。

    同时用一个轮询兜底: 若任务在订阅前就已经结束(或终端事件丢失), 事件流不会再有
    输出, 单纯等 recv 会挂到超时。因此每轮都顺手查一次任务状态作为终止条件。

    另外记录**收到的事件序列**, 用于断言"先 result 后终态 status"这一不变量 ——
    曾经因为成功路径不发终态 status, 界面上的任务永远显示"进行中"。
    """
    import websockets

    url = f"ws://127.0.0.1:8322/ws/tasks/{task_id}"
    terminal = {"success", "failed", "cancelled"}
    events: list[str] = result_box.setdefault("events", [])

    async def poll_until_done() -> None:
        """兜底轮询: 任务进入终态就设置终止标志。"""
        import httpx

        while "final" not in result_box:
            await asyncio.sleep(2.0)
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    detail = (await client.get(f"{BASE}/api/tasks/{task_id}")).json()
                if detail.get("status") in terminal:
                    result_box.setdefault("final", detail["status"])
                    return
            except Exception:  # noqa: BLE001
                continue

    poller = asyncio.create_task(poll_until_done())
    try:
        async with websockets.connect(url, open_timeout=20, ping_interval=20) as ws:
            while "final" not in result_box:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "snapshot":
                    task = event["task"]
                    events.append("snapshot")
                    print(f"  [snapshot] 状态={task['status']} 步骤={len(task['steps'])}")
                    if task["status"] in terminal:
                        result_box["final"] = task["status"]
                        if task.get("result"):
                            result_box["payload"] = task["result"]
                elif kind == "status":
                    events.append(f"status:{event.get('status')}")
                    print(f"  [status ] {event.get('status')} — {event.get('message', '')}")
                    if event.get("status") in terminal:
                        result_box["final"] = event["status"]
                elif kind == "warning":
                    events.append("warning")
                    print(f"  [warning] {event.get('message')}")
                elif kind == "result":
                    events.append("result")
                    payload = event["payload"]
                    result_box["payload"] = payload
                    print(f"  [result ] {payload.get('item_count')} 条 / {payload.get('pages_crawled')} 页")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] 事件流中断: {type(exc).__name__}: {exc}")
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


async def stream_logs(log_lines: list[str], stop: asyncio.Event) -> None:
    """订阅全局日志流, 只挑关键行打印。"""
    import websockets

    keywords = ("AI 模式", "限速", "第 ", "已保存", "任务", "降级", "AI 调用", "代理", "无法", "失败", "错误", "robots")
    try:
        async with websockets.connect("ws://127.0.0.1:8322/ws/logs", open_timeout=20, ping_interval=20) as ws:
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    continue
                event = json.loads(raw)
                if event.get("type") != "log":
                    continue
                msg = event.get("message", "")
                log_lines.append(msg)
                if any(k in msg for k in keywords):
                    print(f"    │ {event.get('level', ''):<7} {msg[:130]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] 日志流中断: {type(exc).__name__}: {exc}")


async def main() -> int:
    async with httpx.AsyncClient(timeout=30.0) as client:
        health = (await client.get(f"{BASE}/api/health")).json()
        print(f"服务健康: {health['status']} | AI={health['ai']['available']} ({health['ai']['model']}) "
              f"| robots={health['compliance']['respect_robots']}\n")

        print(f"提交抓取任务: {TARGET}")
        response = await client.post(
            f"{BASE}/api/crawl",
            json={
                "url": TARGET,
                "goal": "抓取所有书籍的名称、价格和详情页链接",
                "format": "json",
                "max_pages": 2,
                "use_ai": True,
                "wait": 0.5,
            },
        )
        if response.status_code != 202:
            print(f"提交失败 HTTP {response.status_code}: {response.text[:400]}")
            return 1
        task = response.json()["task"]
        task_id = task["id"]
        print(f"任务已受理: id={task_id} 步骤={[s['label'] for s in task['steps']]}\n")

    result_box: dict = {}
    log_lines: list[str] = []
    stop = asyncio.Event()
    started = time.perf_counter()

    log_task = asyncio.create_task(stream_logs(log_lines, stop))
    await stream_task(task_id, result_box, log_lines)
    stop.set()
    log_task.cancel()
    await asyncio.gather(log_task, return_exceptions=True)

    elapsed = time.perf_counter() - started
    print(f"\n任务耗时(客户端观测): {elapsed:.1f}s")

    # 拉取任务最终状态
    async with httpx.AsyncClient(timeout=30.0) as client:
        detail = (await client.get(f"{BASE}/api/tasks/{task_id}")).json()
        print(f"最终状态: {detail['status']} | {detail.get('message', '')}")
        print(f"步骤结果:")
        for step in detail["steps"]:
            print(f"  {step['status']:<8} {step['label']:<12} {step['detail']}")

        if detail.get("errors"):
            print("错误/提示:")
            for err in detail["errors"]:
                print(f"  - {err}")

        payload = result_box.get("payload") or {}
        items = payload.get("items_preview") or []
        print(f"\n提取结果: {payload.get('item_count')} 条, 预览 {len(items)} 条, "
              f"规则来源={((payload.get('rule') or {}).get('source'))}")
        if items:
            print("前 3 条:")
            for row in items[:3]:
                print(f"  {json.dumps(row, ensure_ascii=False)[:170]}")

        # 验证产物下载
        if detail.get("artifacts"):
            artifact = detail["artifacts"][0]
            print(f"\n产物: {artifact['label']} -> {artifact['name']}")
            dl = await client.get(f"{BASE}/api/tasks/{task_id}/download?which=artifact")
            print(f"  下载 HTTP {dl.status_code}, {len(dl.content)} 字节")
            try:
                parsed = json.loads(dl.content)
                if isinstance(parsed, dict):
                    full = (parsed.get("result") or {}).get("items") or []
                elif isinstance(parsed, list):
                    full = parsed
                else:
                    full = []
                print(f"  落盘完整条目数: {len(full)}")
            except json.JSONDecodeError:
                print("  [!] 产物不是合法 JSON")

        # 验证配置读写
        cfg = (await client.get(f"{BASE}/api/config")).json()
        print(f"\n配置接口: {sum(len(s['fields']) for s in cfg['schema']['sections'])} 个字段已暴露")

        # 验证分页读取任务条目
        page = (await client.get(f"{BASE}/api/tasks/{task_id}/items?offset=0&limit=5")).json()
        print(f"条目前 5 条: total={page['total']} 返回={len(page['items'])}")

        # 验证规则校验接口
        rule_check = (
            await client.post(
                f"{BASE}/api/rules/validate",
                json={"rule": '{"mode":"dom","list_rule":{"item_selector":"li","fields":[{"name":"t","selector":"a"}]}}'},
            )
        ).json()
        print(f"规则校验接口: ok={rule_check.get('ok')} 字段数={rule_check.get('field_count')}")

        files = (await client.get(f"{BASE}/api/files")).json()["files"]
        print(f"产出文件接口: {len(files)} 个文件")
        for f in files[:5]:
            print(f"  {f['dir']}/{f['name']} ({f['size']} 字节)")

    # ---- 断言: 事件序列必须满足"先 result 后终态 status" ----
    # 这是界面上"任务永远显示进行中"那个 bug 的根因判据, 固化成验收项。
    events = result_box.get("events", [])
    print(f"\n事件序列: {' → '.join(events) or '(无)'}")
    terminal_events = [e for e in events if e.startswith("status:") and e.split(":", 1)[1] in TERMINAL]
    result_seen = "result" in events
    order_ok = bool(terminal_events) and (
        not result_seen or events.index("result") < events.index(terminal_events[0])
    )
    print(f"  收到 result 事件      : {result_seen}")
    print(f"  收到终态 status 事件  : {terminal_events or '无'}")
    print(f"  顺序正确(先 result)   : {order_ok}")
    if not order_ok:
        print("  ✗ 终态 status 缺失或顺序错误 —— 界面会一直显示『进行中』")

    ok = (
        detail["status"] == "success"
        and payload.get("item_count", 0) > 0
        and order_ok
    )
    print("\n" + ("=" * 62))
    print("验收结果: " + ("通过 ✓ 抓取链路完整可用" if ok else "未通过 ✗ 详见上方输出"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
