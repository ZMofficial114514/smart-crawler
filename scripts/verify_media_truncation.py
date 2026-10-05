"""下载大小上限的回归验收(离线可跑, 不需要浏览器/网络)。

原始场景: 某次下载在磁盘上留下一个 **512 MiB 的 mp4 残片**, 大小恰好等于当时的
`max_file_size_mb: 512`, `ffprobe` 报 `moov atom not found` —— 文件播不了, 但记录
里是"下载成功"。

根因是 :func:`smartcrawler.plugins.builtin._media.download_many` 里的顺序问题:

1. 异常路径的清理依赖 `record.path`, 而旧代码**写盘成功后**才给它赋值, 于是撞上限
   抛异常时清理被整段跳过;
2. 大小判断写成"先写后判"(`if size > max: raise` 在 `write` 之后), 落盘尺寸会变成
   **上限 + 1 个 chunk**, 而不是干净地放弃;
3. 报错信息把字节整除成 MB, 小于 1MB 的一律显示 `0MB`, 看不出到底差多少。

这里把三条都固化成断言。**故意不使用 Content-Length** —— 有它的话新版会在写盘前
就放弃, 旧版的"写了一半"路径反而测不到。

用法: python scripts/verify_media_truncation.py
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smartcrawler.plugins.base import PluginContext  # noqa: E402
from smartcrawler.plugins.builtin._media import download_many  # noqa: E402

#: 每次 `aiter_bytes` 产出的字节数。上限取这个值的整数倍, 断言里就能看出
#: "上限 + 1 个 chunk" 这种越界写法。
CHUNK = 64 * 1024

#: 响应体总大小: 远大于下面设置的上限
BODY = 2 * 1024 * 1024

failures: list[str] = []
total = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global total
    total += 1
    print(f"  {'[OK]' if condition else '[FAIL]'} {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# 靶站: 三个端点分别覆盖"不带 Content-Length""带 Content-Length""正常小文件"
# ---------------------------------------------------------------------------
class Site:
    def __init__(self) -> None:
        wait, declared, small = "/no-length.bin", "/declared.bin", "/small.bin"

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path == wait:
                    self._stream(BODY, declare=False)
                elif path == declared:
                    self._stream(BODY, declare=True)
                elif path == small:
                    payload = b"x" * 4096
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def _stream(self, size: int, *, declare: bool) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if declare:
                    self.send_header("Content-Length", str(size))
                # 不声明长度时必须用 chunked, 否则客户端读到 EOF 才结束
                self.send_header("Connection", "close")
                self.end_headers()
                sent = 0
                while sent < size:
                    n = min(CHUNK, size - sent)
                    self.wfile.write(b"x" * n)
                    sent += n

            def log_message(self, *a):  # noqa: A002 - 静音访问日志
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()


def make_ctx(output_dir: Path) -> PluginContext:
    """最小上下文: 只带 `download_many` 真正会读到的字段。

    刻意走真实的 `Settings()` 而不是伪造 settings —— 它默认不依赖 .env, 而伪造对象
    会让"以后新增一个被读取的字段"变成静默失效的测试。
    """
    from smartcrawler.config import get_settings

    return PluginContext(settings=get_settings(), url="http://127.0.0.1/", output_dir=output_dir)


async def main() -> int:
    import tempfile

    # 刻意用 1.5 MiB 这种非整数 MB 的上限: 修复前 `max // 1024 // 1024` 会把它显示成
    # `1MB`, 而真正想防的是"小于 1MB 一律显示 0MB"。取整数 MB 会让断言失去区分度。
    limit = 1536 * 1024  # 1.5 MiB
    site = Site()
    print(f"靶站: {site.base} (响应体 {BODY // 1024} KiB, 单文件上限 {limit / 1024 / 1024:.1f} MiB)\n")

    try:
        with tempfile.TemporaryDirectory(prefix="sc_media_trunc_") as tmp:
            out = Path(tmp)

            # ==============================================================
            print("=== 1) 流式超限(服务端不声明长度): 记录失败且不留残片 ===")
            ctx = make_ctx(out)
            results = await download_many(
                ctx,
                [(f"{site.base}/no-length.bin", None)],
                plugin_id="verify-truncation",
                subdir="stream",
                max_file_size=limit,
                concurrency=1,
            )
            record = results[0] if results else None
            check(record is not None, "拿到了下载记录")
            if record is not None:
                reason = str(record.error)
                check(not record.ok, "超限文件被判定为失败", f"ok={record.ok} error={reason}")
                check(bool(reason), "失败原因非空", reason)
                # 旧实现只报"上限是多少", 不报"已经写了多少", 于是"文件多大才撞上限"
                # 只能靠翻磁盘猜。修复后带上已写入量, 且格式化成人类可读单位。
                check(
                    "已写入" in reason,
                    "**报错里说明已写入多少(修复前只有上限)**",
                    reason,
                )
                check(
                    "0MB" not in reason,
                    "**报错不再把小于 1MB 的尺寸显示成 0MB**",
                    reason,
                )
                leftover = Path(record.path) if record.path else None
                check(
                    leftover is None or not leftover.exists(),
                    "记录里的半成品路径已失效(文件真被删掉)",
                    str(leftover),
                )

            written = sorted(p.name for p in (out / "stream").glob("*")) if (out / "stream").exists() else []
            check(not written, "**产物目录里没有留下任何半成品**", str(written))

            # ==============================================================
            print("\n=== 2) 声明 Content-Length 且超限: 一个字节都不写 ===")
            ctx2 = make_ctx(out)
            results2 = await download_many(
                ctx2,
                [(f"{site.base}/declared.bin", None)],
                plugin_id="verify-truncation",
                subdir="declared",
                max_file_size=limit,
                concurrency=1,
            )
            record2 = results2[0] if results2 else None
            check(record2 is not None and not record2.ok, "声明超限时直接放弃", str(record2 and record2.error))
            created = list((out / "declared").glob("*")) if (out / "declared").exists() else []
            check(not created, "预检阶段没有创建文件", str([p.name for p in created]))

            # ==============================================================
            print("\n=== 3) 未超限: 正常下载并记录大小 ===")
            ctx3 = make_ctx(out)
            results3 = await download_many(
                ctx3,
                [(f"{site.base}/small.bin", None)],
                plugin_id="verify-truncation",
                subdir="small",
                max_file_size=limit,
                concurrency=1,
            )
            record3 = results3[0] if results3 else None
            check(record3 is not None and record3.ok, "小文件下载成功", str(record3 and record3.error))
            if record3 is not None and record3.ok:
                check(record3.size == 4096, "记录的大小与实际字节数一致", str(record3.size))
                check(
                    bool(record3.path) and Path(record3.path).exists(),
                    "成功产物保留在磁盘上",
                    str(record3.path),
                )

            # ==============================================================
            print("\n=== 4) 人类可读尺寸格式化 ===")
            # 单独 try: 修复前 `_human_size` 还不存在, 这一项必须能报"失败"而不是
            # 让整个脚本 ImportError 崩掉 —— 回归脚本的价值就在于修复前后都能跑完。
            try:
                from smartcrawler.plugins.builtin._media import _human_size
            except ImportError:
                check(False, "**`_human_size` 存在(报错不再显示 0MB)**", "修复前不存在")
            else:
                for value, expect in ((512, "512B"), (2048, "2.0KB"), (512 * 1024 * 1024, "512.0MB")):
                    got = _human_size(value)
                    check(got == expect, f"_human_size({value}) == {expect}", got)
    finally:
        site.stop()

    print("\n" + "=" * 62)
    if failures:
        print(f"下载大小上限回归: 未通过 ({len(failures)}/{total})")
        for item in failures:
            print(f"  - {item}")
    else:
        print(f"下载大小上限回归: 通过 ({total}/{total})")
    print("=" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
