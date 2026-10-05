# 贡献指南

> 这份文档的目标只有一个: **让你克隆下来之后能跑通、能自检、能提一个不需要来回拉的 PR。**

## 1. 环境准备

需要 Python 3.10+(推荐 3.11/3.12)。首次需要下载 Chromium 内核, 约 150 MB。

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
.venv/Scripts/python.exe -m playwright install chromium
```

依赖装不上时先看 [常见坑](#6-常见坑)。

### 1.1 ffmpeg —— **可选, 但流式地址必须有**

**不进仓库, 每个克隆的人要自己准备一次。** 它约 300 MB, 而且各平台二进制不同, 所以
`.gitignore` 把 `tools/` 排除了。

**什么时候需要它**: 下载地址是 **m3u8 / mpd 流**时。此时音频/视频不在单个文件里, 而是被切成
一堆分片并由一个几 KB 的播放列表串起来 —— 必须用 ffmpeg 拉流并**转封装**(`-c copy`, 不重编码,
所以快且无损)才能得到可播放的文件。没有 ffmpeg 时插件会**明确跳过并告警**, 不会把播放列表
当媒体存下来。

**什么时候不需要**: 直链(mp3 / m4a / mp4 / webm)由框架自己流式下载, 完全不碰 ffmpeg。
网易云音乐给的就是直链, 所以**网易云下载器不需要 ffmpeg**。

放置位置(插件按这个顺序找): 插件配置 `ffmpeg_path` → **项目内 `tools/ffmpeg/ffmpeg.exe`** → 系统 PATH。

```bash
# 推荐: 放进项目内, 不污染系统
# 1) 下载 Windows 构建(约 190 MB, 含 libx264)
#    https://github.com/BtbN/FFmpeg-Builds/releases  -> ffmpeg-master-latest-win64-gpl.zip
# 2) 解压后把 bin/ffmpeg.exe 与 bin/ffprobe.exe 放到:
#    tools/ffmpeg/ffmpeg.exe
#    tools/ffmpeg/ffprobe.exe
```

> **要 GPL 构建, 不要 LGPL 构建。** 两者都能做转封装, 但 `scripts/selfcheck.py --e2e` 与
> `scripts/verify_video_integrity.py` 需要**造**一个测试视频, 那要用 `libx264` —— 而 x264 是
> GPL 授权的, LGPL 构建里没有, 会报 `Unknown encoder 'libx264'`。

装好后确认:

```bash
.venv/Scripts/python.exe scripts/selfcheck.py --e2e    # [5] ffmpeg 与 [6] 端到端都应通过
```

### 1.2 pymusiclibrary —— 仅网易云下载器需要

```bash
.venv/Scripts/python.exe -m pip install pymusiclibrary
```

它用 QuickJS 执行 NeteaseCloudMusicApi 的 JS 逻辑, 因此**替我们完成了 weapi 加密**。
网易云不把音频地址放在页面里(歌曲页没有 `<audio>`, 脚本里也没有可用地址), 必须经
`/song/url/v1` 换取 —— 那需要加密请求体。没有这个库时"网易云音乐下载器"插件会跳过并提示安装。

## 2. 启用提交钩子(必做, 只需一次)

仓库带了 `.githooks/pre-push`: 推送前扫描即将推送的内容里有没有密钥。
**Git 出于安全考虑不会自动启用克隆下来的钩子** —— 不执行下面这条命令, 这道防线就是
关着的, 而 `.gitignore` 只能挡住"未被跟踪"的文件, 挡不住"已经 `git add` 过的" `.env`:

```bash
git config core.hooksPath .githooks
```

此后每次 `git push` 都会先跑 `scripts/check_no_secrets.py`, 命中疑似凭据即中止推送。
确认是误报时可以用 `git push --no-verify` 跳过。

## 3. 没有登录会话是正常的

`data/session.json` 与 `data/sessions/` 都在 `.gitignore` 里(会话等同于凭据, 绝不入库),
所以新克隆的仓库里**不存在**这两个文件。抓取需要登录的站点(pixiv、网易云等)之前,
先在控制台里走一次"登录一次" → "我已登录, 保存会话"。

## 4. 提交前自检

改完代码至少跑这几项, 它们都不需要外网:

| 命令 | 作用 |
|---|---|
| `python scripts/check_version.py` | 版本号唯一来源(`smartcrawler/__init__.py`)与 `pyproject.toml` 是否同步 |
| `python scripts/check_changelog.py` | CHANGELOG 里的"存在性声明"逐条对着代码核对 |
| `python scripts/check_no_secrets.py` | 密钥扫描(与 pre-push 钩子同一个脚本) |
| `python scripts/check_plugin_docs.py` | `docs/plugins.md` 点名的接口/签名/示例是否真的存在且能跑 |

改了插件系统就**必须**跑最后一项, 并且同步更新 `docs/plugins.md` ——
文档与代码不一致比没有文档更糟。

再加一个针对改动的验收脚本会更受欢迎, 见下一节。

## 5. 写一个验收脚本

`scripts/verify_*.py` 是仓库的验收惯例: 每个脚本自建靶站(本地 `http.server`)、
真实调用被测代码、逐条打印 `[OK]` / `[FAIL]`, 最后给出 `通过 (n/n)` 并以退出码表示
结果。离线可跑是硬要求, 凡是依赖真实站点的用例(如 pixiv)都可能因为网络而整批失败。

约定:

- 放在 `scripts/` 下, 命名 `verify_<主题>.py`, 用 `python scripts/verify_xxx.py` 直接运行;
- 文件头 docstring 写清**原始故障场景**与根因, 而不是复述代码在做什么;
- 靶站用 `http.server` 起在 `127.0.0.1:0`(随机端口), 不要依赖外网;
- 用 `TemporaryDirectory` 放产物, 别污染仓库;
- 关键断言配一段注释说明"不这么写会测出假绿";
- 修复类改动请**先确认脚本在修复前确实失败**, 否则它证明不了任何东西。

## 6. 常见坑

- **`git add -A` 把 `.env` 带进来**: pre-push 钩子会拦住, 但前提是你装过钩子(见第 2 节)。
- **pip 写不了 `%TEMP%`**(Windows 上表现为 `WinError 5`、无限重试并空烧 CPU):
  把临时目录指到项目内, 例如
  `$env:TEMP="$PWD/.piptmp"; $env:TMP="$PWD/.piptmp"`。
- **Playwright 官方 CDN 很慢**: 设 `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`。
- **`requirements.txt` 的中文注释在 GBK 控制台下会让 pip 崩溃**: 需要纯 ASCII 清单时用
  `python -m pip install` 逐条安装, 或先把控制台切成 UTF-8(`chcp 65001`)。
- **批处理文件必须 CRLF**: `.gitattributes` 已对 `*.bat` / `*.cmd` 强制 `eol=crlf`,
  别手动改成 LF —— `cmd.exe` 对 LF 结尾的批处理容错很差。

## 7. 提交信息

沿用现有风格: `type: 一句话描述`, 用中文写清**为什么**改(而不是复述改了什么)。

```text
fix: 下载撞大小上限时不再留下半成品
feat: 音乐下载器支持流式音频(m3u8/mpd 交给 ffmpeg 合并)
docs: README 补充启用提交钩子的步骤
```

一个 PR 尽量只做一件独立的事; 顺手修的无关问题请拆成单独提交, 便于分别 review 与回退。
