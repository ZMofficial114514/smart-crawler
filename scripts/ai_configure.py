"""
ai_configure.py —— SmartCrawler 的 AI 接口配置与连通性测试工具。

配套的图形化入口在 Web 控制台「系统配置」页(可测试连通性、看响应延迟)。
本脚本适合无图形环境、CI 或想快速写 `.env` 的场景。

功能:
    1. --show   显示当前 AI 配置(Key 脱敏);
    2. --test   实测连通性(发一条最小请求);
    3. --set    把 AI 配置写入 .env(自动创建/更新), 例如:
                   python scripts/ai_configure.py --set base_url=https://api.deepseek.com model=deepseek-flash
                   python scripts/ai_configure.py --set api_key=sk-xxxx
    4. --preset 一键套用服务商预设(openai/deepseek/qwen/glm/moonshot/siliconflow/ollama);
    5. --offline 打开/关闭离线模式(纯规则引擎, 不调用 AI)。

支持的 provider:
    - openai : 任何 OpenAI 兼容接口 —— OpenAI / DeepSeek / 通义千问(DashScope 兼容模式)
               / 智谱 GLM / Moonshot / 硅基流动 等, 只需改 base_url + model;
    - ollama : 本地 Ollama, base_url=http://localhost:11434/v1, 无需 Key。

运行方式: python scripts/ai_configure.py [--show | --test | --set k=v ...] [--offline on|off]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 项目根目录(本文件位于 <ROOT>/scripts/), 保证能 import 到 smartcrawler 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from smartcrawler.config import Settings, get_settings  # noqa: E402
from smartcrawler.ai import AIClient  # noqa: E402
from smartcrawler.utils import setup_logging  # noqa: E402

ENV_FILE = PROJECT_ROOT / ".env"

# 各家服务商的常用预设(方便 --preset 一键切换)
PRESETS: dict[str, dict[str, str]] = {
    "openai":    {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "deepseek":  {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "qwen":      {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "glm":       {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    "moonshot":  {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct"},
    "ollama":    {"base_url": "http://localhost:11434/v1", "model": "qwen2.5", "provider": "ollama"},
}


def mask(key: str) -> str:
    """API Key 脱敏显示。"""
    if not key:
        return "(未设置)"
    return f"{key[:6]}****{key[-4:]}" if len(key) > 12 else "****"


def show_config(settings: Settings) -> None:
    ai = settings.ai
    effective = ai.effective_base_url()
    print("┌───────────── AI 配置 ─────────────")
    print(f"│ enabled   : {ai.enabled}")
    print(f"│ offline   : {ai.offline}   (True=纯规则引擎, 不调用 AI)")
    print(f"│ provider  : {ai.provider}")
    print(f"│ base_url  : {ai.base_url}")
    if effective != ai.base_url.rstrip("/"):
        # 常见误填: 只写了裸域名。请求会挂在版本段之下, 这里给出纠正后的地址
        print(f"│   → 实际使用: {effective}   (已自动补全版本段)")
    print(f"│ model     : {ai.model}")
    print(f"│ api_key   : {mask(ai.resolve_api_key())}")
    print(f"│ cache     : {ai.cache_enabled} -> {ai.cache_path}")
    print(f"│ timeout   : {ai.timeout}s, retries={ai.max_retries}")
    print("└───────────────────────────────────")
    if ai.provider == "openai" and not ai.resolve_api_key():
        print("\n⚠ 尚未配置 API Key。设置方式任选:")
        print("   1) python scripts/ai_configure.py --set api_key=sk-xxx")
        print("   2) python scripts/ai_configure.py --preset deepseek")
        print("   3) 环境变量 OPENAI_API_KEY / DEEPSEEK_API_KEY 等")
        print("   4) Web 控制台 → 系统配置 → AI 辅助 填写并点击「测试 AI 连通性」")
    if ai.provider == "openai":
        print("\n可用预设(python scripts/ai_configure.py --preset deepseek):", ", ".join(PRESETS))


async def test_connection(settings: Settings) -> int:
    """发一条最小请求验证配置是否可用。"""
    ai = settings.ai
    if ai.provider == "openai" and not ai.resolve_api_key():
        print("✗ 未配置 API Key, 无法测试。请先: python scripts/ai_configure.py --set api_key=sk-xxx")
        return 1
    client = AIClient(ai)
    print(f"测试中: {ai.provider} / {ai.model} @ {ai.effective_base_url()} ...")
    reply = await client.chat(
        [{"role": "user", "content": "只回复两个字: 连通"}],
        temperature=0.0,
        # 注意: 不能用很小的预算。deepseek-flash / deepseek-reasoner 等推理模型会先输出
        # reasoning_content, 预算太小(如 16)会被推理全部耗尽, 导致 content 为空串、
        # finish_reason=length —— 看起来像"连接失败", 实际接口是通的。
        max_tokens=512,
    )
    if reply:
        print(f"✓ 连通成功, 模型回复: {reply.strip()[:50]}")
        return 0
    if ai.provider == "openai":
        print(
            "✗ 未拿到回复。若上方日志没有网络错误, 多半是 max_tokens 预算被推理过程耗尽:\n"
            "   请调大 max_tokens(建议 >=256)或改用非推理模型(如 deepseek-chat)。"
        )
    print("✗ 连通失败, 请检查 base_url / model / api_key / 网络(详见上方日志)")
    return 1


def update_env(updates: dict[str, str]) -> None:
    """把 SC_AI__XXX 配置写入 .env(存在则逐行替换, 不存在则创建)。"""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        lines = [l for l in lines if not any(l.startswith(k + "=") for k in updates)]
        if lines and lines[-1].strip():
            lines.append("")
    for k, v in updates.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ 已写入 {ENV_FILE}:")
    for k, v in updates.items():
        shown = v if "KEY" not in k else mask(v)
        print(f"    {k}={shown}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartCrawler AI API 配置工具")
    parser.add_argument("--show", action="store_true", help="显示当前 AI 配置")
    parser.add_argument("--test", action="store_true", help="测试 AI 连通性")
    parser.add_argument("--set", nargs="+", metavar="KEY=VALUE", help="写入 .env, 如 base_url=... model=... api_key=...")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="一键应用服务商预设")
    parser.add_argument("--offline", choices=["on", "off"], help="开关离线模式(纯规则引擎)")
    args = parser.parse_args()

    setup_logging("WARNING", log_file="")  # 工具模式: 只输出到控制台
    settings = get_settings(reload=True)
    updates: dict[str, str] = {}

    if args.preset:
        preset = PRESETS[args.preset]
        for k, v in preset.items():
            key = {"base_url": "SC_AI__BASE_URL", "model": "SC_AI__MODEL", "provider": "SC_AI__PROVIDER"}[k]
            updates[key] = v
    if args.set:
        for item in args.set:
            if "=" not in item:
                print(f"忽略非法参数(应为 KEY=VALUE): {item}")
                continue
            k, _, v = item.partition("=")
            k = k.strip().lower().replace("-", "_")
            if not k.startswith("SC_AI__"):
                k = f"SC_AI__{k.upper()}"
            updates[k] = v.strip()
    if args.offline:
        updates["SC_AI__OFFLINE"] = "true" if args.offline == "on" else "false"

    if updates:
        update_env(updates)
        settings = get_settings(reload=True)

    if args.show or not (args.test or updates or args.offline):
        show_config(settings)
    if args.test:
        return asyncio.run(test_connection(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
