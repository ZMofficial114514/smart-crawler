"""
SmartCrawler 页面结构分析模块。

StructureAnalyzer 在页面内执行一段分析 JS(单次 evaluate, 性能友好), 完成:
1. 简化 DOM 树生成(剔除 script/style/注释, 带节点预算);
2. 重复结构识别: 按"父容器 + 子元素签名(tag+稳定class)"分组, 找出疑似列表区,
   并为每个候选列表项推断字段(标题/链接/图片/价格/其他叶子文本)及其相对选择器;
3. 唯一 CSS 选择器生成: 优先 id > data-* > 稳定 class > nth-of-type 路径,
   自动过滤框架生成的随机 class(css-xxx / 长十六进制哈希等);
4. 分页器识别: "下一页/next/›" 等文本特征 + rel=next 兜底;
5. 结构化元数据提取: JSON-LD / Open Graph / Microdata。

产出 PageStructureReport(JSON), 既可直接落盘调试, 也是 AI 生成提取规则的核心输入。
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger
from playwright.async_api import Page

from .config import Settings
from .models import ExtractionRule, FieldSpec, ListCandidate, ListRule, PageStructureReport, PaginationInfo, PaginationRule
from .utils import truncate

# ---------------------------------------------------------------------------
# 页面内执行的分析脚本(纯 JS, 无外部依赖)
# 说明: 故意不使用 f-string, 避免花括号转义问题
# ---------------------------------------------------------------------------
_ANALYZE_JS = r"""
() => {
    // 不参与结构树的标签。
    //
    // `TEXTAREA` / `INPUT` / `SELECT` / `OPTION` 的加入来自实测: 网易云音乐把
    // ArtTemplate 的页面模板整段放在 `<textarea>` 里(如 `textarea#m-widget-comment3`),
    // 而 `<textarea>` 的内容是 **raw text**, `innerText`/`textContent` 会原样返回那几百行
    // JS(形如 `{if x.userType==4}${before}<sup class=...`)。结果简化树被这些模板代码
    // 塞满, 既看不出真实结构, 又挤占了节点预算, 让真正的数据区进不来。
    const SKIP = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEMPLATE:1, SVG:1, PATH:1, LINK:1,
                  META:1, IFRAME:1, CANVAS:1, TEXTAREA:1, INPUT:1, SELECT:1, OPTION:1};
    const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/([^\w-])/g, '\\$1');
    // 稳定 class: 过滤 CSS Modules / styled-components / 构建工具生成的随机类名
    const stableClasses = (el) => Array.from(el.classList).filter(
        c => c && !/^(css-|sc-|chakra-|jsx-|svelte-)/.test(c) && !/[a-f0-9]{10,}/i.test(c) && c.length <= 40
    );

    //: 选择器里每个元素**最多用几个 class**。
    //:
    //: 原子化 CSS(Tailwind 这类)会让一个元素挂十几二十个 class, 实测 pixiv 上生成了
    //: 这种选择器::
    //:   div.grid.w-full.gap-4 div img.absolute.inset-0.size-full.bg-surface1.object-cover...
    //: 又长又脆 —— 站点换个样式类就全废, 人也没法读、没法维护。
    //: 只取前 3 个(通常是语义性的那位, 原子类一般排在后面), 既保留区分度又保持可读。
    const MAX_CLASSES_PER_SELECTOR = 3;
    const selectorClasses = (el) => stableClasses(el).slice(0, MAX_CLASSES_PER_SELECTOR);

    // 长 URL 的省略方式: **在路径分隔符处截断, 并保留末尾的文件名**。
    //
    // 为什么不能直接 slice(0, N): 图片 URL 的关键信息在**文件名那一段**
    // (作品 ID 与 _p0/_master1200 这类标记), 而冗余的是中间的日期目录。
    // 早先树里给 URL 留 84 字符, pixiv 的 pximg 地址正常就超过这个长度, 于是显示成
    //   https://i.pximg.net/c/250x250_80_a2/custom-thumb/img/2026/09/23/05/28/34/149997519_p
    // 后半段被硬切成半个文件名, 既看不出是缩略图还是原图, 也没法直接对照。
    // 现在: 头 96 + "…" + 尾 46, 且只在 '/' 处下刀, 保证不切碎路径片段。
    const ellipsizeUrl = (u, max) => {
        if (!u || u.length <= max) return u;
        const headLen = Math.max(40, max - 52);
        const tailLen = 46;
        let head = u.slice(0, headLen);
        const cut = head.lastIndexOf('/');
        if (cut > 20) head = head.slice(0, cut);   // 回退到最近的 '/' 之后切断
        let tail = u.slice(-tailLen);
        const slash = tail.indexOf('/');
        if (slash >= 0 && slash < tail.length - 1) tail = tail.slice(slash + 1);
        return head + '/…/' + tail;
    };

    // ---- 唯一选择器生成: id > data-* > 稳定class > nth-of-type 路径 ----
    const uniqSelector = (el) => {
        if (!el || el === document.body) return 'body';
        if (el.id) {
            const sel = '#' + cssEscape(el.id);
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        const tag = el.tagName.toLowerCase();
        for (const attr of Array.from(el.attributes)) {
            if (attr.name.startsWith('data-') && attr.value && attr.name !== 'data-v') {
                const sel = tag + '[' + attr.name + '="' + attr.value.replace(/"/g, '\\"') + '"]';
                if (document.querySelectorAll(sel).length === 1) return sel;
            }
        }
        const cls = selectorClasses(el);
        if (cls.length) {
            const sel = tag + '.' + cls.map(cssEscape).join('.');
            if (document.querySelectorAll(sel).length === 1) return sel;
        }
        // 兜底: 从目标往上拼一条 `A > B > C` 的路径。
        //
        // **遇到带 id 的祖先时, 必须把 id 当成"路径的一段", 不能就地收尾。**
        // 这里出过一个隐蔽的严重缺陷(实测证据):
        //   目标的真实结构是  body > div#__next > div > div > … > nav > a
        //   走到 #__next 时若写成 `#__next > a`, 就等于断言 a 是 #__next 的**直接子级**,
        //   而它其实在两层 div 之下 —— 生成的选择器**匹配 0 个元素**。
        //   后果不只是"选择器不好用": 分页的 next_selector 就是这么生成的,
        //   于是翻页点击永远超时, 界面显示"共 1 页", 而用户明明看到有翻页按钮。
        //   正确做法: 把 '#' + id 塞进 parts, 让它成为链上的一环, 再 break。
        const parts = [];
        let cur = el, depth = 0;
        while (cur && cur.tagName && cur !== document.body && depth < 30) {
            if (cur.id) {
                parts.unshift('#' + cssEscape(cur.id));
                // id 理论上唯一, 但**必须实测确认**: 站点重复 id 或页面里有
                // 动态插入的同 id 节点时, 只有 id 的选择器也会匹配多个。
                // 与上面几层一样, 验证通过才敢用。
                const withId = parts.join(' > ');
                try {
                    if (document.querySelectorAll(withId).length === 1) return withId;
                } catch (e) { /* 忽略 */ }
                // 不唯一: 继续往上补层级, 让它更具体
                cur = cur.parentElement;
                depth++;
                continue;
            }
            let s = cur.tagName.toLowerCase();
            const parent = cur.parentElement;
            if (parent) {
                const sameTag = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
                if (sameTag.length > 1) s += ':nth-of-type(' + (sameTag.indexOf(cur) + 1) + ')';
            }
            parts.unshift(s);
            // 拼出来的路径**必须实测唯一**才返回。
            // 早先这里直接 return, 于是返回了一个匹配 0 个元素的"坏选择器" ——
            // 调用方(翻页点击、字段提取)只会看到"点不动/提不到", 完全不知道是选择器的问题。
            const built = parts.join(' > ');
            try {
                if (document.querySelectorAll(built).length === 1) return built;
            } catch (e) { /* 忽略非法选择器, 继续往上 */ }
            cur = parent; depth++;
        }
        // 一路走到 body 都不唯一: 返回最长的那条路径(至少是"能匹配到目标"的最具体描述),
        // 而不是一个可能匹配 0 个的残缺路径。
        return parts.length ? parts.join(' > ') : tag;
    };

    // ---- 相对 scope 的子选择器(用于列表项字段推断) ----
    // 返回形如 "顶层tag.class 内层tag.class 最深层tag.class" 的后代选择器,
    // 比"仅最近祖先层"更精准(如 article.product_pod p.price_color)
    //
    // **不能因为"太深"就返回 null。** 这是一条踩过的坑: 早先这里最多向上走 5 层,
    // 超过就放弃; 而 pixiv 作品卡片的封面图在第 7 层, 于是图片字段被静默丢弃 ——
    // 用户看到的现象就是"样本 HTML 里明明有 <img>, 生成的规则却没有 image 键",
    // 结果图片下载插件下不到任何图。深度不该是放弃的理由:
    //   - 5 层内: 用完整路径(最准);
    //   - 更深: 退化为"最近祖先 + 目标元素"两段式(如 "div.grid img"), 仍然可用且够稳;
    //   - 只有连祖先都找不到时才返回 null。
    const tagClass = (el) => {
        const cls = selectorClasses(el);
        return el.tagName.toLowerCase() + (cls.length ? '.' + cls.map(cssEscape).join('.') : '');
    };
    const relSelector = (scope, el) => {
        if (!scope || !el || el === scope) return null;
        const path = [];  // 从 el 向上到 scope 直接子节点之间的路径(不含顶层)
        let cur = el;
        for (let i = 0; i < 12 && cur && cur !== scope; i++) {
            if (cur.parentElement === scope) {
                const parts = [tagClass(cur)];
                for (let j = Math.min(path.length, 2) - 1; j >= 0; j--) parts.push(tagClass(path[j]));
                return parts.join(' ');
            }
            path.push(cur);
            cur = cur.parentElement;
        }
        // 超出层级预算: 退化为"最近祖先 + 目标" —— 保证字段不丢。
        // 注意要保持后代关系(空格), 不能用 ' > ', 否则会要求直接的父子关系而匹配不到。
        if (path.length >= 2) {
            return tagClass(path[path.length - 1]) + ' ' + tagClass(el);
        }
        if (path.length === 1) {
            return tagClass(el);
        }
        return tagClass(el);
    };

    // ---- 给字段取一个"能看懂"的名字 ----
    //
    // 默认命名用的是元素的稳定 class(第一个), 但很多站点的 class 是构建工具生成的短哈希,
    // 对用户毫无意义。实测网易云音乐: 歌手名那一列的 class 是 `s-fc7`, 于是规则里出现
    // `s-fc7` 这种键, 用户根本分不清哪个是歌手、哪个是专辑、哪个是时长。
    //
    // 这里用**语义信号**取代它: URL 路径与文案里往往已经写明了这一列是什么
    // (/artist?id= -> 歌手, /album?id= -> 专辑, 03:45 -> 时长)。
    // 只在能明确判断时才改名, 判断不出就保留原 class, 避免瞎猜。
    const semanticName = (el, text, fallback) => {
        const a = el.closest ? el.closest('a[href]') : null;
        const href = a ? (a.getAttribute('href') || '') : '';
        const t = (text || '').trim();
        // 组合信号: URL 路径 + 文案关键词(中英日)
        const probe = (href + ' ' + t).toLowerCase();
        const rules = [
            // 作者/用户类
            [/\/artist|\/musician|\/singer|歌手|艺术家|艺人|演唱|artist/i, 'artist'],
            [/\/album|\/disc|专辑|唱片|album/i, 'album'],
            [/\/user|\/uid|\/member|\/profile|\/author|用户|作者|博主|up主|uploader|\buser\b|\bauthor\b/i, 'user'],
            // 时长/日期/计数类
            [/^\d{1,2}:\d{2}(:\d{2})?$|时长|duration|length/i, 'duration'],
            [/\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\/\d{1,2}\/\d{4}|日期|发布时间|发布于|date|time/i, 'date'],
            [/播放|收听|播放量|play\s*count|views?|播放次数/i, 'play_count'],
            [/评论|回复|comment|repl/i, 'comment_count'],
            [/专辑|所属专辑/i, 'album'],
            [/类型|分类|标签|category|genre|tag/i, 'category'],
        ];
        for (const [re, name] of rules) {
            if (re.test(probe)) return name;
        }
        return fallback;
    };

    // ---- 丢弃"指向同一元素、名字却是构建产物"的冗余字段 ----
    //
    // 实测网易云音乐: 专辑那一格同时产出两个字段 ——
    //     album     选择器 div.td.w2 a.s-fc3 span.s-fc7   (认出 /album 路径, 名字可读)
    //     s-fc7     选择器 div.td.w2 a.s-fc3 span.s-fc7   (同名元素, 名字不可读)
    // 二者选择器完全相同, 结果表格里就会出现两列一模一样的数据, 用户还得猜哪列是什么。
    //
    // 判据: 选择器(含属性)完全相同 + 该名字是构建产物类名 -> 丢掉。只在**有另一个**
    // 更可读的字段指着同一元素时才丢, 避免把唯一的字段也删掉。
    const SELF_NAMES = new Set(['title', 'link', 'image', 'audio', 'video', 'price',
                                'thumb', 'image_srcset', 'text']);
    const looksGenerated = (name) => {
        if (!name || SELF_NAMES.has(name)) return false;
        // s-fc7 / u-icn2 / f-fs1 这类"字母+数字"的样式钩子
        if (/^[a-z]{1,3}-?[a-z]{0,4}\d{1,3}$/i.test(name)) return true;
        // css-1x2y3z / sc-bdVaJa 这类 CSS-in-JS 生成名
        if (/^(css|sc|jsx|emotion|styled)-/i.test(name)) return true;
        // 纯哈希(6 位以上大小写混合)
        if (name.length >= 6 && /^[a-z]+[A-Z]/.test(name)) return true;
        return false;
    };
    const dropRedundantFields = (sample, fields) => {
        if (fields.length < 2) return fields;
        const keep = [];
        for (let i = 0; i < fields.length; i++) {
            const f = fields[i];
            if (!looksGenerated(f.name)) { keep.push(f); continue; }
            // 有别的字段用同一个选择器 + 同一个属性, 且那个名字可读 -> 本字段冗余
            const dup = fields.some((g, j) =>
                j !== i && g.selector === f.selector &&
                (g.attribute || '') === (f.attribute || '') && !looksGenerated(g.name));
            if (!dup) keep.push(f);
        }
        // 语义字段可能确实抓不到内容(站点把该列留空), 那也不该因此让用户失去这一列 ——
        // 这里只在"冗余"时丢弃, 不做"空值"判断(空值判断在提取阶段才有意义)。
        return keep;
    };

    // ---- 从样本列表项推断字段 ----
    const sampleFields = (sample) => {
        const fields = [];
        const used = new Set();
        // 上限放宽到 12: 除了文本字段, 还要给 image / audio / video 留位置 ——
        // 图片下载与音频下载插件都是按**字段名**取地址的, 规则里没有对应键就下不到东西。
        const MAX_FIELDS = 12;
        const push = (name, sel, attr) => {
            if (!sel || fields.length >= MAX_FIELDS) return;
            const key = sel + '|' + (attr || '');
            if (used.has(key)) return;
            used.add(key);
            fields.push({ name: name, selector: sel, attribute: attr });
        };

        // ---- 图片 ----
        // 优先 data-* 懒加载属性: 很多站点的 src 是占位图, 真地址在 data-src / data-original。
        // 实测堆糖等图站: <img src="占位" data-src="真图"> —— 只取 src 会下到一堆占位图。
        const LAZY_ATTRS = ['data-src', 'data-original', 'data-lazy-src', 'data-actualsrc', 'data-echo'];
        const img = sample.querySelector('img');
        if (img) {
            const lazyAttr = LAZY_ATTRS.find(a => (img.getAttribute(a) || '').trim());
            if (lazyAttr) {
                push('image', relSelector(sample, img), lazyAttr);
                if (fields.length < MAX_FIELDS) push('thumb', relSelector(sample, img), 'src');
            } else if (img.getAttribute('src')) {
                push('image', relSelector(sample, img), 'src');
            }
            // srcset 里通常是最高清的那张
            const srcset = (img.getAttribute('srcset') || '').trim();
            if (srcset && fields.length < MAX_FIELDS && !lazyAttr) {
                push('image_srcset', relSelector(sample, img), 'srcset');
            }
        }
        // 背景图(部分图站用 background-image)
        if (!img) {
            // 优先数据属性: 值就是干净的 URL, 不需要再解析 CSS
            const BG_ATTRS = ['data-bg', 'data-background', 'data-bg-src', 'data-background-image'];
            let done = false;
            for (const a of BG_ATTRS) {
                const el = sample.querySelector('[' + a + ']');
                if (el && (el.getAttribute(a) || '').trim()) {
                    push('image', relSelector(sample, el), a);
                    done = true;
                    break;
                }
            }
            if (!done) {
                for (const el of sample.querySelectorAll('*')) {
                    if (fields.length >= MAX_FIELDS) break;
                    const style = (el.getAttribute('style') || '');
                    if (/background(-image)?\s*:\s*url\(/i.test(style)) {
                        // style 属性带的是整段 CSS, 需要 "url" 变换把 url(...) 解出来
                        push('image', relSelector(sample, el), 'style');
                        break;
                    }
                }
            }
        }

        // ---- 音频 ----
        const audio = sample.querySelector('audio[src], audio source[src], a[href$=".mp3" i], a[href$=".m4a" i], a[href$=".ogg" i], a[href$=".wav" i], a[href$=".flac" i]');
        if (audio) {
            const attr = audio.getAttribute('href') ? 'href' : 'src';
            push('audio', relSelector(sample, audio), attr);
        }
        // ---- 视频(顺带, 同属媒体下载插件关心的字段) ----
        const video = sample.querySelector('video[src], video source[src], a[href$=".mp4" i], a[href$=".webm" i]');
        if (video) {
            const attr = video.getAttribute('href') ? 'href' : 'src';
            push('video', relSelector(sample, video), attr);
        }

        // 首个链接: 有文字 -> title(文本) + link(href); 无文字 -> 纯链接
        const link = sample.querySelector('a[href]');
        if (link) {
            const sel = relSelector(sample, link);
            const text = ((link.innerText || '')).trim();
            if (sel && text) {
                // 首个链接不一定是标题 —— 网易云音乐的作品卡里第一个 <a> 是歌手链接。
                // 交给 semanticName 判定: 它认出 /artist 就会命名成 artist 而不是 title。
                const nm = semanticName(link, text, 'title');
                if (nm === 'title') {
                    push('title', sel, null);
                } else {
                    push(nm, sel, null);
                }
                push('link', sel, 'href');
            } else if (sel) {
                push('link', sel, 'href');
            }
        }
        // 价格类叶子文本
        sample.querySelectorAll('*').forEach(el => {
            if (fields.length >= MAX_FIELDS || el.childElementCount !== 0) return;
            const t = ((el.innerText || '')).trim();
            if (!t || t.length > 60) return;
            if (/^[¥$€£]|￥|\d{1,3}(,\d{3})*(\.\d+)?\s*元|\d+\.\d{2}\b/.test(t)) {
                push('price', relSelector(sample, el), null);
            }
        });
        // 其余叶子文本
        sample.querySelectorAll('*').forEach(el => {
            if (fields.length >= MAX_FIELDS || el.childElementCount !== 0) return;
            const t = ((el.innerText || '')).trim();
            if (!t || t.length < 2 || t.length > 60) return;
            const cls = stableClasses(el);
            const tag = el.tagName.toLowerCase();
            // 无 class 的裸标签给中性名(后续归一化: text -> title)
            const base = cls.length ? cls[0] : (tag === 'a' ? 'text' : tag);
            push(semanticName(el, t, base), relSelector(sample, el), null);
        });
        return dropRedundantFields(sample, fields);
    };

    // ---- 重复结构识别 ----
    const detectLists = () => {
        const candidates = [];
        const seen = new Set();
        const containers = document.querySelectorAll('ul, ol, table tbody, div, section, main, article');
        containers.forEach(parent => {
            if (candidates.length >= 12) return;
            // 认定"这是一个列表项"的条件(满足其一):
            //   1. 自身文本够长(>10 字) —— 排除纯装饰性容器;
            //   2. 含有链接或图片 —— 图库/商品墙这类条目的文字往往很短(甚至只有一个
            //      缩略图 + 一行标题), 只按文本长度会把它们整片漏掉。
            const children = Array.from(parent.children).filter(
                c => !SKIP[c.tagName] && (
                    ((c.innerText || '')).trim().length > 10 ||
                    c.querySelector('a[href], img[src]')
                )
            );
            if (children.length < 3) return;
            const groups = new Map();
            children.forEach(c => {
                const sig = c.tagName + '|' + stableClasses(c).slice().sort().join('.');
                if (!groups.has(sig)) groups.set(sig, []);
                groups.get(sig).push(c);
            });
            for (const arr of groups.values()) {
                if (arr.length < 3 || candidates.length >= 12) continue;
                const tag = arr[0].tagName.toLowerCase();
                const cls = stableClasses(arr[0]);
                const parentSel = uniqSelector(parent);
                let itemSel = null;
                if (cls.length) {
                    const cand = parentSel + ' > ' + tag + '.' + cls.map(cssEscape).join('.');
                    if (document.querySelectorAll(cand).length >= arr.length) itemSel = cand;
                }
                if (!itemSel) itemSel = parentSel + ' > ' + tag;
                const hits = document.querySelectorAll(itemSel).length;
                if (hits < 3 || seen.has(itemSel)) continue;
                seen.add(itemSel);
                candidates.push({
                    item_selector: itemSel,
                    container_selector: parentSel,
                    count: hits,
                    sample_fields: sampleFields(arr[0]),
                    sample_html: arr[0].outerHTML.slice(0, 2000)
                });
            }
        });

        // ---- 合并"同一个父容器下被拆开的几组" ----
        // 瀑布流/多列布局里, 列表项的 class 会带上"列"的信息(duitang 就是
        // `div.woo.co0/co1/co2`), 于是 24 张卡片被拆成 7/9/8 三组, 每组都被当成一个
        // 独立列表 —— 最终规则只覆盖**一列**, 另外两列静默丢失。
        //
        // 判据: 同一个父容器下, 若干组元素互不重叠, 而它们的**并集**恰好能被一个
        // 更宽的选择器选中 —— 那就说明它们本来就是一个列表, 合并之。
        const mergeSplitGroups = (cands) => {
            const byParent = new Map();
            for (const c of cands) {
                const key = c.container_selector || '';
                if (!byParent.has(key)) byParent.set(key, []);
                byParent.get(key).push(c);
            }
            const merged = [];
            for (const [parentSel, group] of byParent) {
                group.sort((a, b) => b.count - a.count);
                const leader = group[0];
                // 只有"最大的那组明显不是全部"时才值得合并(否则本来就没被拆)
                const unionSize = group.reduce((s, c) => s + c.count, 0);
                if (group.length < 2 || unionSize <= leader.count) {
                    merged.push(...group);
                    continue;
                }
                let parent = null;
                try { parent = document.querySelector(parentSel); } catch (e) { parent = null; }
                if (!parent) { merged.push(...group); continue; }

                // 候选的"更宽选择器": 父级直接子元素里, 与各组同 tag 的那些
                const tags = new Set();
                try {
                    for (const el of document.querySelectorAll(leader.item_selector)) tags.add(el.tagName.toLowerCase());
                } catch (e) { /* 忽略 */ }
                const tries = [];
                if (parentSel) {
                    for (const t of tags) tries.push(parentSel + ' > ' + t);
                }
                // 也试试"公共 class": 各组元素共享的那个 class 往往就是列表项的本质
                const classCount = new Map();
                for (const c of group) {
                    try {
                        for (const el of document.querySelectorAll(c.item_selector)) {
                            for (const cl of stableClasses(el)) classCount.set(cl, (classCount.get(cl) || 0) + 1);
                        }
                    } catch (e) { /* 忽略 */ }
                }
                for (const [cl, n] of classCount) {
                    if (n >= unionSize) {
                        for (const t of tags) tries.push(t + '.' + cssEscape(cl));
                    }
                }
                let picked = null;
                for (const sel of tries) {
                    let got;
                    try { got = document.querySelectorAll(sel); } catch (e) { continue; }
                    if (got.length === unionSize) { picked = { sel, got }; break; }
                }
                if (!picked) { merged.push(...group); continue; }

                // 用并集里第一个元素的 selector 相对路径重新采字段
                merged.push({
                    item_selector: picked.sel,
                    container_selector: parentSel,
                    count: picked.got.length,
                    sample_fields: sampleFields(picked.got[0]),
                    sample_html: picked.got[0].outerHTML.slice(0, 2000)
                });
            }
            return merged;
        };

        let finalCandidates = mergeSplitGroups(candidates);
        finalCandidates.sort((a, b) => b.count - a.count);
        return finalCandidates;
    };

    // ---- 分页识别 ----
    // ---- 翻页检测 ----
    //
    // 三层策略, 从"最明确"到"最通用":
    //
    //   A) 文案: 下一页 / 次へ / next / › 等(多语言都要覆盖 —— 站点按 locale 渲染)
    //   B) rel="next" 标准标记
    //   C) **数字页码**: 当前页由 aria-current="true" 标记, 下一页 = 当前页 + 1
    //
    // 第 C 层是实测逼出来的: pixiv 搜索页的分页是一个 <nav>, 里面只有数字页码
    // (1..7)与两个**没有文字**的箭头链接(靠 x 坐标区分上一页/下一页),
    // 于是 A、B 都匹配不到 —— 报告里 pagination 直接是 None, 界面显示"未检测到下一页",
    // 用户明明能看到翻页按钮。而数字页码是极常见的翻页形态(电商/论坛/图站),
    // 而且它比文案更可靠: 文案会因语言/图标而缺失, 页码与 ?p= 参数则是结构性的。
    const detectPagination = () => {
        const visible = (el) => {
            if (!el) return false;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const textOf = (el) => ((el.innerText || el.textContent || '')).trim();
        const hrefOf = (el) => {
            const a = el.tagName === 'A' ? el : el.closest('a');
            return a ? (a.getAttribute('href') || '') : '';
        };

        // ---- A) 文案 ----
        const pat = /(下一页|下页|后一页|次へ|次のページ|次ページ|もっと見る|続きを(見|読)む|next\s*page|^next$|older|›|»|>>|→)/i;
        const prevPat = /(上一页|前一页|前一頁|前へ|prev|‹|«|←)/i;
        for (const el of document.querySelectorAll('a, button, [role="button"]')) {
            if (!visible(el)) continue;
            const t = textOf(el);
            if (!t || t.length > 20 || !pat.test(t) || prevPat.test(t)) continue;
            const href = hrefOf(el);
            const target = href ? (el.tagName === 'A' ? el : el.closest('a')) : el;
            return {
                next_selector: uniqSelector(target),
                next_text: t.slice(0, 20),
                next_href: href,
                strategy: 'text',
            };
        }

        // ---- B) rel="next" ----
        const rel = document.querySelector('a[rel="next"], link[rel="next"]');
        if (rel) {
            const href = rel.getAttribute('href') || '';
            return {
                next_selector: uniqSelector(rel.tagName === 'A' ? rel : rel),
                next_text: textOf(rel).slice(0, 20),
                next_href: href,
                strategy: 'rel-next',
            };
        }

        // ---- C) 数字页码 ----
        // 找到"当前页"这个数字, 然后取"当前页 + 1"的链接。
        // 当前页的判据(按可靠度): aria-current > 高亮 class(active/current/selected)
        //                       > 无 href 的那个(很多分页把当前页渲染成不可点的 span)
        const numericLinks = [];
        for (const el of document.querySelectorAll('a[href], button, span, li')) {
            const t = textOf(el);
            if (!/^\d{1,4}$/.test(t)) continue;
            if (!visible(el)) continue;
            // 只要"页码容器"里的: 父/祖父里有 >= 3 个数字兄弟, 排除正文里的孤立数字
            const box = el.parentElement;
            if (!box) continue;
            const sibs = [...box.children].filter(c => /^\d{1,4}$/.test(textOf(c)));
            if (sibs.length < 3) continue;
            const a = el.tagName === 'A' ? el : el.closest('a');
            numericLinks.push({
                el, a, n: parseInt(t, 10),
                href: a ? (a.getAttribute('href') || '') : '',
                current: el.getAttribute('aria-current') === 'true'
                    || el.getAttribute('aria-current') === 'page'
                    || /(^|[\s-])(active|current|selected|is-active)([\s-]|$)/i.test(
                        (el.className || '').toString())
                    || !a,
            });
        }
        if (numericLinks.length >= 3) {
            const cur = numericLinks.find(x => x.current) || numericLinks[0];
            const next = numericLinks.find(x => x.n === cur.n + 1 && x.a && x.href)
                || numericLinks.filter(x => x.a && x.href && x.n > cur.n)
                    .sort((a, b) => a.n - b.n)[0];
            if (next && next.a) {
                return {
                    next_selector: uniqSelector(next.a),
                    next_text: String(next.n),
                    next_href: next.href,
                    strategy: 'numeric',
                    current_page: cur.n,
                };
            }
        }

        // ---- D) 兜底: 明确的"页码 URL"链接里, 取 p 值最小的那个 ----
        // 用于"当前页不是数字按钮"的站点(例如只有 上一页/下一页 两个链接但都没文字)。
        const pageParam = /[?&](?:p|page|pg|pageno|pagenum)=(\d+)/i;
        let best = null;
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            const m = pageParam.exec(href);
            if (!m) continue;
            const n = parseInt(m[1], 10);
            const here = pageParam.exec(location.search);
            const curN = here ? parseInt(here[1], 10) : 1;
            if (n === curN + 1) {
                // 正好是"当前页 + 1" —— 最强信号, 直接用
                return {
                    next_selector: uniqSelector(a),
                    next_text: textOf(a).slice(0, 20) || String(n),
                    next_href: href,
                    strategy: 'page-param',
                    current_page: curN,
                };
            }
            if (visible(a) && (!best || n < best.n)) best = { a, href, n };
        }
        return null;
    };

    // ---- 结构化元数据 ----
    const collectMetadata = () => {
        const meta = { json_ld: [], open_graph: {}, microdata: [] };
        document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
            try { meta.json_ld.push(JSON.parse(s.textContent)); } catch (e) { /* 忽略坏 JSON */ }
        });
        document.querySelectorAll('meta[property^="og:"], meta[name^="twitter:"]').forEach(m => {
            const key = m.getAttribute('property') || m.getAttribute('name');
            if (key) meta.open_graph[key] = m.getAttribute('content');
        });
        document.querySelectorAll('[itemprop]').forEach(el => {
            if (meta.microdata.length >= 50) return;
            const item = { itemprop: el.getAttribute('itemprop'), tag: el.tagName.toLowerCase() };
            if (el.hasAttribute('content')) item.content = el.getAttribute('content');
            else if (el.hasAttribute('href')) item.content = el.getAttribute('href');
            else if (el.hasAttribute('src')) item.content = el.getAttribute('src');
            else item.text = ((el.innerText || '')).trim().slice(0, 200);
            meta.microdata.push(item);
        });
        return meta;
    };

    // ---- 简化 DOM 树 ----
    //
    // 三个上限的取值来自**实测**(pixiv 已登录首页, 1903 个节点 / 60 张图 / 49 个作品链接):
    //
    //   元素深度分布     img  avg 17.3  max 26
    //                  作品链接 avg 19.0  max 25
    //                  侧边栏   深度 11   <- 反而很浅
    //
    //   旧配置 (maxDepth=10, maxNodes=600) 的后果:
    //        img 覆盖 24.6%、作品链接覆盖 **0%** —— 整个正文区被无声丢弃,
    //        只剩侧边栏/页头/页脚。用户看到的正是这个: "真实浏览器与 DOM 树明显不同"。
    //   实测各档覆盖(节点数为实际产出):
    //        (10,600)  298 节点  img 24.6%  art   0%
    //        (18,1200) 1142 节点 img 57.4%  art  52.4%
    //        (22,1200) 1201 节点 img 78.7%  art  76.2%
    //        (24,2000) 1832 节点 img 93.4%  art 100%   <- 采用
    //
    // childCap 从 20 提到 80: 实测某些容器一层就有 80 个子节点, slice(0,20) 会丢掉
    // 后面 3/4 的同级内容。
    const TREE_MAX_DEPTH = 24;
    //: 节点上限 1200。实测(pixiv 已登录首页)各档的"送达后覆盖":
    //:
    //:   节点  负载   截断  img覆盖  作品覆盖
    //:   1000  40KB    是     33%      42%
    //:   1200  40KB    是     54%      64%
    //:   1200  60KB    **否**  61%      90%   <- 采用
    //:   1500  90KB    否     88%     100%
    //:
    //: 注意一个反直觉的现象: **提高节点上限反而会降低送达覆盖** —— 因为负载上限是固定的,
    //: 树越大, 头尾截断要丢掉的"中间段"就越多, 而正文正好在被丢的中间。
    //: 所以关键是让整棵树**不超过负载上限**(1200 节点 ≈ 61KB < 60KB 上限), 而不是拼命多抓。
    const TREE_MAX_NODES = 1200;
    const TREE_CHILD_CAP = 80;
    //: 输出缩进的层数上限。**缩进是纯开销**: 真实深度可以到 24, 每层 2 空格意味着
    //: 最深层每行光缩进就 48 字符 —— 实测把树撑到 107KB / 1782 行, 而 AI prompt 只有
    //: 8000 字符预算, 等于 93% 的内容根本送不进模型。
    //: 遍历仍然按真实深度裁剪(靠 maxDepth), 只有**打印**时把缩进压平到这一层。
    //: 层级信息并未丢失: 相邻行的缩进差依然能看出父子关系。
    const TREE_INDENT_CAP = 12;

    // 子节点排序: **内容优先**。
    //
    // 为什么必须排: 深度优先会把"浅而啰嗦"的侧边栏/页脚先写满预算, 正文(深)反而排在
    // 输出的最后。而消费方有两处只看前半段 —— AI prompt 只取前 4000 字符, 人也是从上往下读 ——
    // 于是正文即使被抓到也等于没抓到。把带图/带链接/带文本的子树提前, 让有限预算花在
    // 有数据的地方。
    const contentScore = (el) => {
        let score = 0;
        const imgs = el.querySelectorAll('img');
        score += imgs.length * 6;
        const anchors = el.querySelectorAll('a[href]');
        score += anchors.length * 3;
        const txt = (el.innerText || '');
        score += Math.min(txt.length, 400) / 20;
        // 带语义标签的加分: 列表/文章/卡片通常就是数据区
        if (/^(UL|OL|ARTICLE|SECTION|MAIN|TABLE|FIGURE)$/.test(el.tagName)) score += 4;
        return score;
    };
    const orderedChildren = (el) => {
        const kids = Array.from(el.children);
        if (kids.length < 2) return kids;
        return kids
            .map((c, i) => ({ c, i, s: contentScore(c) }))
            .sort((a, b) => (b.s - a.s) || (a.i - b.i))   // 同分保持原顺序, 输出才稳定
            .map(x => x.c);
    };

    const simplifiedTree = (maxDepth, maxNodes) => {
        const lines = [];
        let nodes = 0;
        const walk = (el, depth, prefix) => {
            if (nodes >= maxNodes) return;
            if (depth > maxDepth) {
                if (!truncated.depth) truncated.depth = depth;
                return;
            }
            if (SKIP[el.tagName]) return;
            const tag = el.tagName.toLowerCase();
            const id = el.id ? '#' + el.id : '';
            const cls = stableClasses(el).slice(0, 3).map(c => '.' + c).join('');
            let text = '';
            if (!el.childElementCount) {
                text = ' ' + ((el.innerText || el.textContent || '')).trim().slice(0, 60).replace(/\s+/g, ' ');
            }
            const href = (tag === 'a' && el.getAttribute('href')) ? ' -> ' + el.getAttribute('href').slice(0, 80) : '';
            // 图片也把地址带出来, 和链接的 ' -> href' 对称。
            //
            // 为什么必须带: 此前树里的图片行长这样 —— `img.kHdoFK`, 只有标签名和 class。
            // 于是(a)人在树里根本看不出哪里有图片、哪个是数据图; (b)也没法用它判断
            // 图片字段该指向哪。而 <a> 一直是带 href 的, 这个不对称纯属疏漏。
            // 优先取懒加载属性, 与 sampleFields 的口径保持一致(很多图站 src 是占位图)。
            let media = '';
            if (tag === 'img' || tag === 'source') {
                const LAZY = ['data-src', 'data-original', 'data-lazy-src', 'data-actualsrc', 'data-echo'];
                const lazyAttr = LAZY.find(x => (el.getAttribute(x) || '').trim());
                const u = lazyAttr ? el.getAttribute(lazyAttr) : (el.getAttribute('src') || el.getAttribute('srcset') || '');
                if (u) media = (lazyAttr ? ' [' + lazyAttr + '] -> ' : ' -> ') + ellipsizeUrl(u.trim().split(/\s+/)[0], 150);
            } else if (tag === 'video' || tag === 'audio') {
                const u = el.getAttribute('src') || '';
                if (u) media = ' -> ' + ellipsizeUrl(u, 150);
            }
            lines.push(prefix + tag + id + cls + text + href + media);
            nodes++;
            if (nodes >= maxNodes) {
                truncated.nodes = true;
                return;
            }
            // 子节点前缀在缩进封顶后不再增长, 省下大量字符预算给真正的内容
            const nextPrefix = depth + 1 >= TREE_INDENT_CAP ? prefix : prefix + '  ';
            orderedChildren(el).slice(0, TREE_CHILD_CAP)
                .forEach(c => walk(c, depth + 1, nextPrefix));
        };
        walk(document.body, 0, '');
        return lines.join('\n');
    };

    const domStats = () => ({
        total_elements: document.querySelectorAll('*').length,
        links: document.querySelectorAll('a[href]').length,
        images: document.querySelectorAll('img').length,
        iframes: document.querySelectorAll('iframe').length,
        forms: document.querySelectorAll('form').length
    });

    // 记录"这次是不是真的被截断了"。
    // 早先这个标记在前端被用来显示「已截断」徽标, 但后端**从未赋过值** ——
    // 界面上那个提示永远不会出现, 用户无从知道结构树其实不完整。
    const truncated = { nodes: false, depth: 0 };

    const tree = simplifiedTree(TREE_MAX_DEPTH, TREE_MAX_NODES);

    return {
        title: document.title || '',
        candidates: detectLists(),
        pagination: detectPagination(),
        metadata: collectMetadata(),
        tree: tree,
        tree_truncated: !!(truncated.nodes || truncated.depth),
        tree_info: {
            nodes: tree.split('\n').length,
            max_depth: TREE_MAX_DEPTH,
            depth_capped: truncated.depth || 0,
            node_capped: !!truncated.nodes,
            total_elements: document.querySelectorAll('*').length,
            images_in_dom: document.querySelectorAll('img').length,
            images_in_tree: (tree.match(/^\s*img/gm) || []).length
        },
        dom_stats: domStats()
    };
};
"""


class StructureAnalyzer:
    """页面结构分析器。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    async def _wait_for_frames_to_settle(
        page: Page, *, timeout: float = 15.0, interval: float = 0.5,
        stable_rounds: int = 2, min_elements: int = 400,
    ) -> None:
        """等内层 frame 把内容渲染出来, 供"外壳 + 内嵌 iframe"的站点使用。

        **为什么不能只看主文档**: 外壳页在 ``domcontentloaded`` 时就已 complete, 而内层
        iframe 的内容是之后异步注入的。此前实测网易云音乐: 外壳 284~336 个元素(只有导航和
        播放条), 内容 frame 最终 949~955 个元素(30 条歌曲)。分析早于内容注入时, 内层 frame
        只有 3~257 个元素, 会被判为"没什么内容", 于是选中外壳 —— 用户看到的就是
        "有时候能抓到、有时候一条都没有"。

        **为什么"稳定"还不够**: 一个始终没填充的内层 frame 同样"稳定"。所以还要求
        **最丰富的那个 frame** 元素数达到 ``min_elements``; 达不到就继续等, 直到超时。

        **为什么要按 frame 分别看**: 只看"主文档 + 所有 iframe 的元素总数"会把外壳自己的
        300 多个元素也算进去, 于是"总数已经够了"而内层其实还是空的。
        """
        import asyncio as _asyncio

        probe = """() => {
            const out = [document.querySelectorAll('*').length];
            for (const f of document.querySelectorAll('iframe')) {
                try {
                    const d = f.contentDocument;
                    if (d) out.push(d.querySelectorAll('*').length);
                } catch (e) { /* 跨域 iframe 读不到, 忽略 */ }
            }
            return out;
        }"""
        try:
            last_max = -1
            stable = 0
            loop = _asyncio.get_event_loop()
            deadline = loop.time() + max(0.0, timeout)
            while loop.time() < deadline:
                try:
                    counts = await page.evaluate(probe)
                    richest = max(counts) if counts else -1
                except Exception:  # noqa: BLE001
                    richest = -1
                if richest == last_max and richest >= min_elements:
                    stable += 1
                    if stable >= stable_rounds:
                        return
                else:
                    stable = 0
                last_max = richest
                await _asyncio.sleep(interval)
        except Exception as exc:  # noqa: BLE001 - 等待失败不该影响分析
            logger.debug(f"等待 frame 稳定时出错(忽略): {exc}")

    @staticmethod
    def _accessible_frames(page: Page) -> list[Any]:
        """返回可读的 frame 列表: 主文档在前, 其后是同源的子 frame。

        跨域 frame 读 DOM 会抛异常, 直接跳过 —— 它们本来也抓不到。
        `about:blank` 与 `data:` 之类的空 frame 一并跳过。
        """
        out: list[Any] = []
        try:
            frames = list(page.frames)
        except Exception:  # noqa: BLE001
            return out
        for f in frames:
            url = (getattr(f, "url", "") or "")
            if url.startswith(("about:", "data:", "blob:")):
                continue
            out.append(f)
        return out

    async def analyze(self, page: Page) -> PageStructureReport:
        """对当前页面执行结构分析, 返回结构报告。

        **会下探到同源 iframe。** 部分站点(实测网易云音乐)采用"外壳 + 内嵌 iframe":
        主文档只有导航与播放条, 目标列表全在内层 frame 里。只分析主文档的话,
        候选列表是 0, 要么报"没有可提取的列表", 要么从外壳里提出一堆导航链接。
        因此先分析主文档, 若没有候选列表, 再依次分析各同源子 frame, 取内容最丰富的那个,
        并把 frame 名称记进报告, 供提取阶段使用同一个 frame。
        """
        url = page.url
        # iframe 里的内容常常晚于主文档渲染完成(实测网易云音乐: 外壳 domcontentloaded
        # 时内层 frame 还在导航, 元素数只有几十; 搜索结果是随后异步填进去的)。
        # 若不等待就打分, 会选中"当时较大"的主文档, 于是又回到只看到外壳的老问题。
        # 这里给一次重试窗口: 若各 frame 的元素总数还在增长, 就等一下再看。
        await self._wait_for_frames_to_settle(page)

        frames = self._accessible_frames(page)

        best_frame: Any = None
        best_raw: dict[str, Any] | None = None
        best_name = ""
        best_score: tuple[int, int, int] | None = None
        main_raw: dict[str, Any] | None = None
        for frame in frames:
            try:
                raw: dict[str, Any] = await frame.evaluate(_ANALYZE_JS)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"frame 分析失败({getattr(frame, 'url', '')[:60]}): {exc}")
                continue
            name = "" if frame is page.main_frame else (getattr(frame, "name", "") or "")
            if frame is page.main_frame:
                main_raw = raw
            cands = raw.get("candidates") or []
            stats = raw.get("dom_stats") or {}
            # "内容更丰富"的判据: 先看候选列表数量, 再看元素数与图片数
            score = (len(cands), int(stats.get("total_elements") or 0), int(stats.get("images") or 0))
            if best_score is None or score > best_score:
                best_frame, best_raw, best_name, best_score = frame, raw, name, score

        if best_raw is None:
            logger.error(f"页面结构分析失败 {url}: 所有 frame 都不可读")
            return PageStructureReport(url=url, title="")

        raw = best_raw
        frame_name = best_name
        main_cands = len((main_raw or {}).get("candidates") or [])
        if frame_name and len(raw.get("candidates") or []) > main_cands:
            logger.info(
                f"内容位于 iframe {frame_name!r}: "
                f"主文档候选 {main_cands} 个 -> 该 frame {len(raw.get('candidates') or [])} 个"
            )

        candidates = [ListCandidate(**c) for c in raw.get("candidates", [])]
        pag_raw = raw.get("pagination")
        tree = raw.get("tree", "") or ""
        tree_info = raw.get("tree_info") or {}
        report = PageStructureReport(
            url=url,
            title=raw.get("title", ""),
            simplified_tree=tree,
            simplified_tree_truncated=bool(raw.get("tree_truncated")),
            content_frame=frame_name,
            candidate_lists=candidates,
            pagination=PaginationInfo(**pag_raw) if pag_raw else None,
            metadata=raw.get("metadata", {}),
            dom_stats=raw.get("dom_stats", {}),
        )
        # 把"正文到底有没有被抓到"记进日志。这是本模块最隐蔽的失效模式:
        # 树看着挺长(全是导航), 但数据区一张图都没有, 而界面与调用方都察觉不到。
        imgs_dom = int(tree_info.get("images_in_dom") or 0)
        imgs_tree = int(tree_info.get("images_in_tree") or 0)
        cover = (imgs_tree / imgs_dom * 100) if imgs_dom else 100.0
        logger.info(
            f"结构分析完成: {truncate(url, 80)} | 候选列表 {len(candidates)} 个 "
            f"| 分页 {'✓' if report.pagination else '✗'} | 节点 {report.dom_stats.get('total_elements', 0)} "
            f"| 树 {tree_info.get('nodes', 0)} 行 (图片覆盖 {cover:.0f}%"
            f"{', 已达深度上限' if tree_info.get('depth_capped') else ''}"
            f"{', 已达节点上限' if tree_info.get('node_capped') else ''})"
        )
        if imgs_dom >= 5 and cover < 50:
            logger.warning(
                f"结构树只覆盖了 {cover:.0f}% 的图片(DOM {imgs_dom} 张 / 树内 {imgs_tree} 张) —— "
                "正文区可能被深度或节点上限截掉, 提取规则会缺少图片字段"
            )
        return report

    # ------------------------------------------------------------------
    # 规则引擎: 无 AI 时的降级方案
    # ------------------------------------------------------------------
    @staticmethod
    def build_rule_from_structure(
        report: PageStructureReport,
        max_pages: int = 1,
        field_limit: int = 8,
    ) -> Optional[ExtractionRule]:
        """根据结构报告生成默认提取规则(AI 不可用时的规则引擎降级方案)。

        评分策略(按重要性排序):
            score = 不同选择器数*4 + 字段数 + min(count, 20) + **图片字段奖励(6)**

        - "不同选择器数"权重最高: 导航/侧边栏列表的各字段常来自同一元素(如 title/link
          都取自 <a>), 而真实数据列表(作品卡片/商品卡)的字段来自不同元素。
        - **图片字段奖励是必须的**: 实测 pixiv 搜索页, 侧边栏"推荐标签"列表靠字段数
          以 14:12 压过了真正的作品列表, 于是生成的规则里没有 image 键、图片插件下不到图。
          对"抓图"这类目标来说, 有图片字段的候选几乎总是更该选的。
        """
        best: Optional[ListCandidate] = None
        best_score = -1.0
        for cand in report.candidate_lists:
            if len(cand.sample_fields) < 2:
                continue
            distinct = len({(f.get("selector"), f.get("attribute")) for f in cand.sample_fields})
            has_image = any(
                "image" in str(f.get("name", "")).lower()
                or "thumb" in str(f.get("name", "")).lower()
                or str(f.get("attribute", "")).lower() in ("src", "srcset", "data-src")
                for f in cand.sample_fields
            )
            score = distinct * 4 + len(cand.sample_fields) + min(cand.count, 20)
            if has_image:
                score += 6
            if score > best_score:
                best, best_score = cand, score
        if best is None:
            logger.warning("规则引擎: 结构报告中没有可用的候选列表")
            return None

        fields: list[FieldSpec] = []
        for f in best.sample_fields[:field_limit]:
            name = _normalize_field_name(f.get("name", ""), f.get("attribute"))
            fields.append(
                FieldSpec(
                    name=name,
                    selector=f.get("selector", ""),
                    attribute=f.get("attribute"),
                    transform=_default_transforms(name, f.get("attribute")),
                )
            )
        # 至少保证标题与链接存在
        if not any(f.name == "link" for f in fields) and any(f.attribute == "href" for f in fields):
            pass  # sample_fields 已含 link

        rule = ExtractionRule(
            mode="dom",
            list_rule=ListRule(item_selector=best.item_selector, fields=fields),
            source="rule",
            notes=f"规则引擎生成(候选区 {best.container_selector}, 重复项 {best.count})",
        )
        if report.pagination and report.pagination.next_selector:
            rule.pagination = PaginationRule(next_selector=report.pagination.next_selector, max_pages=max_pages)
        return rule


def _normalize_field_name(raw_name: str, attribute: Optional[str]) -> str:
    """把推断出的字段名归一化为语义名。"""
    name = (raw_name or "").strip().lower()
    if attribute == "href" or any(k in name for k in ("link", "href", "url")):
        return "link"
    if any(k in name for k in ("price", "价", "amount")):
        return "price"
    if any(k in name for k in ("title", "name", "标题", "名称", "text")):
        return "title"
    if any(k in name for k in ("img", "image", "pic", "图")):
        return "image"
    # 非法字符清理, 保证可作为 dict key / CSV 列名
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name) or "field"
    return cleaned


def _default_transforms(name: str, attribute: Optional[str]) -> list[str]:
    """按归一化字段名给出默认清洗管线(规则引擎降级时的启发式)。"""
    if name == "price":
        return ["price"]  # "£45.17" -> 45.17
    if name in ("link", "image", "audio", "video") or (attribute or "").lower() in (
        "src", "href", "data-src", "data-original", "data-bg", "srcset",
    ):
        return ["url"]  # 相对路径 -> 绝对 URL
    return ["strip"]
