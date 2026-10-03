/**
 * ui/logpanel.js —— 实时日志抽屉
 * ---------------------------------------------------------------------------
 * 日志是全站最高频的更新源(爬虫在浏览器里每翻一页都会打点),所以这里刻意
 * 避开虚拟 DOM 与整体重渲染:
 *   - 单行 DOM 直接 append,配合 CSS 动画入场;
 *   - 只保留最近 MAX_LINES 行,超出时批量裁剪(每 50 行裁一次,避免频繁重排);
 *   - 级别过滤在前端做,切换过滤时重建一次列表即可。
 */

import { $, el } from '../utils.js';
import { onLog, connectLogs } from '../ws.js';
import { get, set } from '../store.js';

const MAX_LINES = 1500;
const TRIM_CHUNK = 60;
const LEVEL_ORDER = { DEBUG: 10, INFO: 20, SUCCESS: 25, WARNING: 30, ERROR: 40, CRITICAL: 50 };

const buffer = [];
let pendingTrim = 0;
let unread = 0;

function levelPasses(level) {
  const min = LEVEL_ORDER[get('ui.logLevel') || 'INFO'] ?? 20;
  return (LEVEL_ORDER[level] ?? 20) >= min;
}

function renderLine(entry) {
  return el('div.log-line', { dataset: { level: entry.level, seq: entry.seq } }, [
    el('span.log-line__time', { text: entry.time }),
    el('span.log-line__level', { text: entry.level }),
    el('span.log-line__module', { text: entry.module || '' }),
    el('span.log-line__msg', { text: entry.message }),
  ]);
}

function atBottom(node) {
  return node.scrollHeight - node.scrollTop - node.clientHeight < 48;
}

function trim(body) {
  while (body.children.length > MAX_LINES) {
    body.removeChild(body.firstElementChild);
  }
}

/** 追加一条日志;抽屉关闭时累加未读计数 */
function append(entry) {
  buffer.push(entry);
  if (buffer.length > MAX_LINES * 2) buffer.splice(0, buffer.length - MAX_LINES);

  const body = $('#logBody');
  if (!body) return;

  const drawerOpen = $('#logDrawer')?.classList.contains('is-open');
  if (!drawerOpen) {
    unread += 1;
    updateBadge();
    return;
  }
  if (levelPasses(entry.level)) {
    const stick = $('#logAutoScroll').checked && atBottom(body);
    body.appendChild(renderLine(entry));

    pendingTrim += 1;
    if (pendingTrim >= TRIM_CHUNK) {
      pendingTrim = 0;
      trim(body);
    }
    if (stick) body.scrollTop = body.scrollHeight;
  }
}

function updateBadge() {
  const badge = $('#logBadge');
  if (!badge) return;
  badge.hidden = unread === 0;
  badge.textContent = unread > 99 ? '99+' : String(unread);
}

/** 按当前级别重建列表 */
function rebuild() {
  const body = $('#logBody');
  if (!body) return;
  body.replaceChildren();
  const rows = buffer.filter((e) => levelPasses(e.level)).slice(-MAX_LINES);
  const frag = document.createDocumentFragment();
  for (const entry of rows) frag.appendChild(renderLine(entry));
  body.appendChild(frag);
  body.scrollTop = body.scrollHeight;
}

export function openLogDrawer() {
  const drawer = $('#logDrawer');
  if (!drawer) return;
  drawer.classList.add('is-open');
  drawer.setAttribute('aria-hidden', 'false');
  unread = 0;
  updateBadge();
  set('ui.logOpen', true);
  rebuild();
}

export function closeLogDrawer() {
  const drawer = $('#logDrawer');
  if (!drawer) return;
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden', 'true');
  set('ui.logOpen', false);
}

export function toggleLogDrawer() {
  const drawer = $('#logDrawer');
  if (!drawer) return;
  if (drawer.classList.contains('is-open')) closeLogDrawer();
  else openLogDrawer();
}

export function initLogPanel() {
  const levelSelect = $('#logLevel');
  const autoScroll = $('#logAutoScroll');

  // 恢复上次选择的级别
  const saved = get('ui.logLevel') || 'INFO';
  if (levelSelect) {
    levelSelect.value = saved;
    levelSelect.addEventListener('change', () => {
      set('ui.logLevel', levelSelect.value);
      rebuild();
    });
  }
  autoScroll?.addEventListener('change', () => {
    if (autoScroll.checked) {
      const body = $('#logBody');
      body.scrollTop = body.scrollHeight;
    }
  });

  $('#logClear')?.addEventListener('click', () => {
    buffer.length = 0;
    pendingTrim = 0;
    $('#logBody').replaceChildren();
    unread = 0;
    updateBadge();
  });

  $('#logClose')?.addEventListener('click', closeLogDrawer);
  $('#chipLogs')?.addEventListener('click', toggleLogDrawer);

  // 抽屉内的空状态提示
  const body = $('#logBody');
  if (body && body.children.length === 0) {
    body.appendChild(
      el('div.log-line', { dataset: { level: 'INFO' } }, [
        el('span.log-line__time', { text: '--:--:--' }),
        el('span.log-line__level', { text: 'INFO' }),
        el('span.log-line__module', { text: 'ui' }),
        el('span.log-line__msg', { text: '日志通道已连接,等待任务输出…' }),
      ]),
    );
  }

  connectLogs();
  onLog(append);
}

export function logCount() {
  return buffer.length;
}

export default { initLogPanel, openLogDrawer, closeLogDrawer, toggleLogDrawer };
