/**
 * utils.js —— 通用工具
 * ---------------------------------------------------------------------------
 * 只放"与业务无关"的纯函数和极小的 DOM 助手。所有函数要么是纯函数,要么是
 * 幂等的 DOM 操作,方便在视图之间复用与单测。
 */

/* ==========================================================================
   选择器与 DOM
   ========================================================================== */
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/**
 * 极简元素构造器。
 *   el('div.card', { id: 'x' }, [el('span', {}, '文字')])
 * 第一个参数支持 "tag.class1.class2" 简写,避免到处写 createElement + className。
 */
export function el(spec, attrs = {}, children = []) {
  const [tag, ...classes] = String(spec).split('.');
  const node = document.createElement(tag || 'div');
  if (classes.length) node.className = classes.join(' ');

  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class' || key === 'className') {
      node.className = [node.className, value].filter(Boolean).join(' ');
    } else if (key === 'html') {
      node.innerHTML = value;
    } else if (key === 'text') {
      node.textContent = value;
    } else if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key === 'style' && typeof value === 'object') {
      Object.assign(node.style, value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value') {
      node.value = value;
    } else if (value === true) {
      node.setAttribute(key, '');
    } else {
      node.setAttribute(key, value);
    }
  }

  appendChildren(node, children);
  return node;
}

export function appendChildren(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** 清空容器并填入新内容(避免手写 innerHTML = '') */
export function mount(container, ...children) {
  if (!container) return container;
  container.replaceChildren();
  appendChildren(container, children.flat());
  return container;
}

/* ==========================================================================
   格式化
   ========================================================================== */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

export function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value.toFixed(value < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function formatNumber(n) {
  if (n === null || n === undefined) return '—';
  return new Intl.NumberFormat('zh-CN').format(n);
}

export function truncate(text, max = 90) {
  const s = String(text ?? '');
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

export function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 把"被当成字面量"的转义序列还原成真实字符。
 *
 * 场景: 后端返回的文本(简化 DOM 树 / 页面正文 / HTML 片段)有时会带着
 * `\n` `\t` `\u003C` 这类**字面量**转义 —— 它们经过一层 JSON 或 JS 字符串字面量时
 * 被转义了两次, 直接显示就成了 `body\n  div\n    span` 这样一行看不懂的东西。
 * 这里把它们还原成真正的换行/制表/字符, 让用户看到应有的排版。
 *
 * 只处理这些"确定无歧义"的序列, 不做通用反转义(那会把用户数据改坏)。
 */
export function unescapeDisplay(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\n')
    .replace(/\\t/g, '  ')
    .replace(/\\u003C/gi, '<')
    .replace(/\\u003E/gi, '>')
    .replace(/\\u0026/gi, '&')
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'");
}

/**
 * 把 DOM 树这类**逐行**文本整理成适合显示的形态。
 *
 * 三件事: 还原字面量转义、统一换行、把过长的单行折断(压缩过的 HTML 常见上万个字符
 * 挤在一行, 不折断会把容器撑爆且完全没法读)。
 */
export function formatTreeText(value, maxLineLength = 240) {
  const text = unescapeDisplay(value).replace(/\r\n?/g, '\n');
  return text
    .split('\n')
    .map((line) => {
      if (line.length <= maxLineLength) return line;
      // 在属性之间断开, 尽量不切断标签本身
      return line.replace(new RegExp(`(.{${maxLineLength}})`, 'g'), '$1\n');
    })
    .join('\n');
}

/**
 * 纯文本代码块着色: 只做转义 + 行号, **不做 JSON 着色**。
 *
 * 为什么需要它: DOM 树/HTML 片段根本不是 JSON, 用 JSON 着色器处理会把里面的引号、
 * 数字意外染色, 出现大片莫名其妙的颜色。这类内容只需要等宽 + 正确换行。
 */
export function renderPlainText(value, { maxLineLength = 240 } = {}) {
  const text = formatTreeText(value, maxLineLength);
  if (!text) return '';
  return text
    .split('\n')
    .map((line, i) => {
      const num = String(i + 1).padStart(4, ' ');
      return `<span class="pt-num">${num}</span>  ${escapeHtml(line)}`;
    })
    .join('\n');
}

/**
 * 极简 JSON 语法着色。仅用于展示 —— 输入始终先做 HTML 转义,不存在注入面。
 * 支持折叠超长字符串,避免一条 500KB 的响应把 DOM 撑爆。
 */
export function highlightJson(value, maxStringLength = 400) {
  const json = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  if (json === undefined) return '';

  return escapeHtml(json).replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = 'j-num';
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? 'j-key' : 'j-str';
      } else if (/true|false/.test(match)) {
        cls = 'j-bool';
      } else if (/null/.test(match)) {
        cls = 'j-null';
      }
      const shown =
        match.length > maxStringLength && cls === 'j-str'
          ? `${match.slice(0, maxStringLength)}…[已截断 ${match.length - maxStringLength} 字符]"`
          : match;
      return `<span class="${cls}">${shown}</span>`;
    },
  );
}

/** 下载任意文本为文件(前端导出 CSV/JSON 用) */
export function downloadText(filename, text, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  // 交给下一轮事件循环释放,确保下载已开始
  setTimeout(() => URL.revokeObjectURL(url), 1200);
}

/** 二维数组 -> CSV 文本(自动加 BOM,Excel 打开不乱码) */
export function toCsv(columns, rows) {
  const cell = (v) => {
    if (v === null || v === undefined) return '';
    const s = typeof v === 'object' ? JSON.stringify(v) : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [columns.map(cell).join(',')];
  for (const row of rows) lines.push(columns.map((c) => cell(row[c])).join(','));
  return `\uFEFF${lines.join('\r\n')}`;
}

/* ==========================================================================
   交互助手
   ========================================================================== */
export function debounce(fn, wait = 220) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function throttle(fn, wait = 120) {
  let last = 0;
  let pending = null;
  return (...args) => {
    const now = Date.now();
    if (now - last >= wait) {
      last = now;
      fn(...args);
    } else if (!pending) {
      pending = setTimeout(() => {
        pending = null;
        last = Date.now();
        fn(...args);
      }, wait - (now - last));
    }
  };
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text));
    return true;
  } catch {
    // 非安全上下文(如 http://192.168.x.x)下 clipboard API 不可用,退回 execCommand
    try {
      const ta = el('textarea', { value: String(text), style: { position: 'fixed', opacity: '0' } });
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

/** 让按钮进入"加载中"状态,返回恢复函数 */
export function withLoading(button, label) {
  if (!button) return () => {};
  const spinner = $('.btn__spinner', button);
  const labelNode = $('.btn__label', button) || button;
  const original = labelNode.textContent;
  button.classList.add('is-loading');
  button.disabled = true;
  if (spinner) spinner.hidden = false;
  if (label) labelNode.textContent = label;
  return () => {
    button.classList.remove('is-loading');
    button.disabled = false;
    if (spinner) spinner.hidden = true;
    labelNode.textContent = original;
  };
}

/** 滚动到元素并高亮一下(用于"定位到某条记录") */
export function flashScroll(node) {
  if (!node) return;
  node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  node.style.animation = 'flash-highlight 0.9s var(--ease-out)';
  setTimeout(() => {
    node.style.animation = '';
  }, 950);
}

export function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

export function isUrlLike(value) {
  return /^https?:\/\//i.test(String(value || '').trim());
}

/** 把长 URL 折叠成 "host/…/tail" 形式,便于在窄列里辨识 */
export function prettyUrl(url, head = 34, tail = 40) {
  const s = String(url || '');
  if (s.length <= head + tail + 3) return s;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}
