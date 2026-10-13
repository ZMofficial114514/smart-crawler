# 介绍动画(4 分钟)

用 Pillow 逐帧绘制、经管道交给 ffmpeg 编码, 配乐用 numpy 现场合成。**不依赖剪辑软件, 也不用下载任何素材**。

```bash
# 渲染(约 3.5 分钟, 产出 dist/SmartCrawler-介绍.mp4)
python scripts/intro_video/make_intro_video.py

# 渲染前自检(强列建议先跑, 见下)
python scripts/intro_video/precheck.py

# 只导出几帧看画面
python scripts/intro_video/make_intro_video.py --frames-only 0 60
```

> 用 DSH 自带的 Python(含 Pillow / numpy), 项目 venv 里没有图形库:
> `C:\Users\<你>\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`

## 文件

| 文件 | 作用 |
|---|---|
| `render_kit.py` | 画布/调色/缓动/绘图原语/背景纹理; 字体与豆腐块判据 |
| `scenes.py` | 13 个主场景 |
| `scenes_extra.py` | 5 个补充场景(iframe / 选择器 / 抓包 / 瀑布流 / 验收) |
| `make_intro_video.py` | 场景编排、时间轴注入、ffmpeg 管道、配乐合成 |
| `precheck.py` | 渲染前自检(6 项) |
| `check_fonts.py` | 扫源码字面量 × 字体, 抓豆腐块 |
| `test_timeline.py` | `fade` 时间轴归一化的单测 |

## 风格

对齐参考视频: 深灰细纹理底 + 白色超粗中文大字 + 斜体英文副题 + 黄色强调词/箭头/底部字幕;
漂浮关键词只出现在页面边缘(中央用椭圆羽化遮罩留白, 否则会压住正文)。

## 为什么有自检

这个渲染器的失败方式**大多是静默的** —— 不报错, 只是画面难看或空掉。实际踩到过的:

| 现象 | 根因 |
|---|---|
| 中文全是豆腐块 | `latin()` / `mono()` 不含中文字形, 却被用来画含中文的副标题 |
| 整片大量空屏 | 场景补时后, 入场/关键帧/淡出仍按旧时长排, 内容早早淡完 |
| 一个场景渲染到一半崩 | 漏 import 一个缓动函数, 静态看不出, 跑 3 分钟才炸 |
| 画布改尺寸后错位 | 场景模块用 `from render_kit import W, H` 把尺寸**绑死**在导入时 |

对应的六道闸门:

1. **时间轴单测** —— 停留期必须铺满场景中段(CJK 场景拉伸后仍成立)
2. **画布尺寸一致性** —— 场景模块绑定的尺寸必须等于 render_kit 的
3. **字体检查(扫源码)** —— 每个写死的字面量都要能被它旁边那个字体画出来
4. **场景顺序/标签/字幕完整性**
5. **冒烟** —— 把 7000 多帧全部真画一遍, 静态检查抓不到的当场抓
6. **尾帧不能是空屏**

另外 `make_intro_video.py` 支持 `--fps` / `--size` / `--no-audio`。
