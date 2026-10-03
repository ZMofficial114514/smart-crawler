/**
 * store.js —— 极简响应式状态容器
 * ---------------------------------------------------------------------------
 * 没有引入任何框架,但需要一个"多处共享、改动后自动刷新相关视图"的地方:
 * 健康状态(顶栏芯片)、配置(设置页)、当前任务(抓取页时间线)、任务历史/文件
 * (结果页)都要跨页面共享。

 * 实现方式: 发布/订阅 + 点路径读取。set() 支持 "a.b.c" 形式的深路径更新,
 * subscribe() 可以只监听某个前缀(`task.`),避免无关刷新。
 */

const state = {
  /** 健康状态快照(来自 /api/health 与 /ws/health) */
  health: null,
  /** 配置 Schema + 当前取值(来自 /api/config) */
  config: null,
  /** 本地未保存的配置改动 { "browser.headless": false } */
  configDirty: {},
  /** 当前正在跟踪的任务 { id, kind, status, steps, progress, ... } */
  task: null,
  /** 结果: 最近一次抓取/分析/抓包的载荷 */
  lastCrawl: null,
  lastAnalyze: null,
  lastRequests: null,
  /** 任务历史与产出文件 */
  tasks: [],
  files: [],
  /** 界面状态 */
  ui: {
    page: 'crawl',
    sidebarCollapsed: false,
    logOpen: false,
    logLevel: 'INFO',
    reducedMotion: false,
    theme: 'aurora',
  },
};

const listeners = new Set();

/** 读取深路径("task.progress");路径上任何一段缺失都返回 undefined */
export function get(path) {
  if (!path) return state;
  return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), state);
}

/**
 * 写入深路径并广播。
 * @param {string} path 形如 "task.progress"
 * @param {*} value
 * @param {{silent?: boolean}} [opts] silent=true 时只改值不广播(用于高频日志等)
 */
export function set(path, value, opts = {}) {
  const keys = path.split('.');
  let node = state;
  for (let i = 0; i < keys.length - 1; i += 1) {
    if (typeof node[keys[i]] !== 'object' || node[keys[i]] === null) node[keys[i]] = {};
    node = node[keys[i]];
  }
  const last = keys[keys.length - 1];
  const previous = node[last];
  node[last] = value;

  if (!opts.silent && previous !== value) notify(path, value, previous);
  return value;
}

/** 合并对象到深路径(浅合并) */
export function merge(path, patch, opts = {}) {
  const current = get(path);
  const next = { ...(current || {}), ...patch };
  return set(path, next, opts);
}

/**
 * 订阅变更。
 * @param {string|string[]} paths 关注的前缀;传 '*' 监听全部
 * @param {(path: string, value: *, previous: *) => void} handler
 * @returns {() => void} 取消订阅
 */
export function subscribe(paths, handler) {
  const prefixes = Array.isArray(paths) ? paths : [paths];
  const entry = { prefixes, handler };
  listeners.add(entry);
  return () => listeners.delete(entry);
}

function notify(path, value, previous) {
  for (const { prefixes, handler } of listeners) {
    if (prefixes.includes('*') || prefixes.some((p) => path === p || path.startsWith(p))) {
      try {
        handler(path, value, previous);
      } catch (err) {
        console.error('[store] 订阅回调异常:', err);
      }
    }
  }
}

/** 批量更新(只广播一次,避免连锁刷新) */
export function batch(updates) {
  for (const [path, value] of Object.entries(updates)) {
    const keys = path.split('.');
    let node = state;
    for (let i = 0; i < keys.length - 1; i += 1) {
      if (typeof node[keys[i]] !== 'object' || node[keys[i]] === null) node[keys[i]] = {};
      node = node[keys[i]];
    }
    node[keys[keys.length - 1]] = value;
  }
  notify('*', null, null);
}

/* ==========================================================================
   持久化: 只存界面偏好,不存数据
   ========================================================================== */
const STORAGE_KEY = 'smartcrawler.ui';

export function loadUiPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    const prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    set('ui', {
      ...state.ui,
      ...saved,
      // 页面不持久化,每次打开都从抓取页开始
      page: 'crawl',
      logOpen: false,
      reducedMotion: prefersReduced,
    });
  } catch {
    /* 存储不可用时用默认值即可 */
  }
}

export function persistUiPreferences() {
  const { sidebarCollapsed, logLevel, theme } = state.ui;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ sidebarCollapsed, logLevel, theme }));
  } catch {
    /* 忽略:隐私模式下 localStorage 可能抛错 */
  }
}

export { state };
export default { get, set, merge, subscribe, batch, state };
