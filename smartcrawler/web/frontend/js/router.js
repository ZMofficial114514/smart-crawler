/**
 * router.js —— 极简哈希路由
 * ---------------------------------------------------------------------------
 * 页面切换是纯前端行为,但仍然需要:
 *   - 深链接(#/settings 可以直接打开配置页);
 *   - 浏览器前进/后退可用;
 *   - 刷新后停在原页面。
 * 因此用 hash 路由,而不是纯内存状态。
 *
 * 页面定义集中在 PAGES,标题/副标题也放在这里,避免散落在各处。
 */

import { $$, $ } from './utils.js';
import { get, set } from './store.js';

export const PAGES = {
  crawl: {
    title: '抓取任务',
    subtitle: '用自然语言描述目标,或用精确规则控制提取',
    init: () => import('./pages/crawl.js').then((m) => m.init()),
  },
  analyze: {
    title: '结构分析',
    subtitle: '识别列表区、分页器与结构化元数据,为规则提供依据',
    init: () => import('./pages/analyze.js').then((m) => m.init()),
  },
  requests: {
    title: '网络抓包',
    subtitle: '捕获 XHR / fetch / WebSocket,找到真正的数据接口',
    init: () => import('./pages/requests.js').then((m) => m.init()),
  },
  results: {
    title: '结果与历史',
    subtitle: '查看任务记录、下载产出文件、回溯历史结果',
    init: () => import('./pages/results.js').then((m) => m.init()),
  },
  plugins: {
    title: '插件',
    subtitle: '启用内置能力, 或放入自己的插件来扩展下载、反爬与清洗逻辑',
    init: () => import('./pages/plugins.js').then((m) => m.init()),
  },
  settings: {
    title: '系统配置',
    subtitle: '所有可调参数都在这里,改动会写入 .env',
    init: () => import('./pages/settings.js').then((m) => m.init()),
  },
  about: {
    title: '关于与帮助',
    subtitle: '框架能力、运行环境与命令行等价用法',
    init: () => import('./pages/about.js').then((m) => m.init()),
  },
};

const inited = new Set();

/** 解析当前 hash 对应的页面名(带白名单校验) */
export function currentPage() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').trim();
  const name = raw.split('?')[0];
  return Object.prototype.hasOwnProperty.call(PAGES, name) ? name : 'crawl';
}

/**
 * 切换页面。
 * @param {string} name
 * @param {{silent?: boolean}} [opts] silent=true 时不改 hash(用于初始化)
 */
export function goToPage(name, opts = {}) {
  if (!PAGES[name]) name = 'crawl';

  for (const section of $$('.page')) {
    section.classList.toggle('is-active', section.id === `page-${name}`);
  }
  for (const item of $$('#nav .nav__item')) {
    item.classList.toggle('is-active', item.dataset.page === name);
  }

  const meta = PAGES[name];
  const titleNode = $('#pageTitle');
  const subNode = $('#pageSubtitle');
  if (titleNode) titleNode.textContent = meta.title;
  if (subNode) subNode.textContent = meta.subtitle;

  document.title = `${meta.title} · SmartCrawler 控制台`;
  set('ui.page', name);

  // 首次进入该页才执行 init(避免每次切页重复绑定事件)
  if (!inited.has(name)) {
    inited.add(name);
    Promise.resolve(meta.init()).catch((err) => console.error(`[router] 页面 ${name} 初始化失败:`, err));
  }

  if (!opts.silent && window.location.hash !== `#/${name}`) {
    window.location.hash = `#/${name}`;
  }

  // 移动端切页后自动收起侧边抽屉
  if (window.matchMedia('(max-width: 820px)').matches) {
    $('#app')?.classList.remove('is-drawer-open');
  }

  $('#content')?.scrollTo({ top: 0, behavior: 'smooth' });
}

export function initRouter() {
  for (const item of $$('#nav .nav__item')) {
    item.addEventListener('click', () => goToPage(item.dataset.page));
  }

  window.addEventListener('hashchange', () => goToPage(currentPage(), { silent: true }));

  // 其他模块可以派发 app:navigate 事件来跳页,避免彼此 import
  window.addEventListener('app:navigate', (event) => goToPage(event.detail));

  // 键盘快捷键: 1-4 切页(输入框内不触发)
  document.addEventListener('keydown', (event) => {
    if (event.target.closest('input, textarea, select, [contenteditable]')) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const map = { 1: 'crawl', 2: 'analyze', 3: 'requests', 4: 'settings', 5: 'plugins' };
    if (map[event.key]) {
      event.preventDefault();
      goToPage(map[event.key]);
    }
  });

  goToPage(currentPage(), { silent: true });
}

export default { PAGES, goToPage, initRouter, currentPage };
