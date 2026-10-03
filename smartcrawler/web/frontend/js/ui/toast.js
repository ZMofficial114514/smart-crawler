/**
 * ui/toast.js —— 轻量通知
 * ---------------------------------------------------------------------------
 * 右上角滑入的玻璃质感提示。同一时间最多保留 4 条,超出时先淘汰最旧的,
 * 避免批量错误把屏幕糊满。
 */

import { $, el } from '../utils.js';

const MAX_TOASTS = 4;
const ICONS = {
  success: '<path d="M20 6 9 17l-5-5"/>',
  error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/>',
  warn: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5M12 8h.01"/>',
};

function icon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.9');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', 'i toast__icon');
  svg.innerHTML = ICONS[kind] || ICONS.info;
  return svg;
}

/**
 * @param {string} message 正文
 * @param {{ type?: 'success'|'error'|'warn'|'info', title?: string, duration?: number }} [opts]
 */
export function toast(message, opts = {}) {
  const container = $('#toasts');
  if (!container) return () => {};

  const type = opts.type || 'info';
  const duration = opts.duration ?? (type === 'error' ? 7000 : 3800);

  const node = el(`div.toast.toast--${type}`, { role: 'status' }, [
    icon(type),
    el('div.toast__content', {}, [
      opts.title ? el('div.toast__title', { text: opts.title }) : null,
      el('div.toast__msg', { text: message }),
    ]),
  ]);

  container.appendChild(node);

  // 超出上限: 立刻移除最旧的
  while (container.children.length > MAX_TOASTS) {
    container.firstElementChild?.remove();
  }

  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    node.classList.add('is-leaving');
    node.addEventListener('animationend', () => node.remove(), { once: true });
    // 兜底:动画被禁用(prefers-reduced-motion)时也要清理
    setTimeout(() => node.remove(), 400);
  };

  const timer = duration > 0 ? setTimeout(remove, duration) : null;
  node.addEventListener('click', () => {
    if (timer) clearTimeout(timer);
    remove();
  });

  return remove;
}

export const toastSuccess = (msg, title) => toast(msg, { type: 'success', title });
export const toastError = (msg, title) => toast(msg, { type: 'error', title });
export const toastWarn = (msg, title) => toast(msg, { type: 'warn', title });
export const toastInfo = (msg, title) => toast(msg, { type: 'info', title });

export default toast;
