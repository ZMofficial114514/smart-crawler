# 插件开发准则

> 本文是**写插件时的唯一参考**。所有接口、参数、返回值都按当前代码实际行为撰写,
> 并配有可运行的骨架。改动插件系统时请同步更新本文 —— 文档与代码不一致比没有文档更糟。

SmartCrawler 提供**两条并行**的扩展路径,常见需求不用写代码,特殊需求才写 Python。

| 路径 | 适合谁 | 能做什么 | 安全性 |
|---|---|---|---|
| **声明式配置** | 不想写代码 | 附加请求头、正则批量下载、补充固定字段、额外导出 CSV/JSONL | 只暴露"已知能力",无代码执行 |
| **Python 单文件插件** | 需要自定义逻辑 | 任意: 图片/音乐下载、新反爬策略、自定义清洗与存储 | ⚠️ **等同于在本机运行代码**,框架不提供沙箱 |

---

## 目录

1. [5 分钟做一个插件](#1-5-分钟做一个插件)
2. [三条硬性准则](#2-三条硬性准则)
3. [插件元信息](#3-插件元信息)
4. [生命周期钩子](#4-生命周期钩子)
5. [上下文对象 ctx](#5-上下文对象-ctx)
6. [声明配置项](#6-声明配置项)
7. [现成的辅助函数](#7-现成的辅助函数)
8. [登记下载产物](#8-登记下载产物)
9. [完整示例](#9-完整示例)
10. [调试与排错](#10-调试与排错)
11. [声明式插件能做什么](#11-声明式插件能做什么)
12. [失败隔离与安全](#12-失败隔离与安全)
13. [HTTP 接口与按任务选择插件](#13-http-接口与按任务选择插件)
14. [提交前自检清单](#14-提交前自检清单)

---

## 1. 5 分钟做一个插件

**文件放哪**: 项目根的 `plugins/` 目录,任意 `*.py`。放进即被发现,无需注册、无需改框架代码。
按文件名排序加载。

```python
# plugins/my_first.py
from smartcrawler.plugins.base import BasePlugin, PluginContext


class MyFirstPlugin(BasePlugin):
    id = "my-first"                  # 唯一标识, kebab-case
    name = "我的第一个插件"
    description = "给每条记录加一个来源标记"
    category = "cleanup"             # download / anti-bot / cleanup / storage / other
    default_enabled = False          # 交给用户显式启用

    config_schema = [
        {"key": "mark", "label": "标记值", "type": "str", "default": "hello"},
    ]

    def after_extract(self, ctx: PluginContext, items):
        mark = str(ctx.config.get("mark") or "hello")
        for item in items or []:
            item.setdefault("mark", mark)
        ctx.notify("INFO", f"已为 {len(items or [])} 条记录打上标记 {mark}")
        return items
```

**启用它**: 打开控制台 → 左侧「插件」→ 点**重新扫描** → 在卡片上打开开关。

```
python -m smartcrawler web      # 控制台默认 http://127.0.0.1:8322
```

也可以点「插件」页的**新建插件**,让界面直接生成骨架文件。

---

\\python
def on_page(self, ctx, page_no):
    pass
\
## 2. 三条硬性准则

这三条不是风格偏好,违反它们会造成真实故障:

### 准则一:钩子抛异常会被吞掉,所以要主动上报

管理器对每个钩子做**失败隔离** —— 单个插件报错只记进 `plugin_errors`,**不会中断抓取**。
这既是保护也是陷阱: 插件静默失效时不会有任何明显提示。因此:

- 自己 `try/except` 包住可能失败的 IO,并用 `ctx.notify("WARNING", ...)` 说明原因;
- 不要依赖"抛异常让用户看到"。
- **例外**: 如果插件数据必须存在才能继续,抛异常反而是对的(会被记入 `plugin_errors`),
  但要确保这不是"每个页面都抛一次"的噪音。

### 准则二:不要覆盖用户的提取结果

用户辛苦调出来的规则,不该被插件悄悄改掉。**一律用 `setdefault` 而不是 `=`**:

```python
item.setdefault("source_domain", domain)   # ✅
item["source_domain"] = domain             # ❌ 会覆盖用户规则里的同名字段
```

需要覆盖时,把它做成配置项并默认关闭。

### 准则三:先看 `ctx.data` 里有没有任务级约束

框架会把**本次任务的意图**放进 `ctx.data`,最典型的是"最多下载几个":

```python
from smartcrawler.plugins.builtin._media_transform import resolve_limit

limit = resolve_limit(ctx, 200)   # 任务参数 > 插件配置 > 200
```

优先级是刻意这样排的: 用户在抓取页填的数量、或直接写在抓取目标里的"爬取前三张",
都是**本次任务**的意图,不该被插件的长期配置覆盖。曾出现过"说好前三张、结果下了 24 张"
的问题,根因就是插件只读了自己的配置。

---

## 3. 插件元信息

用**类属性**声明即可(比实现 `info()` 更省事)。需要动态值时才覆盖 `info()`。

| 属性 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 唯一标识,**kebab-case**;留空则用类名小写。同一 id 只保留一个 |
| `name` | `str` | 界面显示名;留空则用类名 |
| `description` | `str` | 一句话说明,显示在卡片上 |
| `version` | `str` | 插件自身版本,默认 `1.0.0` |
| `author` | `str` | 作者 |
| `category` | `str` | 分组: `download` / `anti-bot` / `cleanup` / `storage` / `other` |
| `requires` | `list[str]` | 依赖的第三方包,**仅作界面提示**,框架不自动安装 |
| `tags` | `list[str]` | 标签 |
| `default_enabled` | `bool` | 框架建议的默认开关。**用户插件请保持 `False`** |
| `config_schema` | `list[dict]` | 配置项声明,见 [第 6 节](#6-声明配置项) |

---

## 4. 生命周期钩子

```
on_start(ctx)                     任务开始(一次性)
  └─ 每页重复:
       before_navigate(ctx)       导航前 —— 必须在 goto 之前(加请求头只能在这里)
       after_navigate(ctx)        页面加载后(可滚动 / 注入脚本 / 判断拦截页)
       before_extract(ctx)        提取前
       after_extract(ctx, items)  提取后(清洗 / 下载 / 补字段)   ← 最常用
       on_page(ctx)               每翻完一页
on_finish(ctx, items)             任务结束(汇总 / 额外导出)
```

### 签名(以代码为准)

| 钩子 | 调用形式 | 返回值是否生效 |
|---|---|---|
| `on_start` | `on_start(ctx)` | 忽略 |
| `before_navigate` | `before_navigate(ctx)` | 忽略 |
| `after_navigate` | `after_navigate(ctx)` | 忽略 |
| `before_extract` | `before_extract(ctx)` | 忽略 |
| `after_extract` | `after_extract(ctx, items)` | **返回 list 会替换数据集**;返回 `None` 表示只观察 |
| `on_page` | `on_page(ctx)` | 忽略 |
| `on_finish` | `on_finish(ctx, items)` | 忽略 |

> ⚠️ **`on_page` 只接收 `ctx`,不接收页号参数**。当前页号从 `ctx.page_no` 读。
> 写成 `on_page(self, ctx, page_no)` 会因参数不匹配而每次调用都报错。

### 同步 or 异步

**两种都支持**。不需要 `await` 就写普通 `def`(比如只改字典),需要网络/IO 就写
`async def`(比如下载文件、`page.evaluate`)。管理器会检测返回值是否为 awaitable 并自动适配,
不需要你操心。

```python
def after_extract(self, ctx, items):          # 纯数据改写, 同步即可
    ...

async def after_navigate(self, ctx):          # 需要 await playwright
    await ctx.page.evaluate("window.scrollTo(0, 0)")
```

### 执行顺序

- 插件之间: 按管理器中登记的顺序依次执行;
- `after_extract` 是**链式**的 —— 前一个插件的返回值会作为后一个的输入。

---

## 5. 上下文对象 ctx

`PluginContext` 是 `@dataclass`,由管理器为**每个插件单独派生**一份(注入该插件自己的配置)。

| 字段 | 类型 | 说明 |
|---|---|---|
| `ctx.settings` | `Settings` | 全局配置(代理、UA、限速、存储等) |
| `ctx.url` | `str` | 当前页面 URL(翻页时会更新) |
| `ctx.page` | `Page \| None` | Playwright 页面对象,可 `await ctx.page.evaluate(...)` |
| `ctx.page_no` | `int` | 当前页号,从 1 开始 |
| `ctx.crawler` | `SmartCrawler \| None` | 爬虫实例,可用 `ctx.crawler.browser.human_scroll(...)` 等 |
| `ctx.config` | `dict` | **本插件**的配置(已合并用户改动与默认值) |
| `ctx.data` | `dict` | **跨钩子、跨插件共享**的字典;也承载任务级约束(如 `media_limit`) |
| `ctx.downloads` | `list[DownloadedFile]` | 产物累积区,与任务结果共享同一个 list |
| `ctx.output_dir` | `Path` | `data/plugin_output/` |
| `ctx.notify(level, message)` | `Callable` | 把进度推给界面实时日志 |
| `ctx.download_path(subdir, filename)` | `→ Path` | 生成落盘路径并**自动建目录** |

### `ctx.notify` 的级别

`"DEBUG"` / `"INFO"` / `"SUCCESS"` / `"WARNING"`（另有 `"ERROR"`）。

界面表现: `WARNING` / `ERROR` 会弹提示条,其余进实时日志抽屉。**别把正常流程刷成 WARNING**
—— 那会让真正的问题淹没在噪音里。

### `ctx.data` 的约定键

| 键 | 含义 | 由谁写入 |
|---|---|---|
| `media_limit` | 本次任务的下载数量上限 | 框架(抓取页「下载数量」或抓取目标里的"前三张") |

想加自己的跨钩子状态,用一个带插件名前缀的键,避免和别人的撞车:
`ctx.data.setdefault("myplugin.cache", {})`。

---

## 6. 声明配置项

`config_schema` 里每一项都会在「插件」页自动生成控件,用户改完立刻持久化到
`data/plugins.json`。

| `type` | 界面控件 | 备注 |
|---|---|---|
| `bool` | 开关 | 读出来是 `True/False` |
| `int` / `float` | 数字输入 | 支持 `min` / `max` 即时校验 |
| `str` | 单行输入 | |
| `textarea` | 多行输入 | 适合请求头、逐行清单 |
| `enum` | 下拉框 | **必须**给 `options` |
| `list` | 逗号分隔输入 | 自动转成列表 |
| `json` | 代码编辑器 | 带 JSON 语法校验 |

字段定义支持: `key`(必填) / `label` / `type` / `default` / `description` /
`min` / `max` / `options`。

```python
config_schema: list[dict[str, Any]] = [
    {
        "key": "concurrency",
        "label": "并发数",
        "type": "int",
        "default": 6,
        "min": 1,
        "max": 32,
        "description": "过高容易被目标站点限速, 建议 4~8",
    },
    {
        "key": "mode",
        "label": "处理模式",
        "type": "enum",
        "default": "safe",
        "options": ["safe", "fast"],
    },
]
```

**读取配置的推荐写法** —— `ctx.config` 已经合并了默认值,但仍要防空:

```python
concurrency = int(ctx.config.get("concurrency") or 6)
mode = str(ctx.config.get("mode") or "safe")
```

---

## 7. 现成的辅助函数

框架已把"下载"这件事里最容易踩坑的部分实现好了,**优先复用而不是重写**。

### 7.1 并发下载:`download_many`

```python
from smartcrawler.plugins.builtin._media import download_many

results = await download_many(
    ctx,
    [(url, index), ...],              # [(绝对URL, 来源记录下标或 None)]
    plugin_id=self.id,
    subdir="files",                   # data/plugin_output/files/
    referer=ctx.url,
    max_file_size=20 * 1024 * 1024,
    concurrency=6,
    allowed_types=("image/",),        # 只收这些 Content-Type 前缀; 空则放行
    filename_prefix="",               # 文件名前缀
)
```

它替你处理了这些**实战坑**:

- `asyncio.Semaphore` 限流(默认 6 路),避免把对方打崩;
- **自动带 `Referer`** —— 大量图床没有 Referer 会返回 403 或占位图;
- 不做 HEAD 预检(很多站点不支持),直接流式 GET 并按 `max_file_size` **超限即弃**;
- 扩展名按 `Content-Type → URL 路径 → .bin` 推断(顺序反了会踩 `/image?id=123`);
- 同一 URL 只下一次,并清理失败留下的半成品文件;
- 结果**同时**返回并追加进 `ctx.downloads`。

返回 `list[DownloadedFile]`,可以据此统计成功数:

```python
ok = sum(1 for r in results if r.ok)
failed = [r for r in results if not r.ok]
if failed and len(failed) == len(results):
    # 全部失败是很强的信号(防盗链/限速), 值得单独提示
    ctx.notify("WARNING", f"全部下载失败, 首个原因: {failed[0].error}")
```

### 7.2 从提取结果里取 URL:`urls_from_items`

```python
from smartcrawler.plugins.builtin._media import urls_from_items

pairs = urls_from_items(items, "image")                     # 值为 str 或 list 都支持
pairs = urls_from_items(items, "audio", allowed_ext=(".mp3", ".m4a"))
```

- 字段值可以是字符串、列表,**也支持用 `;` 或 `,` 分隔的多值字符串**;
- 内部已按 URL 去重;
- 返回 `[(url, index), ...]`,`index` 是它在 `items` 里的下标,便于回溯来源。

### 7.3 直接从页面取 URL:`urls_from_dom`

```python
from smartcrawler.plugins.builtin._media import urls_from_dom

urls = await urls_from_dom(ctx.page, "img", "src", limit=200)
urls = await urls_from_dom(ctx.page, ".card img", "srcset")   # 自动取第一个候选
```

`attribute="src"` 时会**自动回退**到 `data-src` / `data-original` —— 应对懒加载图站。

### 7.4 URL 补全与文件名

```python
from smartcrawler.plugins.builtin._media import absolute_url, guess_extension, safe_filename_from_url

absolute_url(ctx.url, "/a/b.jpg")        # -> https://host/a/b.jpg; data: URI 返回 ""
guess_extension("image/webp", url)       # -> ".webp"
safe_filename_from_url(url)              # 清洗 + 哈希去重, 避免同名覆盖
```

### 7.5 缩略图转原图:`_media_transform`

列表页给的往往是缩略图。用"给一对 URL 让它自己推规律"的方式,不必写死站点规则:

```python
from smartcrawler.plugins.builtin._media_transform import build_rules, to_original

rules = build_rules([
    {"name": "堆糖", "pattern": "400_0", "replacement": "1000_0"},
])
# 或给它对照行, 自动推导:
rules = build_rules([f"{thumb_url}|{full_url}"])

new_url, hit = to_original(thumb_url, rules)   # 未命中时原样返回, 不会给出坏链接
```

另有:

- `derive_rule(thumb, full)` —— 从一对 URL 推出规则;
- `resolve_limit(ctx, default)` —— 解析下载数量上限(见准则三);
- `parse_count_from_goal(text)` —— 从自然语言里解析数量(支持中文数字,"前三张")。

### 7.6 浏览器动作

```python
await ctx.crawler.browser.human_scroll(ctx.page, times=2, step=600)   # 拟人滚动
await ctx.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
await ctx.page.wait_for_timeout(800)
```

---

## 8. 登记下载产物

任何希望出现在界面「下载产物」卡片里的文件,都要追加进 `ctx.downloads`:

```python
from smartcrawler.models import DownloadedFile

target = ctx.download_path("my-plugin", f"report_{stamp}.json")   # 自动建目录
target.write_text(payload, encoding="utf-8")

ctx.downloads.append(DownloadedFile(
    url=ctx.url,
    path=str(target),
    filename=target.name,
    size=target.stat().st_size,
    mime_type="application/json",
    plugin_id=self.id,
))
```

`DownloadedFile` 的字段: `url` / `path` / `relative_path` / `filename` / `size` /
`mime_type` / `ok` / `error` / `plugin_id` / `source_item_index`。

> 用 `download_many()` 时**不需要**手动追加,它已经做了。
> 访问 `ctx.output_dir` 与 `ctx.download_path()` 的路径都会自动创建父目录。

---

## 9. 完整示例

一个"抓完后把记录另存为 CSV,并给出汇总提示"的插件,覆盖了主要接口:

```python
# plugins/save_csv.py
from __future__ import annotations

import csv
from datetime import datetime
from typing import Any

from smartcrawler.models import DownloadedFile
from smartcrawler.plugins.base import BasePlugin, PluginContext


class SaveCsvPlugin(BasePlugin):
    """把本次抓取结果另存为 CSV, 并在界面登记为可下载产物。"""

    id = "save-csv"
    name = "结果另存 CSV"
    description = "抓取结束后把全部记录导出为一份 CSV"
    version = "1.0.0"
    category = "storage"
    tags = ["导出", "CSV"]
    default_enabled = False

    config_schema: list[dict[str, Any]] = [
        {"key": "subdir", "label": "保存子目录", "type": "str", "default": "csv"},
        {
            "key": "delimiter",
            "label": "分隔符",
            "type": "enum",
            "default": ",",
            "options": [",", ";", "\t"],
        },
        {
            "key": "min_rows",
            "label": "少于此行数不导出",
            "type": "int",
            "default": 1,
            "min": 0,
            "max": 10000,
            "description": "避免抓失败时留下一堆空文件",
        },
    ]

    def on_start(self, ctx: PluginContext):
        # 跨钩子状态: 加插件名前缀, 避免和别的插件撞键
        ctx.data.setdefault("save-csv.started_at", datetime.now().isoformat(timespec="seconds"))

    def on_finish(self, ctx: PluginContext, items: list[dict[str, Any]]):
        rows = [i for i in (items or []) if isinstance(i, dict)]
        min_rows = int(ctx.config.get("min_rows") or 1)
        if len(rows) < min_rows:
            ctx.notify("INFO", f"结果另存 CSV: 只有 {len(rows)} 条, 未达阈值 {min_rows}, 跳过导出")
            return items

        columns: list[str] = []
        for row in rows:                       # 保持首次出现的列顺序
            for key in row:
                if key not in columns:
                    columns.append(key)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        subdir = str(ctx.config.get("subdir") or "csv")
        target = ctx.download_path(subdir, f"result_{stamp}.csv")

        try:
            with target.open("w", encoding="utf-8-sig", newline="") as handle:
                # utf-8-sig: Excel 打开中文 CSV 不乱码
                writer = csv.DictWriter(
                    handle, fieldnames=columns, delimiter=str(ctx.config.get("delimiter") or ","),
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(rows)
        except OSError as exc:
            # 准则一: 自己兜住 IO 失败并说明原因, 否则会被静默吞掉
            ctx.notify("WARNING", f"结果另存 CSV: 写入失败 {exc}")
            return items

        ctx.downloads.append(DownloadedFile(
            url=ctx.url,
            path=str(target),
            filename=target.name,
            size=target.stat().st_size,
            mime_type="text/csv",
            plugin_id=self.id,
        ))
        ctx.notify("SUCCESS", f"结果另存 CSV: 已导出 {len(rows)} 行 x {len(columns)} 列 -> {target.name}")
        return items
```

---

## 10. 调试与排错

| 现象 | 排查方向 |
|---|---|
| 插件卡片显示**加载失败** | 语法错误或导入异常,卡片上会给出原因;`requires` 里的包没装也会这样 |
| 插件**没被执行** | 卡片开关是否打开;本次任务是否用 `plugin_ids` 把它排除了 |
| 钩子**静默没生效** | 钩子签名写错了(尤其 `on_page(self, ctx, page_no)`)。错误在 `plugin_errors` 里 |
| 下载全部失败 | 多半是防盗链: 确认 `referer=ctx.url`;也看 `allowed_types` 是否把类型挡掉了 |
| 结果里出现**多余的字段** | 检查是否用了 `=` 而不是 `setdefault`(准则二) |
| 数量不对 | 看是否忽略了 `ctx.data` 里的任务级约束(准则三) |

**看日志**: 控制台右下角「日志」抽屉(Ctrl+L)是实时的;
`ctx.notify` 的内容会出现在那里,任务详情里还能看到 `plugin_errors`。

**看产物**: `data/plugin_output/<subdir>/`,以及任务结果里的 `downloads` 列表。

**快速验证**(不必跑真实站点):

```bash
python scripts/verify_plugins.py      # 插件系统验收: 真实下载 + 失败隔离 + 声明式插件
```

---

## 11. 声明式插件能做什么

不想写代码时,启用内置的 `declarative` 插件并在界面上配置:

| 配置项 | 作用 |
|---|---|
| `headers` | 附加请求头(每行 `Key: Value`) |
| `download_pattern` | 按正则匹配 URL 并下载 |
| `download_subdir` | 下载到 `data/plugin_output/` 下的哪个子目录 |
| `download_limit` | 单次任务最多下载几个 |
| `add_fields` | 给每条记录补充固定字段(JSON) |
| `export_format` | 额外导出一份 `csv` / `jsonl` |

它只暴露这些"已知能力",**不执行任意代码**,所以对不想碰代码的用户更安全。

---

## 12. 失败隔离与安全

### 失败隔离

- **加载失败**(语法错误 / 导入异常): 该插件被标记为「加载失败」并显示原因,
  **其他插件与框架照常启动**;
- **运行时报错**: 记入任务结果的 `plugin_errors`,界面给出黄色提示,
  **后续插件继续执行,抓取结果不受影响**。

### 安全

> ⚠️ 用户插件**等同于在本机运行的 Python 代码**,框架**不提供沙箱**。
> 只加载你自己写的、或完全信任的插件。

框架不试图沙箱化 —— 那在 Python 里无法可靠做到 —— 取而代之的是界面上的明确警示,
以及对插件文件接口的路径白名单(源码读取与删除只允许 `plugins/` 下的 `.py`,
避免借助这些接口读写任意文件)。

---

## 13. HTTP 接口与按任务选择插件

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plugins` | 列出全部插件 + 统计 + 安全提示 |
| POST | `/api/plugins/refresh` | 重新扫描插件目录 |
| POST | `/api/plugins/{id}/toggle` | 启用/停用 |
| POST | `/api/plugins/{id}/config` | 更新配置(逐字段类型校验) |
| POST | `/api/plugins/{id}/reset` | 恢复默认配置 |
| POST | `/api/plugins/create` | 由模板创建用户插件 |
| GET | `/api/plugins/template` | 获取模板源码 |
| GET | `/api/plugins/source?path=` | 查看插件源码(限 `plugins/` 目录) |
| DELETE | `/api/plugins?path=` | 删除用户插件文件(限 `plugins/` 目录) |

`POST /api/crawl` 支持 `plugin_ids`:

- **不传**: 按插件自身的启用配置执行(界面开关决定);
- **传数组**: 本次任务只运行列出的插件,如 `["image-downloader", "save-csv"]`;
- **传空数组**: 本次任务不跑任何插件。

Python API 同理:

```python
result = await crawler.crawl(
    url, rule=rule,
    plugin_ids=["image-downloader"],
    on_progress=lambda level, msg: print(f"[{level}] {msg}"),
)
print(result.downloads, result.plugins_used, result.plugin_errors)
```

---

## 14. 提交前自检清单

写完插件后逐条过一遍:

- [ ] `id` 全局唯一且是 kebab-case;`name` / `description` 填了(否则卡片上是一片空白)
- [ ] `default_enabled = False`(用户插件不该默认开启)
- [ ] 钩子签名与[第 4 节](#4-生命周期钩子)一致,尤其 `on_page(ctx)` 只有一个参数
- [ ] 改写记录用 `setdefault`,不覆盖用户规则产出的字段
- [ ] 所有可能失败的外部 IO 都有 `try/except`,并用 `ctx.notify` 说明原因
- [ ] 涉及数量限制的地方调用了 `resolve_limit(ctx, default)`
- [ ] 产生的文件都登记进了 `ctx.downloads`(或走了 `download_many`)
- [ ] 配置项都有合理的 `default`,读取时用 `or` 兜底
- [ ] 跑过 `python scripts/verify_plugins.py`,并手动抓一个真实页面看效果

---

## 15. 文件与目录约定

| 位置 | 说明 |
|---|---|
| `plugins/*.py` | 用户插件(**只加载你自己信任的代码**) |
| `data/plugins.json` | 插件的启用开关与参数(与 `.env` 分离,可自由增删字段) |
| `data/plugin_output/` | 插件下载产物(按插件子目录归类) |
| `smartcrawler/plugins/base.py` | 插件基类、`PluginContext`、钩子名定义 |
| `smartcrawler/plugins/builtin/` | 内置插件源码(可作参考实现) |
| `smartcrawler/plugins/builtin/_media.py` | 下载与取 URL 的公共辅助函数 |
| `smartcrawler/plugins/builtin/_media_transform.py` | 缩略图→原图、下载数量约束 |

插件配置**独立于 `.env`**: 插件是用户自行增删的,把每个参数都摊成 `SC_PLUGIN__*`
环境变量会让 `.env` 迅速失控,也会污染「系统配置」页那份稳定的清单。

---

## 16. 相关脚本

```bash
python scripts/verify_plugins.py          # 插件系统验收(真实下载图片 + 失败隔离 + 声明式插件)
python scripts/verify_media_limit.py      # 下载数量优先级: 任务参数 > 目标里的数量 > 插件配置
python scripts/verify_media_fields.py     # 规则含 image/audio 字段 + 下载原图
python scripts/verify_ui_alerts.py        # 下载产物卡的真实渲染验证
```

`verify_plugins.py` 会真的抓取 `books.toscrape.com` 并把商品图下载到
`data/plugin_output/images/`,然后校验文件存在、大小合理、**文件头是真实图片**
(JPEG/PNG 魔数),因此能发现"下载到了 403 错误页"这类伪装成成功的问题。
