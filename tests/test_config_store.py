"""
配置层单元测试。

覆盖三处最容易出错的逻辑:
1. Schema 内省 —— 必须把 Settings 里的每个字段都暴露出来, 漏字段意味着界面上
   改不了某项配置;
2. .env 写入 —— 必须保留原有注释与顺序, 且空列表要写成 `[]`(否则
   pydantic-settings 会把空字符串当解析失败);
3. 类型归一化 —— 前端传来的字符串要正确变成 bool/int/float/list。

运行: python -m pytest tests/ -q    (需要 pytest; 也可直接 python tests/test_config_store.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from smartcrawler.config import Settings, get_settings  # noqa: E402
from smartcrawler.web.config_store import (  # noqa: E402
    EnvConfigStore,
    build_config_schema,
    coerce_value,
    encode_env_literal,
    flatten_settings,
    is_sensitive,
    mask_secret,
    path_to_env_key,
)


# ---------------------------------------------------------------------------
# Schema 内省
# ---------------------------------------------------------------------------
def test_schema_covers_every_field():
    """Schema 里的字段集合必须与 Settings 完全一致。"""
    settings = get_settings()
    schema = build_config_schema(settings)
    paths = {field["path"] for section in schema["sections"] for field in section["fields"]}

    expected = set(flatten_settings(settings.model_dump(mode="json")).keys())
    assert paths == expected, f"Schema 缺失: {expected - paths}; 多余: {paths - expected}"


def test_schema_has_labels_and_control_types():
    schema = build_config_schema(get_settings())
    for section in schema["sections"]:
        assert section["fields"], f"分组 {section['key']} 没有字段"
        for field in section["fields"]:
            assert field["type"] in {"bool", "int", "float", "str", "textarea", "enum", "list", "json"}
            assert field["label"], f"{field['path']} 缺少中文标签"
            assert field["default"] is not None or field["type"] == "str"


def test_enum_and_numeric_constraints_are_extracted():
    schema = build_config_schema(get_settings())
    fields = {f["path"]: f for section in schema["sections"] for f in section["fields"]}

    assert sorted(fields["browser.engine"]["options"]) == ["chromium", "firefox", "webkit"]
    assert sorted(fields["storage.default_format"]["options"]) == ["csv", "json", "jsonl", "sqlite"]
    # crawler.max_depth 声明了 ge=1
    assert fields["crawler.max_depth"]["min"] == 1
    # 列表字段
    assert fields["anti_spider.random_delay_range"]["type"] == "list"
    assert fields["anti_spider.capture_resource_types" if "anti_spider.capture_resource_types" in fields else "network.capture_resource_types"]["type"] == "list"


def test_sensitive_fields_detected():
    assert is_sensitive("ai.api_key")
    assert is_sensitive("ai.some_api_key")
    assert not is_sensitive("ai.model")


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------
def test_mask_secret_keeps_edges_only():
    assert mask_secret("sk-1234567890abcdef") == "sk-1********cdef"
    assert mask_secret("") == ""
    assert mask_secret("short") == "*****"
    # 掩码后的值必须能被 apply_patch 识别(含连续星号)
    assert "****" in mask_secret("sk-1234567890abcdef")


# ---------------------------------------------------------------------------
# 类型归一化
# ---------------------------------------------------------------------------
def test_coerce_value_all_kinds():
    assert coerce_value("bool", "true") is True
    assert coerce_value("bool", "false") is False
    assert coerce_value("bool", "on") is True
    assert coerce_value("bool", True) is True

    assert coerce_value("int", "42") == 42
    assert coerce_value("int", 42) == 42
    assert coerce_value("float", "1.5") == 1.5

    assert coerce_value("list", "[1, 3]") == [1, 3]
    assert coerce_value("list", "a, b ,c") == ["a", "b", "c"]
    assert coerce_value("list", "") == []
    assert coerce_value("list", ["x"]) == ["x"]

    assert coerce_value("str", 123) == "123"


def test_coerce_value_rejects_bad_input():
    for kind, bad in (("int", "abc"), ("int", ""), ("float", "x")):
        try:
            coerce_value(kind, bad)
        except ValueError:
            continue
        raise AssertionError(f"coerce_value({kind!r}, {bad!r}) 应当抛出 ValueError")


def test_encode_env_literal():
    assert encode_env_literal("bool", True) == "true"
    assert encode_env_literal("bool", False) == "false"
    assert encode_env_literal("list", [1, 3]) == "[1, 3]"
    assert encode_env_literal("list", []) == "[]"
    assert encode_env_literal("int", 5) == "5"


def test_path_to_env_key():
    assert path_to_env_key("ai.base_url") == "SC_AI__BASE_URL"
    assert path_to_env_key("browser.headless") == "SC_BROWSER__HEADLESS"
    assert path_to_env_key("log_level") == "SC_LOG_LEVEL"


# ---------------------------------------------------------------------------
# .env 读写
# ---------------------------------------------------------------------------
def test_env_store_roundtrip_preserves_comments(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "# 顶部注释\n"
        "SC_AI__MODEL=old-model\n"
        "\n"
        "# 中间注释\n"
        "SC_BROWSER__HEADLESS=false\n",
        encoding="utf-8",
    )
    store = EnvConfigStore(env)

    assert store.as_dict()["SC_AI__MODEL"] == "old-model"

    store.write({"SC_AI__MODEL": "new-model", "SC_LOG_LEVEL": "DEBUG"})
    lines = store.raw_lines()
    text = "\n".join(lines)

    # 注释与顺序保留
    assert "# 顶部注释" in text
    assert "# 中间注释" in text
    assert lines.index("SC_AI__MODEL=new-model") < lines.index("SC_BROWSER__HEADLESS=false")
    # 新键被追加
    assert "SC_LOG_LEVEL=DEBUG" in text
    # 反复写入不会重复追加
    store.write({"SC_AI__MODEL": "third-model"})
    assert "\n".join(store.raw_lines()).count("SC_AI__MODEL=") == 1


def test_env_store_removal(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SC_AI__MODEL=x\nSC_AI__API_KEY=secret\n", encoding="utf-8")
    store = EnvConfigStore(env)
    store.write({}, removals=["SC_AI__API_KEY"])
    assert "SC_AI__API_KEY" not in "\n".join(store.raw_lines())
    assert store.as_dict()["SC_AI__MODEL"] == "x"


def test_env_store_handles_missing_file(tmp_path: Path):
    store = EnvConfigStore(tmp_path / "nope.env")
    assert store.as_dict() == {}
    assert store.raw_lines() == []


# ---------------------------------------------------------------------------
# Settings 行为
# ---------------------------------------------------------------------------
def test_ai_base_url_normalization():
    """裸域名要补上 /v1, 已带版本段的保持不变。"""
    assert Settings(ai={"base_url": "https://api.deepseek.com"}).ai.effective_base_url() == "https://api.deepseek.com/v1"
    assert Settings(ai={"base_url": "https://api.deepseek.com/"}).ai.effective_base_url() == "https://api.deepseek.com/v1"
    assert Settings(ai={"base_url": "https://api.openai.com/v1"}).ai.effective_base_url() == "https://api.openai.com/v1"
    assert (
        Settings(ai={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}).ai.effective_base_url()
        == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    assert Settings(ai={"base_url": ""}).ai.effective_base_url() == "https://api.openai.com/v1"


def test_project_root_points_at_package_parent():
    from smartcrawler.config import PROJECT_ROOT

    assert (PROJECT_ROOT / "smartcrawler" / "config.py").exists()


# ---------------------------------------------------------------------------
# 直接运行(无 pytest 时的降级路径)
# ---------------------------------------------------------------------------
def _run_manually() -> int:
    import inspect
    import tempfile
    import traceback

    tests = [
        (name, func)
        for name, func in sorted(globals().items())
        if name.startswith("test_") and callable(func)
    ]
    passed = failed = 0
    for name, func in tests:
        kwargs = {}
        if "tmp_path" in inspect.signature(func).parameters:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp(prefix="sc_test_"))
        try:
            func(**kwargs)
            passed += 1
            print(f"  PASS  {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_manually())
