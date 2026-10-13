# 介绍动画(4 分钟)

用 Pillow 逐帧绘制、经管道交给 ffmpeg 编码, 配乐/音效用 numpy 现场合成。**不依赖剪辑软件, 也不用下载任何素材**。

## 成片结构

```
片头 114s(动画介绍, 22 个场景里的 9 个)
  ↓
演示  66s(真实控制台录屏, 加速 + 大鼠标符 + 点击/滑动音效)
  ↓
片尾  59s(边界 / 合规 / 三步上手 / 快速开始 / 收尾)
= 239s ≈ 4 分钟
```

```bash
# 1) 片头
python scripts/intro_video/make_intro_video.py .runtmp/head.mp4 \
    --scenes "title:sniff" --no-audio

# 2) 演示片段(读录屏: 先自动预处理, 再加速 + 叠加鼠标符 + 音效)
python scripts/intro_video/make_demo_frames.py

# 3) 片尾
python scripts/intro_video/make_intro_video.py .runtmp/tail.mp4 \
    --scenes "boundary:end" --no-audio

# 4) 拼接
python scripts/intro_video/assemble.py

# 渲染前自检(强烈建议先跑)
python scripts/intro_video/precheck.py

# 只调演示片段的参数(不出片, 秒出结果)
python scripts/intro_video/plan_demo.py
```

> 用 DSH 自带的 Python(含 Pillow / numpy), 项目 venv 里没有图形库:
> `C:\Users\<你>\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`

`--scenes` 支持单 key 与区间(`title:sniff`), 用逗号分隔。
所有中间产物都写在 `.runtmp/intro_video/`(已被 .gitignore 排除), 不会污染 `git status`。

## 文件

| 文件 | 作用 |
|---|---|
| `render_kit.py` | 画布/调色/缓动/绘图原语/背景纹理; 字体与豆腐块判据 |
| `scenes.py` | 13 个技术向场景 |
| `scenes_extra.py` | 5 个补充场景(iframe / 选择器 / 抓包 / 瀑布流 / 验收) |
| `scenes_easy.py` | **4 个大众向场景**(抓取是什么 / 能干什么 / 三步上手 / 边界) |
| `make_intro_video.py` | 场景编排、时间轴注入、ffmpeg 管道、配乐合成 |
| `demo_kit.py` | 演示录屏底层: 预处理、灰度采样、变化定位、音效合成、鼠标符贴图 |
| `demo_plan.py` | 演示分析层: 活动度、速度计划、操作事件(纯计算) |
| `make_demo_frames.py` | 演示片段逐帧渲染(加速 + 鼠标符 + 音效) |
| `plan_demo.py` | 只打印速度计划与事件(调参用) |
| `assemble.py` | 片头 + 演示 + 片尾 拼接 |
| `precheck.py` | 渲染前自检(6 项) |
| `check_fonts.py` | 扫源码字面量 × 字体, 抓豆腐块 |
| `test_timeline.py` | `fade` 时间轴归一化的单测 |

## 风格

对齐参考视频: 深灰细纹理底 + 白色超粗中文大字 + 黄色强调词/箭头/底部字幕;
漂浮关键词只出现在页面边缘(中央用椭圆羽化遮罩留白, 否则会压住正文)。

## 踩过的坑(重要)

这个项目里最耗时的不是写效果, 而是**排掉几个静默失败与死锁**。都记在对应文件的注释里:

| 现象 | 根因 |
|---|---|
| 中文全是豆腐块 | `latin()` / `mono()` 不含中文字形, 却被用来画含中文的副标题 |
| 整片大量空屏 | 场景补时后, 入场/关键帧/淡出仍按旧时长排, 内容早早淡完 |
| 一个场景渲染到一半崩 | 漏 import 一个缓动函数, 静态看不出, 跑 3 分钟才炸 |
| 画布改尺寸后错位 | 场景模块用 `from render_kit import W, H` 把尺寸**绑死**在导入时 |
| 演示片段**反复卡死**(卡在第 120 帧, 两边 ffmpeg CPU 都停) | **这个 ffmpeg 构建的原声 `aac` 编码器会挂死** —— 单独把 66 秒 WAV 转 aac 超过 45 秒不完成, 换 `libmp3lame` 只要 0.5 秒。编码器卡住 -> 不再读 stdin -> Python 写 stdin 阻塞 -> 看起来像"管道死锁" |
| 录屏剪不动 | 第一版把 `fps/scale/pad` 放进"每个 trim 段各做一次", 21 段各缩放一遍, ffmpeg 跑 32 分钟没出片。改成先做一次预处理转码(18 秒) |

**音频一律用 `libmp3lame`, 不要用 `aac`。**

## 视频里的数字

场景文案里有几个**硬编码的统计数字**(verify 脚本数、内置插件数、控制台页面数、配置项数)。
它们不像 README 那样改一行就行 —— 改了要整体重渲染。

所以:

- 改脚本/插件/配置之后, 顺手跑 `python scripts/check_doc_counts.py` 看 README 是否漂移;
- **要重渲染视频前**, 先把字幕里引用的数字重新实测一遍, 别直接沿用旧值。
  实测命令示例:
  ```bash
  ls scripts/verify_*.py | wc -l          # verify 脚本数
  ls smartcrawler/plugins/builtin/*.py     # 内置插件
  ```
- 这些数字曾经漂过一次: README 写「49 个脚本(43 verify + 6 check)」, 而某时刻仓库里
  实际是 43 verify + 7 check。加了 `check_doc_counts.py` 之后, 这类漂移会自动报出来。

## 为什么有自检

渲染器的失败方式**大多是静默的** —— 不报错, 只是画面难看或空掉。六道闸门:

1. **时间轴单测** —— 停留期必须铺满场景中段(场景拉伸后仍成立)
2. **画布尺寸一致性** —— 场景模块绑定的尺寸必须等于 render_kit 的
3. **字体检查(扫源码 AST)** —— 每个写死的字面量都要能被它旁边那个字体画出来
4. **场景顺序/标签/字幕完整性**
5. **冒烟** —— 把 8000 多帧全部真画一遍, 静态检查抓不到的当场抓
6. **尾帧不能是空屏**

字体检查最初用手挑样本, 一路绿灯而画面上仍是豆腐块 —— 改成扫源码里真实出现的字面量后,
一次抓出 6 处。**判据必须对着真实输入验证, 不能对着自己挑的样本。**

