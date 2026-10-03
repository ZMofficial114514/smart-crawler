/**
 * pages/requests.js —— 网络抓包
 * ---------------------------------------------------------------------------
 * 目标是"找到可用的数据接口",因此在结果之上提供两条捷径:
 *   1. 一键把某条 JSON 响应变成 mode:"json" 的提取规则,并跳转到抓取页;
 *   2. 直接把响应的 JSON 结构展示出来(带路径提示),方便手写 JSONPath。
 */

import { $, el, mount, withLoading, truncate, copyText, flashScroll, formatNumber } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, jsonBlock, closeModal } from '../ui/modal.js';
import { renderRecords, recordDetail } from '../ui/table.js';
import { connectTask, onTaskEvent } from '../ws.js';
import { goToPage } from '../router.js';
// 任务事件归属判断(三个页面共用同一条总线, 必须区分是谁的任务)
import { isOwnTaskEvent } from '../ui/task-owner.js';

let activeTaskId = null;
let currentRecords = [];

/* ==========================================================================
   渲染
   ========================================================================== */
function summarizeJson(value, maxKeys = 8) {
  if (Array.isArray(value)) {
    const first = value[0];
    if (first && typeof first === 'object') {
      return `数组(${value.length}) · 元素字段: ${Object.keys(first).slice(0, maxKeys).join(', ')}`;
    }
    return `数组(${value.length})`;
  }
  if (value && typeof value === 'object') {
    return `对象 · 字段: ${Object.keys(value).slice(0, maxKeys).join(', ')}`;
  }
  return typeof value;
}

/** 猜测条目数组的 JSONPath(给规则预填一个合理的起点) */
function guessItemPath(body) {
  if (Array.isArray(body)) return '$[*]';
  if (!body || typeof body !== 'object') return '$';

  const preferred = ['data', 'list', 'items', 'results', 'records', 'rows', 'content'];
  for (const key of preferred) {
    const value = body[key];
    if (Array.isArray(value)) return `$.${key}[*]`;
    if (value && typeof value === 'object') {
      for (const inner of preferred) {
        if (Array.isArray(value[inner])) return `$.${key}.${inner}[*]`;
      }
    }
  }
  // 兜底: 找第一个数组字段(只下探一层)
  for (const [key, value] of Object.entries(body)) {
    if (Array.isArray(value)) return `$.${key}[*]`;
    if (value && typeof value === 'object') {
      for (const [innerKey, innerValue] of Object.entries(value)) {
        if (Array.isArray(innerValue)) return `$.${key}.${innerKey}[*]`;
      }
    }
  }
  return '$';
}

function guessFields(record) {
  const path = guessItemPath(record.body_json);
  // 从数组里取一个样本元素
  const sample = firstArrayItem(record.body_json);
  if (!sample || typeof sample !== 'object') return [];
  return Object.entries(sample)
    .slice(0, 12)
    .map(([key, value]) => ({
      name: key,
      selector: `$.${key}`,
      attribute: null,
      transform: typeof value === 'number' ? ['float'] : ['strip'],
    }));
}

function firstArrayItem(body) {
  if (Array.isArray(body)) return body.find((x) => x && typeof x === 'object') || null;
  if (!body || typeof body !== 'object') return null;
  for (const value of Object.values(body)) {
    if (Array.isArray(value)) {
      const found = value.find((x) => x && typeof x === 'object');
      if (found) return found;
    } else if (value && typeof value === 'object') {
      for (const inner of Object.values(value)) {
        if (Array.isArray(inner)) {
          const found = inner.find((x) => x && typeof x === 'object');
          if (found) return found;
        }
      }
    }
  }
  return null;
}

/** 把某条 JSON 响应转成提取规则并交给抓取页 */
function useAsRule(record) {
  if (!record.body_json) {
    toastInfo('该记录没有可解析的 JSON 响应');
    return;
  }
  const rule = {
    mode: 'json',
    list_rule: {
      item_selector: guessItemPath(record.body_json),
      fields: guessFields(record),
    },
    notes: `由网络抓包生成,来源接口: ${record.url}`,
  };

  const box = $('#ruleJson');
  if (box) box.value = JSON.stringify(rule, null, 2);
  const urlInput = $('#crawlUrl');
  if (urlInput && !urlInput.value.trim()) urlInput.value = record.page_url || '';

  closeModal();
  toastSuccess('已生成 JSON 模式规则并填入抓取页');
  goToPage('crawl');
  setTimeout(() => {
    document.querySelector('#ruleMode .segmented__item[data-mode="manual"]')?.click();
    flashScroll($('#ruleJson'));
  }, 260);
}

function showRecord(record) {
  const body = recordDetail(record, jsonBlock);
  const footer = [];

  if (record.body_json) {
    footer.push(
      el('button.btn.btn--sm.btn--primary', {
        text: '用这条响应的 JSON 生成提取规则',
        onclick: () => useAsRule(record),
      }),
      el('button.btn.btn--sm.btn--ghost', {
        text: '复制 URL',
        onclick: async () => {
          await copyText(record.url);
          toastSuccess('已复制接口 URL');
        },
      }),
    );
  }

  openModal({ title: `${record.method || 'GET'} 请求详情`, body, footer });
}

function renderOutput(payload, params) {
  const out = $('#reqOutput');
  if (!out) return;

  const children = [];

  children.push(
    el('div.card.glass', {}, [
      el('div.card__head', {}, [
        el('h2', { text: '抓包结果' }),
        el('div.card__tools', {}, [
          el('span.tag', { text: `匹配 ${payload.matched} / 共 ${payload.total}` }),
          payload.json_records ? el('span.tag.tag--success', { text: `JSON ${payload.json_records}` }) : null,
          payload.ws_records ? el('span.tag.tag--warning', { text: `WS ${payload.ws_records}` }) : null,
        ]),
      ]),
      el('div.req-toolbar', {}, [
        el('span.hint', {
          text: params?.pattern ? `URL 正则: ${params.pattern}` : '提示: 点击任意记录查看响应体详情',
        }),
        el('div', { style: { marginLeft: 'auto', display: 'flex', gap: '8px' } }, [
          el('button.btn.btn--sm.btn--ghost', {
            text: '查看完整记录 (JSON)',
            onclick: () => openModal({ title: '抓包记录', body: jsonBlock(payload.records, '70vh') }),
          }),
        ]),
      ]),
      renderRecords(payload.records || [], showRecord),
    ]),
  );

  mount(out, children);
  currentRecords = payload.records || [];
  flashScroll(out.firstElementChild);
}

/* ==========================================================================
   任务
   ========================================================================== */
function collectParams() {
  return {
    url: $('#reqUrl').value.trim(),
    wait: Number($('#reqWait').value || 5),
    scroll: $('#reqScroll').checked,
    pattern: $('#reqPattern').value.trim() || null,
    mime: $('#reqMime').value.trim() || null,
    status: $('#reqStatus').value ? Number($('#reqStatus').value) : null,
    resource: $('#reqResource').value || null,
    has_json: $('#reqHasJson').checked,
    limit: Number($('#reqLimit').value || 200),
  };
}

async function startCapture() {
  const params = collectParams();
  if (!/^https?:\/\/.+/i.test(params.url)) {
    toastError('请填写以 http:// 或 https:// 开头的目标地址');
    return;
  }

  const out = $('#reqOutput');
  mount(
    out,
    el('div.card.glass', {}, [
      el('div.card__head', {}, [el('h2', { text: '捕获中…' }), el('span.tag[data-state=running]', { text: '进行中' })]),
      el('p.hint', { text: `正在打开页面并等待 ${params.wait} 秒,以便捕获异步接口请求…` }),
      el('div.record-list', {}, [1, 2, 3, 4].map(() => el('div.skeleton', { style: { height: '38px' } }))),
    ]),
  );

  const restore = withLoading($('#btnReqStart'), '捕获中…');
  try {
    const { task } = await api.startRequests(params);
    activeTaskId = task.id;
    set('lastRequestsTaskId', task.id);
    connectTask(task.id);
  } catch (err) {
    restore();
    mount(
      out,
      el('div.alert.alert--error', {}, [el('div.alert__body', { text: err instanceof ApiError ? err.message : String(err) })]),
    );
    toastError(err instanceof ApiError ? err.message : String(err), '抓包失败');
    return;
  }
  setTimeout(restore, 400);
}

function handleTaskEvent(event) {
  // 同 analyze.js: 以任务 id 判断归属(store.task 的 kind 可能来自别的页面)
  if (!isOwnTaskEvent(event, { activeTaskId, knownTask: get('task'), kind: 'requests' })) return;
  if (event.type === 'result') {
    renderOutput(event.payload, collectParams());
    set('lastRequests', event.payload);
    // 兜底: 结果已到, 任务即已结束, 避免界面停留在"进行中"
    if (get('task.status') === 'running') set('task.status', 'success');
    toastSuccess(`捕获 ${event.payload.total} 条请求,匹配 ${event.payload.matched} 条`);
  } else if (event.type === 'status' && event.status === 'failed') {
    mount(
      $('#reqOutput'),
      el('div.alert.alert--error', {}, [el('div.alert__body', { text: event.message || '抓包失败,详见实时日志' })]),
    );
    toastError(event.message || '抓包失败');
  } else if (event.type === 'snapshot' && event.task?.status === 'success' && event.task.result) {
    renderOutput(event.task.result, collectParams());
  }
}

export function init() {
  $('#btnReqStart')?.addEventListener('click', startCapture);
  // 支持在任意输入框里回车提交
  for (const id of ['#reqUrl', '#reqPattern', '#reqMime', '#reqWait']) {
    $(id)?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        startCapture();
      }
    });
  }
  onTaskEvent(handleTaskEvent);

  // 从抓取页跳转过来时,自动带上同一个 URL
  const crawlUrl = $('#crawlUrl')?.value?.trim();
  const reqUrl = $('#reqUrl');
  if (crawlUrl && reqUrl && !reqUrl.value.trim()) reqUrl.value = crawlUrl;
}

export default { init };
