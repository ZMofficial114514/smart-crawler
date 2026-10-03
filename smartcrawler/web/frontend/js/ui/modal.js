/**
 * ui/modal.js —— 通用弹层
 * ---------------------------------------------------------------------------
 * 用于展示 JSON 详情、结构报告、抓包记录等大块内容。支持:
 * - Esc 关闭 / 点击遮罩关闭;
 * - 打开时锁定背景滚动,关闭后恢复;
 * - 焦点管理: 打开聚焦弹层,关闭后焦点回到触发元素(a11y)。
 */

import { $, el, mount, highlightJson } from '../utils.js';

let lastFocused = null;

export function openModal({ title = '详情', body, footer = null, width = null }) {
  const modal = $('#modal');
  if (!modal) {
    // 早先这里是静默 return —— 一旦 #modal 不在 DOM 里(改版、脚本加载顺序问题),
    // 用户点按钮就"什么都没发生", 而且控制台也没有任何线索。宁可抛出来。
    const message = '弹层容器 #modal 不存在, 无法打开弹层';
    console.error(`[modal] ${message}`);
    throw new Error(message);
  }

  $('#modalTitle').textContent = title;
  const bodyNode = $('#modalBody');
  const footNode = $('#modalFoot');

  // body 可以是 Node / Node[] / 字符串
  if (body instanceof Node || Array.isArray(body)) {
    mount(bodyNode, body);
  } else {
    bodyNode.innerHTML = '';
    bodyNode.textContent = String(body ?? '');
  }

  mount(footNode, footer ? (Array.isArray(footer) ? footer : [footer]) : []);

  if (width) modal.querySelector('.modal__panel').style.width = width;

  lastFocused = document.activeElement;
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
  // 背景滚动锁定: 打开弹层时锁住页面滚动, 避免用户滚动时弹层"跑偏"的错觉。
  // (弹层本身是 position: fixed, 不受 #content 滚动影响, 所以只需锁 body。)
  document.body.style.overflow = 'hidden';
  // 弹层内部也复位到顶部: 上次打开时滚到一半的位置不该带到这次
  modal.scrollTop = 0;
  modal.querySelector('.modal__panel')?.scrollTo?.({ top: 0 });

  // 让弹层内的第一个可聚焦元素获得焦点
  requestAnimationFrame(() => {
    const focusable = modal.querySelector('button, [href], input, select, textarea');
    focusable?.focus();
  });
}

export function closeModal() {
  const modal = $('#modal');
  if (!modal || !modal.classList.contains('is-open')) return;
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
  document.body.style.overflow = '';
  modal.querySelector('.modal__panel').style.width = '';
  if (lastFocused instanceof HTMLElement) lastFocused.focus();
  lastFocused = null;
}

export function isModalOpen() {
  return $('#modal')?.classList.contains('is-open') || false;
}

/** 便捷构造: 一个可滚动的大块 JSON 视图 */
export function jsonBlock(value, maxHeight = '60vh') {
  const pre = el('pre.json');
  pre.style.maxHeight = maxHeight;
  pre.innerHTML = highlightJson(value);
  return pre;
}

/** 初始化全局关闭行为(只调用一次) */
export function initModal() {
  const modal = $('#modal');
  if (!modal) return;
  modal.addEventListener('click', (event) => {
    if (event.target.closest('[data-close]')) closeModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && modal.classList.contains('is-open')) {
      event.stopPropagation();
      closeModal();
    }
  });
}

export default { openModal, closeModal, initModal, jsonBlock };
