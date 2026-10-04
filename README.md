# SmartCrawler —— 智能爬虫框架 + 图形化控制台

Playwright + AI 的生产级智能爬虫框架,自带**毛玻璃风格 Web 控制台**。
**仅限用于合法授权的数据采集场景。**

```
                        ┌──────────────────────────────────────────────────┐
   自然语言 goal     ──▶ │                Web 控制台 (web/)                  │
   可视化规则编辑     ──▶ │  抓取任务 · 结构分析 · 网络抓包 · 结果历史 · 配置    │
                        └───────────────────────┬──────────────────────────┘
                                                │ 任务编排 + 实时事件流
                        ┌───────────────────────▼──────────────────────────┐
   自然语言 goal     ──▶ │            SmartCrawler (crawler.py)             │
   显式 rule         ──▶ │  编排: 合规→限速→导航→监听→分析→提取→存储         │
                        └──┬──────────┬──────────┬──────────┬─────────────┘
                           ▼          ▼          ▼          ▼
                     browser.py  network.py  structure.py  ai.py
                    (Playwright) (XHR/WS捕获) (DOM分析)   (LLM规则生成)
                           │          │          │          │
                           └──────────┴────┬─────┴──────────┘
                                           ▼
                                extractor.py ──▶ storage.py
                              (提取/清洗/去重)   (json/csv/sqlite/webhook)
```

**数据流**: 打开页面 → NetworkRecorder 后台捕获 XHR/fetch/WebSocket → StructureAnalyzer
生成结构报告(候选列表/分页/元数据) → 规则来源三选一(**① 调用方传入** / **② AI 按自然语言
目标生成** / **③ 规则引擎降级**) → Extractor 在 DOM 或捕获的 JSON 上提取 → 分页跟随 →
内容哈希去重 → 增量过滤 → 存储(可选 Webhook) → `TaskResult`。

## 快速开始

> 版本与各轮更新记录见 **[CHANGELOG.md](CHANGELOG.md)**。
> 只想快速知道"改了什么、对我有什么影响", 看它开头的 **最近更新速览**(大白话版)。

**Windows 一键启动(推荐)**

双击项目根目录的 **`start.bat`** 即可。启动器会自动完成:找到 Python → 按需创建
`.venv` → 检查并补齐依赖 → 检查浏览器内核(缺失则自动下载)→ 检查端口占用 → 启动服务
并打开浏览器。全程有中文进度提示,失败时给出**可直接复制执行的修复命令**。

```bat
start.bat                   :: 双击等价于此
start.bat --port 9000       :: 换端口
start.bat --no-browser      :: 不自动打开浏览器
start.bat --check           :: 只做环境自检, 不启动
start.bat --reload          :: 代码变更自动重载(开发用)
```

> 首次运行的下载(依赖 + Chromium 内核约 200MB)支持断点续传,中断后重新运行即可继续。

**手动安装(跨平台)**

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 下载浏览器内核(带进度/断点续传,装到项目内 .browsers/)
python scripts/bootstrap_browsers.py

# 3. 配置 AI(可选,不配也能用 —— 会自动降级到规则引擎)
cp .env.example .env
python scripts/ai_configure.py --preset deepseek     # 一键预设
python scripts/ai_configure.py --test                # 实测连通性

# 4. 启动图形化控制台
python -m smartcrawler web
#    -> 浏览器打开 http://127.0.0.1:8322
```

## 图形化控制台

| 页面 | 能做什么 |
|---|---|
| **抓取任务** | 自然语言描述目标 或 手写规则;8 步进度时间线;结果表格预览 + CSV 导出;插件下载产物 |
| **结构分析** | 识别候选列表区/唯一选择器/分页器/JSON-LD 元数据;**登录墙诊断**;一键把结构回填成规则 |
| **网络抓包** | 捕获 XHR/fetch/WebSocket,自动解析 JSON;一键由接口响应生成 `mode:"json"` 规则 |
| **插件** | 启用内置能力(图片/音乐下载、反爬增强、声明式扩展),或放入自己的 Python 插件 |
| **结果与历史** | 任务回溯、产出文件预览/下载/删除、分页读取完整条目 |
| **系统配置** | **54 个配置项全部可视化编辑**(8 个分组),改动写回 `.env`;AI/代理自检 |
| **关于与帮助** | 运行环境、框架能力清单、CLI 等价用法 |

界面特性:**毛玻璃(glue/glassmorphism)** 面板、三团缓慢漂移的渐变光斑背景、悬停高光与
抬升、页面切换模糊淡入、卡片错峰入场、时间线脉冲、进度条流光、实时日志抽屉。
支持深色/浅色/高对比三套主题(`Ctrl+J` 切换)与 `prefers-reduced-motion` 降级。

> 详细的界面说明、快捷键、全部 HTTP/WebSocket 接口、前端结构见
> **[docs/web-console.md](docs/web-console.md)**。

## 访问受限诊断

抓不到数据时表面的结果都是"0 条",但原因差别极大,**处置方式完全不同** —— 混为一谈会让人
往错误方向排查很久。因此结构分析与抓取都会做一次**分类诊断**:

| 类型 | 典型表现 |
|---|---|
| `login_required` | 跳转 `/login`、有密码框 |
| **`permission_denied`** | **HTTP 401/403 但 URL 不变**,页面写"没有权限" |
| `risk_control` | 风控页、Cloudflare 挑战、"检测到异常访问" |
| `captcha` / `rate_limited` | 人机验证 / 请求过于频繁 |
| `spa_shell` / `empty_page` | 内容未渲染 / 页面正常但没有列表 |
| `server_error` / `not_found` | 5xx / 404 |

界面弹出按类型换色的警示卡,包含:URL 对比 + HTTP 状态码、逐条判定依据、**类型归因得分**、
**页面关键文本元素**(带语义标签与 CSS 选择器)、提取到的错误码/请求 ID、按类型给出的处置
建议,以及可折叠的页面完整文本与元数据。

**真实案例**:`luogu.com.cn/training/1096881#scoreboard` 无权限时返回 **HTTP 401 但 URL
不变**、由 JS 渲染出 `Error - 洛谷` / `没有权限请求此资源。`。诊断为 `permission_denied`
(100%),如实展示原文并建议"确认是否需要报名/加入"——**不会**误导用户去配置登录会话。

顺带修掉两个底层问题:① `goto()` 曾把任何 ≥400 当导航失败并丢弃页面(现在只有
5xx/429/408 才重试,4xx 保留页面交给诊断层);② SPA 文案渲染延迟导致的偶发空采集
(现在带短轮询重采)。

只做**识别与提示**,不做自动登录(自动填充凭据的风险与责任远超框架边界)。
正常列表页置信度为 0.00,阈值设在 0.5 以避免误报。

## 登录与人机验证

有些内容必须登录才能看到,而且**登录前后页面结构可能完全不同** —— pixiv 首页就是典型:
匿名访问时是注册/登录引导页, 登录后才变成作品瀑布流。结构分析与抓取都会先做一次
**登录状态判定**, 界面上以提示条呈现并询问是否登录。

| 方向 | 信号(节选) |
|---|---|
| 未登录 | "用 xx 账号登录 / 注册账号 / Sign in"(中英文)、密码框、登录入口链接、鉴权表单 |
| 已登录 | "退出登录"、**页头**头像/用户菜单、页头"我的收藏/关注/设置"入口 |

两类都无明显信号时判为 `unknown` —— **宁可不说, 也不要误导**用户去登录一个根本不需要
登录的站点。

**手动登录**: 点"登录一次"会弹出一个**可见**的浏览器窗口, 你在里面正常登录
(验证码/扫码都可以), 然后点「我已登录, 保存会话」—— 会话写入 `data/session.json`,
之后抓取自动复用, **无需再改任何配置**。保存成功后弹层会展开「第 2 步」, 可直接用登录
后的身份重新分析 / 抓取 / 核实会话是否生效。

**手动过人机验证**: 站点拉起 reCAPTCHA / Turnstile / 滑块时(例如 pixiv 每次登录都会),
页面顶部会出现「**手动过验证**」按钮 —— 打开可见窗口过完验证, 通行凭据
(`cf_clearance` 等)自动存进会话, 之后的抓取不再遇到同一个挑战。

识别上重点做了**防误报**: 很多站点每次访问都会挂上 reCAPTCHA 的 anchor iframe, 但只在
部分情况才真的弹挑战(实测未挑战时该 iframe 是 `visibility: hidden`)。因此判据是
"**可见的**挑战元素"而非"页面提到了 captcha", 否则会变成每次都提示、反而更烦。

> **框架自始至终不接触你的密码**。自动填账号密码涉及凭据的存储/加密/泄露面, 风险远超
> 框架边界; 人机验证更是设计来拦自动化的, 绕过它既不可靠也不合规。这里只保存服务端签发
> 的会话凭据。接口只返回掩码摘要(Cookie 数量、域名、名字), 绝不回传值; 会话文件已在
> `.gitignore`。

## 插件系统

两条并行扩展路径:

| 路径 | 能做什么 | 安全性 |
|---|---|---|
| **声明式配置** | 附加请求头、正则批量下载、补充固定字段、额外导出 | 只暴露已知能力,无代码执行 |
| **Python 单文件插件** | 任意: 图片/音乐下载、新反爬策略、自定义清洗与存储 | ⚠️ 等同本机代码执行,框架不提供沙箱 |

往项目根 `plugins/` 丢一个 `.py` 即被自动发现。生命周期钩子:
`on_start` / `before_navigate` / `after_navigate` / `before_extract` / `after_extract` /
`on_page` / `on_finish`(同步与异步写法都支持)。

内置插件:

| 插件 | 默认 | 说明 |
|---|---|---|
| **图片下载器** | ✅ 启用 | 从记录字段或页面 `img` 下载图片;并发限流、大小上限、最小宽度过滤 |
| 音乐下载器 | 关闭 | 下载 `audio`/`source` 或音频字段;可按同名只留最大文件 |
| 反爬增强 | 关闭 | 注入请求头、模拟人类滚动节奏、补 webdriver 伪装、**识别拦截页** |
| 声明式扩展 | 关闭 | 免代码配置下载/补字段/额外导出 |
| 示例: 补充字段 | 关闭 | 用户插件编写模板 |

插件配置独立存放于 `data/plugins.json`(不污染 `.env`),产物落在
`data/plugin_output/`。**故障隔离**:单个插件加载失败或运行崩溃都只记录错误,
不影响其他插件与抓取结果。

> **写插件请看 [docs/plugins.md](docs/plugins.md)** —— 那是插件开发的准则:
> 5 分钟上手、三条硬性准则、钩子签名表、`ctx` 全字段、配置项类型表、
> 现成的辅助函数(并发下载 / 取 URL / 缩略图转原图 / 数量约束)、完整示例、
> 排错对照表与提交前自检清单。
>
> 该文档由 `python scripts/check_plugin_docs.py` **逐项核对代码**(接口是否存在、
> 签名是否一致、示例能否真的跑通),所以不会和实现脱节。

## 核心能力

| 模块 | 文件 | 说明 |
|---|---|---|
| 浏览器自动化 | `browser.py` | Chromium/Firefox/WebKit、无头可配、模拟交互、多标签页、iframe、信号量并发、会话持久化 |
| 网络监听 | `network.py` | 只捕获 xhr/fetch/websocket、JSON 自动解析、耗时统计、JSONL 落盘、正则/MIME/状态码查询 |
| 结构分析 | `structure.py` | 重复结构识别、唯一选择器生成(id>data-*>稳定class)、分页识别、JSON-LD/OG/Microdata |
| AI 辅助 | `ai.py` | 自然语言→规则、API JSON 字段映射、选择器自愈、数据清洗;OpenAI 兼容(DeepSeek/通义/智谱/Moonshot…)+ 本地 Ollama;响应缓存;离线降级 |
| 数据提取 | `extractor.py` | CSS/XPath/JSONPath/正则、清洗管线(strip/price/date/url…)、Pydantic 校验、去重与增量 |
| 反爬稳定性 | `anti_spider.py` | 随机 UA/请求头、代理池(失败剔除+冷却)、随机限速(默认 1~3s)、指数退避重试、指纹伪装注入、**robots.txt 合规(默认开启)** |
| 存储 | `storage.py` | `save(data, format, path)` 统一接口:json/jsonl/csv/sqlite + Webhook |
| 访问诊断 | `access_control.py` + `page_diagnostics.py` | 登录/权限/风控/验证码/空壳等分类诊断;提取页面文本元素、错误码与元数据 |
| 懒加载 | `lazy_load.py` | 模拟滚动触发懒加载;区分"滚到底"与"无限流", 后者交由用户决定是否继续 |
| 人机验证 | `challenge.py` | 识别 reCAPTCHA/Turnstile/极验等**可见**挑战;提供"手动过一次"入口(防误报) |
| 登录会话 | `session.py` + `login_state.py` | 登录态识别;手动登录一次后保存/复用 Cookie 与 localStorage(不接触密码) |
| 插件系统 | `plugins/` | 生命周期钩子 + 两条扩展路径;内置图片/音乐下载、反爬增强、声明式扩展;故障隔离 |
| Web 控制台 | `web/` | FastAPI + WebSocket + 零构建前端;任务管理、配置内省、实时日志总线 |

## 命令行用法

控制台之外,核心能力都有等价的 CLI:

```bash
# 自然语言抓取: "抓取所有商品名称和价格"
python -m smartcrawler crawl https://books.toscrape.com \
    --goal "抓取所有书籍的名称、价格和链接" --format csv --output books.csv --show

# 页面结构分析(看看识别出了哪些候选列表)
python -m smartcrawler analyze https://books.toscrape.com

# 查看页面发出的 XHR 请求(找隐藏 API)
python -m smartcrawler requests https://example.com --pattern "api" --dump data/net.jsonl

# 启动控制台 / 纯 API 调试服务
python -m smartcrawler web --port 8322
python -m smartcrawler serve --port 8322        # 仅 API,文档在 /api/docs
```

### Python 代码

```python
import asyncio
from smartcrawler import SmartCrawler

async def main():
    async with SmartCrawler() as crawler:
        # AI 模式: 自然语言 -> 规则 -> 数据
        result = await crawler.crawl(
            "https://books.toscrape.com",
            goal="抓取所有书籍的名称、价格和链接",
            format="csv", output="books.csv",
            max_pages=3,
        )
        print(result.item_count, result.saved_to)

asyncio.run(main())
```

### 自然语言指令的工作方式

`crawl(url, goal="抓取所有商品名称和价格")` 内部发生:

1. 页面加载后进行结构分析, 得到候选列表区(如 `section > div ol > li.col-xs-6`, 30 项)
   及其样本字段(标题/价格/链接);
2. 将**结构报告 + 目标语句**发给大模型(带缓存), 要求输出严格 JSON 规则:
   ```json
   {
     "mode": "dom",
     "list_rule": {
       "item_selector": "article.product_pod",
       "fields": [
         {"name": "title", "selector": "h3 a", "transform": ["strip"]},
         {"name": "price", "selector": ".price_color", "transform": ["price"]}
       ]
     },
     "pagination": {"next_selector": "li.next a", "max_pages": 3}
   }
   ```
3. 规则经 Pydantic 校验后在页面上批量提取; 若目标更适合走接口数据, AI 会输出
   `mode=json` 的 JSONPath 规则, 框架改从捕获的 XHR 响应中提取。

## 项目结构

```
SmartCrawler/
├── smartcrawler/                核心包
│   ├── crawler.py               主编排: 合规→限速→导航→监听→分析→提取→存储
│   ├── browser.py               浏览器生命周期与页面管理
│   ├── network.py               XHR/fetch/WebSocket 捕获
│   ├── structure.py             DOM 结构分析与唯一选择器生成
│   ├── extractor.py             提取 / 清洗管线 / 去重
│   ├── anti_spider.py           UA 轮换、代理池、限速、重试、robots.txt
│   ├── access_control.py        访问受限分类诊断(登录/权限/风控/验证码/空壳)
│   ├── page_diagnostics.py      页面文本元素/错误码/元数据采集
│   ├── storage.py               json/jsonl/csv/sqlite + Webhook
│   ├── ai.py                    LLM 客户端与四大能力(规则生成/字段映射/自愈/清洗)
│   ├── models.py                Pydantic 数据模型
│   ├── config.py                分层配置(环境变量 > .env > YAML > 默认值)
│   ├── utils.py                 日志 / 迷你 JSONPath / 哈希 / 选择器工具
│   ├── cli.py                   命令行入口
│   ├── api.py                   兼容层(转发到 web/)
│   ├── plugins/                 ← 插件系统
│   │   ├── base.py              抽象、上下文、钩子协议
│   │   ├── manager.py           发现 / 配置持久化 / 失败隔离执行
│   │   └── builtin/             内置插件(图片、音乐、反爬、声明式)
│   └── web/                     ← Web 控制台后端
│       ├── api.py               应用工厂(lifespan / 路由装配 / 静态挂载)
│       ├── service.py           服务层: 任务编排、配置热更新、自检
│       ├── state.py             任务状态模型 + 事件总线
│       ├── config_store.py      Schema 内省 + .env 读写 + 类型归一化
│       ├── logs.py              loguru → WebSocket 的实时日志总线
│       ├── schemas.py           请求/响应模型
│       ├── ws.py                三个 WebSocket 通道
│       ├── routes/              HTTP 路由(system / config / tasks / plugins)
│       └── frontend/            ← 零构建前端(原生 ESM + CSS)
│           ├── index.html
│           ├── styles/          tokens/base/components/layout/pages/animations
│           └── js/              main/router/store/api/ws/utils + ui/ + pages/
├── plugins/
│   └── example_enrich.py        ← 用户插件目录(示例/模板)
├── scripts/
│   ├── launcher.py              启动器主逻辑(环境自检 + 启动, 中文输出)
│   ├── bootstrap_browsers.py    浏览器内核下载(进度/续传/重试)
│   ├── ai_configure.py          AI 配置与连通性测试
│   ├── e2e_acceptance.py        端到端验收(真实抓取 + WebSocket 事件序列断言)
│   ├── verify_plugins.py        插件验收(真实下载图片 + 失败隔离 + 声明式插件)
│   ├── verify_access_control.py 访问受限诊断验收(46 项 · 合成样本)
│   ├── verify_luogu_case.py     洛谷权限页真实回归(16 项 · 真实站点)
│   ├── verify_ui_layout.py      UI 回归(容器重叠 / 终态状态断言)
│   ├── verify_ui_alerts.py      UI 验收(登录墙提醒卡 + 下载产物卡渲染)
│   ├── check_frontend_imports.py 前端"漏导入"静态检查
│   └── screenshot_ui.py         UI 截图与前端错误采集
├── tests/test_config_store.py   配置层单元测试(14 项)
├── examples/                    独立示例脚本
├── docs/
│   ├── web-console.md           控制台完整文档
│   └── plugins.md               插件扩展指南
├── config.yaml.example          YAML 配置模板(含逐项注释)
├── start.bat                    ← Windows 一键启动器(双击即可)
├── aiAPI.py                     兼容入口 → scripts/ai_configure.py
├── pyproject.toml               打包 / ruff / pytest 配置
└── requirements.txt
```

## 配置

优先级: **环境变量 > .env > YAML(`SC_CONFIG_FILE=cfg.yaml`) > 默认值**。
嵌套键用双下划线, 如 `SC_BROWSER__HEADLESS=false`。

- **图形化**: 控制台「系统配置」页,54 个字段全部可视化编辑并写回 `.env`(保留注释);
- **文件**: 完整清单见 [.env.example](.env.example) 与 [config.yaml.example](config.yaml.example)。

常用项:

| 变量 | 默认 | 说明 |
|---|---|---|
| `SC_BROWSER__HEADLESS` | `true` | 无头模式 |
| `SC_BROWSER__MAX_PAGES` | `4` | 并发页面上限(信号量) |
| `SC_AI__PROVIDER` | `openai` | `openai`(兼容接口)/ `ollama` |
| `SC_AI__OFFLINE` | `false` | 强制离线(纯规则引擎) |
| `SC_ANTI_SPIDER__RANDOM_DELAY_RANGE` | `[1.0,3.0]` | 随机限速区间(秒) |
| `SC_ANTI_SPIDER__RESPECT_ROBOTS` | `true` | robots.txt 合规开关 |
| `PROXY_POOL` | 空 | 逗号分隔代理列表 |
| `SC_CRAWLER__INCREMENTAL` | `false` | 增量抓取(内容哈希) |
| `SC_STORAGE__WEBHOOK_URL` | 空 | 结果 Webhook 推送 |

## 测试与验收

```bash
python tests/test_config_store.py          # 配置层单测(无需 pytest 也能跑)
python scripts/check_version.py            # 版本号一致性(包/打包/接口)
python scripts/check_changelog.py          # 核对 CHANGELOG 的声明与代码是否一致
python scripts/check_frontend_imports.py   # 前端"漏导入"静态检查
python scripts/e2e_acceptance.py           # 端到端: 真实抓取 + WebSocket 事件观测
python scripts/verify_plugins.py           # 插件: 真实下载图片 + 失败隔离 + 声明式插件
python scripts/verify_access_control.py    # 访问受限诊断(46 项 · 合成样本, 可复现)
python scripts/verify_luogu_case.py        # 洛谷权限页真实回归(16 项 · 真实站点)
python scripts/verify_session.py           # 登录会话: 掩码安全 + 会话恢复端到端(42 项)
python scripts/verify_login_flow.py        # 手动登录全链路: 可见窗口→探测→保存→生效(22 项)
python scripts/verify_login_commit.py      # 保存会话必须真正落盘 + 立刻生效(17 项)
python scripts/verify_challenge.py         # 人机验证识别: 真挑战命中 + 真实站点不误报(22 项)
python scripts/verify_challenge_flow.py    # 手动过验证全链路: 过完→存凭据→不再被拦(14 项)
python scripts/verify_lazy_load.py         # 懒加载: 滚到底 vs 无限流 + deep_scroll 生效(11 项)
python scripts/verify_scroll_interactive.py # 续滚: 反复询问直到用户选择停止 + 轮数可配(8 项)
python scripts/verify_overlay_dismiss.py   # 登录浮层: 有内容则关掉遮罩继续抓 + 真没内容仍跳过(8 项)
python scripts/verify_masonry_merge.py     # 瀑布流多列合并成一个列表, 不丢列(6 项)
python scripts/verify_media_fields.py      # 规则含 image/audio 字段 + 懒加载图取 data-src + 下载原图(10 项)
python scripts/verify_media_limit.py       # 下载数量优先级: 任务参数 > 目标里的数量 > 插件配置(4 项)
python scripts/verify_url_groups.py        # 结果按 URL 归类 + 删除时真正清理磁盘产出(21 项)
python scripts/verify_ui_sandbox.py       # UI 沙箱: 静态内容 + 缓存头 + 对比度 + 不拉靶站(33 项)
python scripts/verify_shutdown.py         # 关闭服务生效 + 子进程清理 + 不误杀
python scripts/verify_multi_instance.py   # 跨实例互不干扰(关掉一个, 另一个仍能干活)
python scripts/check_plugin_docs.py       # 插件文档事实核查: 接口/签名/示例逐项核对(111 项)
python scripts/verify_plugin_docs_check.py # 上者的注入式自检(证明它真的会报错)
python scripts/verify_ui_layout.py         # UI 回归: 断言容器不重叠 + 终态 UI 正确
python scripts/verify_ui_alerts.py         # UI: 访问诊断卡 + 下载产物卡真实渲染
python scripts/check_frontend_imports.py   # 前端"漏导入"静态检查
python scripts/screenshot_ui.py            # UI 截图 + 前端控制台错误采集
```

几个关键的验收思路:

- `e2e_acceptance.py` 真实发起抓取, 并断言事件序列为「先 result 后终态 status」——
  这是「任务成功后仍显示进行中」那个 bug 的固化判据;
- `verify_plugins.py` 会真的把图片下载下来, 并**校验文件头是真实图片**(JPEG/PNG 魔数),
  因此能发现"下载到了 403 错误页"这类伪装成成功的问题;
- `verify_access_control.py` 用**本地合成页面**而非真实站点, 因此可复现、离线可跑,
  还能精确构造"应该判成哪一类"与"不应误判"两类样本(正常列表页置信度 0.00);
- `verify_luogu_case.py` 拿**真实站点**做回归: 断言 401 页面不被丢弃、归类为
  `permission_denied`、页面原文被提取出来。跑通它等于确认用户报的那个场景真的解决了;
- `verify_ui_layout.py` / `verify_ui_alerts.py` 用 Playwright **真实渲染**, 并用几何矩形
  相交断言布局、用选择器等断言诊断卡真的出现 —— 诊断信息如果没渲染出来, 对用户等于不存在;
- `check_frontend_imports.py` 补上 `node --check` 查不到的盲区: 语法合法但用到了未导入的
  符号(这类错误只在执行到那一行才抛 `ReferenceError`)。

## 合规与免责

- 默认**遵守 robots.txt**(`SC_ANTI_SPIDER__RESPECT_ROBOTS=false` 可关闭, 但请三思);
- 默认随机限速 1~3 秒/请求, 避免对目标站点造成压力;
- 本框架仅供学习与合法授权的数据采集(自有系统监控、公开数据研究、获书面授权的采集等);
  使用者需自行遵守目标网站服务条款及所在地区法律法规, 由此产生的责任由使用者承担。
