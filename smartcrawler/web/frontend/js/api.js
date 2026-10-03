/**
 * api.js —— 后端接口封装
 * ---------------------------------------------------------------------------
 * 统一处理三件事:
 * 1. 基础路径与 JSON 序列化;
 * 2. 错误归一化 —— 后端用 FastAPI 的 {detail: ...} 报错,detail 可能是字符串,
 *    也可能是 Pydantic 的校验数组,这里统一压成一句人类可读的中文;
 * 3. 把 HTTP 语义(<400 / 4xx / 5xx)翻译成调用方容易判断的 ApiError。
 */

const BASE = '/api';

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

/** 把 FastAPI 的错误体压成一行可读文本 */
function describeError(payload, status) {
  if (!payload) return `请求失败 (HTTP ${status})`;
  const detail = payload.detail ?? payload.message ?? payload;

  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    // Pydantic v2 校验错误: [{loc: [...], msg: '...'}]
    return detail
      .map((item) => {
        const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
        return loc ? `${loc}: ${item.msg}` : item.msg;
      })
      .join('; ');
  }

  if (typeof detail === 'object') {
    try {
      return JSON.stringify(detail);
    } catch {
      return `请求失败 (HTTP ${status})`;
    }
  }
  return String(detail);
}

async function request(path, { method = 'GET', body, query, signal } = {}) {
  const url = new URL(BASE + path, window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue;
      url.searchParams.set(key, value);
    }
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError('无法连接到后端服务,请确认服务仍在运行', 0, null);
  }

  const isJson = (response.headers.get('content-type') || '').includes('application/json');
  const payload = isJson ? await response.json().catch(() => null) : await response.text().catch(() => null);

  if (!response.ok) {
    throw new ApiError(describeError(payload, response.status), response.status, payload);
  }
  return payload;
}

export const api = {
  /* ---------------- 系统 ---------------- */
  health: () => request('/health'),
  usage: () => request('/usage'),
  readEnv: () => request('/env'),
  testAi: () => request('/ai/test', { method: 'POST' }),
  testProxies: () => request('/proxies/test', { method: 'POST' }),
  clearCache: () => request('/cache/clear', { method: 'POST' }),
  resetState: () => request('/state/reset', { method: 'POST' }),

  /* ---------------- 配置 ---------------- */
  getConfig: () => request('/config'),
  patchConfig: (patch, persist = true) => request('/config', { method: 'POST', body: { patch, persist } }),
  reloadConfig: () => request('/config/reload', { method: 'POST' }),

  /* ---------------- 任务 ---------------- */
  startCrawl: (payload) => request('/crawl', { method: 'POST', body: payload }),
  startAnalyze: (payload) => request('/analyze', { method: 'POST', body: payload }),
  startRequests: (payload) => request('/requests', { method: 'POST', body: payload }),
  listTasks: (limit = 30) => request('/tasks', { query: { limit } }),
  getTask: (id) => request(`/tasks/${id}`),
  cancelTask: (id) => request(`/tasks/${id}/cancel`, { method: 'POST' }),
  /** 回答"是否继续向下滚动"(无限流页面的阻塞式询问) */
  answerScroll: (id, cont, rounds = 0) =>
    request(`/tasks/${id}/scroll`, {
      method: 'POST',
      body: { continue: Boolean(cont), rounds: Number(rounds) || 0 },
    }),
  taskItems: (id, offset = 0, limit = 100) => request(`/tasks/${id}/items`, { query: { offset, limit } }),
  /** 按目标 URL 归类任务与产出文件 */
  tasksByUrl: (limit = 200) => request('/tasks/by-url', { query: { limit } }),
  /** 删除单个任务(默认连带清理产出文件) */
  deleteTask: (id, removeFiles = true) =>
    request(`/tasks/${id}`, { method: 'DELETE', query: { remove_files: removeFiles } }),
  /** 按 URL 删除全部记录与产出文件 */
  deleteUrlGroup: (url, removeFiles = true) =>
    request('/tasks/by-url', { method: 'DELETE', query: { url, remove_files: removeFiles } }),

  /* ---------------- 插件 ---------------- */
  listPlugins: () => request('/plugins'),
  refreshPlugins: () => request('/plugins/refresh', { method: 'POST' }),
  togglePlugin: (id, enabled) => request(`/plugins/${encodeURIComponent(id)}/toggle`, { method: 'POST', body: { enabled } }),
  updatePluginConfig: (id, config) => request(`/plugins/${encodeURIComponent(id)}/config`, { method: 'POST', body: { config } }),
  resetPluginConfig: (id) => request(`/plugins/${encodeURIComponent(id)}/reset`, { method: 'POST' }),
  pluginTemplate: () => request('/plugins/template'),
  pluginSource: (path) => request('/plugins/source', { query: { path } }),
  createPlugin: (payload) => request('/plugins/create', { method: 'POST', body: payload }),
  deletePlugin: (path) => request('/plugins', { method: 'DELETE', query: { path } }),

  /* ---------------- 服务 ---------------- */
  /** 关闭服务(会连同 Playwright 的浏览器子进程一起清理) */
  shutdown: () => request('/shutdown', { method: 'POST' }),

  /* ---------------- 登录会话 ---------------- */
  /** 已保存会话的摘要 + 当前登录流程状态(响应里只有掩码, 不含凭据值) */
  getSession: () => request('/session'),
  /** 打开可见浏览器手动登录 / 手动过人机验证(mode: 'login' | 'challenge') */
  startLogin: (url, mode = 'login') => request('/session/login', { method: 'POST', body: { url, mode } }),
  /** 轮询登录流程进度 */
  loginStatus: () => request('/session/login/status'),
  /** 确认已登录, 请求保存会话 */
  confirmLogin: () => request('/session/login/confirm', { method: 'POST' }),
  /** 取消登录流程 */
  cancelLogin: () => request('/session/login/cancel', { method: 'POST' }),
  /** 清理登录流程状态 */
  resetLogin: () => request('/session/login/reset', { method: 'POST' }),
  /** 用已保存的会话访问一次, 核实登录是否生效 */
  verifySession: (url) => request('/session/verify', { method: 'POST', body: { url } }),
  /** 删除已保存的会话(退出登录) */
  deleteSession: () => request('/session', { method: 'DELETE' }),

  /* ---------------- 规则 ---------------- */
  validateRule: (rule) => request('/rules/validate', { method: 'POST', body: { rule } }),
  repairRule: (payload) => request('/rules/repair', { method: 'POST', body: payload }),
  cleanData: (items, schema) => request('/ai/clean', { method: 'POST', body: { items, schema } }),

  /* ---------------- 文件 ---------------- */
  listFiles: (limit = 100) => request('/files', { query: { limit } }),
  readFileText: (path, maxChars = 60000) => request('/files/text', { query: { path, max_chars: maxChars } }),
  deleteFile: (path) => request('/files', { method: 'DELETE', query: { path } }),

  /* ---------------- URL 构造(直接用 <a href> 或 <img src> 时用) ---------------- */
  downloadUrl: (path) => `${BASE}/files/download?path=${encodeURIComponent(path)}`,
  taskDownloadUrl: (id, which = 'artifact') => `${BASE}/tasks/${id}/download?which=${which}`,

  /** 用 fetch 拉文件并触发浏览器下载(带上正确的文件名) */
  async downloadFile(path, fallbackName = 'download') {
    const url = `${BASE}/files/download?path=${encodeURIComponent(path)}`;
    const response = await fetch(url);
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(describeError(payload, response.status), response.status, payload);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const name = match ? decodeURIComponent(match[1]) : fallbackName;
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
  },
};

export default api;
