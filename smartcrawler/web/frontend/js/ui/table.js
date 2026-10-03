/**
 * ui/table.js —— 结果表格与记录列表
 * ---------------------------------------------------------------------------
 * 两个可复用的渲染器:
 *   renderTable()  —— 抓取结果表格(列由数据推断,超长值折叠,URL 变可点链接)
 *   renderRecords()—— 抓包记录列表(点击查看详情)
 * 都只依赖传入的数据,不读全局状态,方便在"任务历史详情"里复用。
 */

import { el, mount, truncate, prettyUrl, formatBytes } from '../utils.js';

const isUrlValue = (v) => typeof v === 'string' && /^https?:\/\/\S+$/i.test(v.trim());

function cell(value) {
  if (value === null || value === undefined || value === '') {
    return el('td.empty-cell', { text: '—' });
  }
  if (typeof value === 'boolean') {
    return el('td', {}, el('span.code-inline', { text: String(value) }));
  }
  if (typeof value === 'number') {
    return el('td.num', { text: String(value) });
  }
  if (typeof value === 'object') {
    const text = JSON.stringify(value);
    const td = el('td', { title: text });
    td.textContent = truncate(text, 80);
    return td;
  }
  const text = String(value);
  if (isUrlValue(text)) {
    const td = el('td', { title: text });
    const a = el('a', { href: text, target: '_blank', rel: 'noopener noreferrer' });
    a.textContent = prettyUrl(text, 28, 24);
    td.appendChild(a);
    return td;
  }
  const td = el('td', { title: text });
  td.textContent = truncate(text, 110);
  return td;
}

/**
 * 渲染结果表格。
 * @param {{ columns: string[], items: object[], limit?: number }} data
 */
export function renderTable({ columns, items, limit = 300 }) {
  const cols = columns?.length ? columns : inferColumns(items);
  if (!items?.length || !cols.length) {
    return el('div.empty-state', {}, [
      el('div.empty-state__icon', { html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/></svg>' }),
      el('h3', { text: '没有数据' }),
      el('p', { text: '任务未返回任何条目,可以查看实时日志定位原因。' }),
    ]);
  }

  const shown = items.slice(0, limit);
  const table = el('table.data');
  const thead = el('thead');
  const headRow = el('tr');
  headRow.appendChild(el('th', { text: '#' }));
  for (const col of cols) headRow.appendChild(el('th', { text: col }));
  thead.appendChild(headRow);

  const tbody = el('tbody');
  shown.forEach((row, index) => {
    const tr = el('tr');
    // 行入场错峰: 前 40 行逐条延迟,更多的就不再延迟(避免总时长过长)
    tr.style.animationDelay = `${Math.min(index, 40) * 12}ms`;
    tr.appendChild(el('td.num', { text: String(index + 1) }));
    for (const col of cols) tr.appendChild(cell(row[col]));
    tbody.appendChild(tr);
  });

  table.append(thead, tbody);
  const wrap = el('div.table-wrap', { style: { maxHeight: '520px' } }, table);

  const footer =
    items.length > limit
      ? el('p.hint', { text: `仅显示前 ${limit} 条,共 ${items.length} 条。点击卡片右上角"下载完整结果"获取全部数据。` })
      : null;

  return el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } }, [wrap, footer]);
}

export function inferColumns(items, limit = 22) {
  const cols = [];
  for (const row of (items || []).slice(0, 200)) {
    for (const key of Object.keys(row || {})) {
      if (!cols.includes(key)) cols.push(key);
      if (cols.length >= limit) return cols;
    }
  }
  return cols;
}

/**
 * 渲染抓包记录列表。
 * @param {object[]} records
 * @param {(record: object) => void} onSelect
 */
export function renderRecords(records, onSelect) {
  if (!records?.length) {
    return el('div.empty-state', {}, [
      el('div.empty-state__icon', { html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 14a8 8 0 0 1 16 0"/><path d="M2 14h20"/></svg>' }),
      el('h3', { text: '没有匹配的请求' }),
      el('p', { text: '尝试放宽 URL 正则或增大捕获等待时间 —— 有些接口要等页面滚动后才发出。' }),
    ]);
  }

  const list = el('div.record-list');
  records.forEach((rec, index) => {
    const item = el(
      'div.record',
      {
        style: { animationDelay: `${Math.min(index, 30) * 14}ms` },
        title: rec.url,
        onclick: () => onSelect?.(rec),
      },
      [
        el('span.record__method', { dataset: { m: rec.method || 'GET' }, text: rec.method || 'GET' }),
        el('span.record__status', {
          dataset: { bad: rec.failed || (rec.status && rec.status >= 400) ? '1' : '0' },
          text: rec.failed ? 'ERR' : String(rec.status ?? '—'),
        }),
        el('span.record__url', { text: prettyUrl(rec.url, 60, 46) }),
        el('span.record__tags', {}, [
          rec.body_json !== undefined && rec.body_json !== null
            ? el('span.record__badge.record__badge--json', { text: 'JSON' })
            : null,
          rec.resource_type === 'websocket'
            ? el('span.record__badge.record__badge--ws', { text: 'WS' })
            : null,
          rec.mime_type ? el('span.record__badge', { text: prettyMime(rec.mime_type) }) : null,
          rec.duration_ms ? el('span.record__badge', { text: `${Math.round(rec.duration_ms)}ms` }) : null,
        ]),
      ],
    );
    list.appendChild(item);
  });
  return list;
}

function prettyMime(mime) {
  return String(mime).split(';')[0].replace('application/', '').replace('text/', '');
}

/**
 * 单条抓包记录的详情(用于弹层)。
 * @param {object} rec
 * @param {(v:any)=>Node} jsonBlockRenderer
 */
export function recordDetail(rec, jsonBlockRenderer) {
  const rows = [
    ['URL', rec.url],
    ['方法', rec.method],
    ['状态', rec.failed ? `失败: ${rec.error || '未知'}` : String(rec.status ?? '—')],
    ['资源类型', rec.resource_type],
    ['MIME', rec.mime_type || '—'],
    ['耗时', rec.duration_ms ? `${Math.round(rec.duration_ms)} ms` : '—'],
    ['响应体大小', rec.body_text ? formatBytes(rec.body_text.length) : '—'],
    ['页面', rec.page_url || '—'],
  ];

  const children = [
    el('div.record-detail__url', { text: rec.url }),
    el(
      'dl.kv',
      {},
      rows.flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: String(v) })]),
    ),
  ];

  if (rec.post_data) {
    children.push(el('h4', { text: '请求体' }));
    children.push(jsonBlockRenderer(rec.post_data, '22vh'));
  }

  if (rec.body_json !== undefined && rec.body_json !== null) {
    children.push(el('h4', { text: '响应 JSON' }));
    children.push(jsonBlockRenderer(rec.body_json, '46vh'));
  } else if (rec.body_text) {
    children.push(el('h4', { text: '响应文本' }));
    children.push(jsonBlockRenderer(truncate(rec.body_text, 60000), '46vh'));
  }

  return el('div.record-detail', {}, children);
}

export default { renderTable, renderRecords, recordDetail, inferColumns };
