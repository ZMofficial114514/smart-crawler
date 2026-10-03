/**
 * pages/results.js —— 结果与历史
 * ---------------------------------------------------------------------------
 * 左侧按**目标 URL** 归类: 一个站点/一个页面一行, 展开后是它的历次任务与产出文件。
 *
 * 为什么按 URL 归类而不是混在一张表里: 抓过多个站点后, "文件列表"会变成一堆互相
 * 对不上号的文件名 —— 看到 `result_20250101_120000.csv` 根本想不起来是哪个站的。
 * 归类后每个 URL 自带它的产出, 删除时也能"连同这个 URL 的所有缓存一起清掉"。
 */

import { $, el, mount, formatBytes, formatDuration, truncate, formatNumber, downloadText, toCsv } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, jsonBlock } from '../ui/modal.js';
import { renderTable } from '../ui/table.js';
import { goToPage } from '../router.js';

const KIND_LABEL = { crawl: '抓取', analyze: '分析', requests: '抓包' };
const STATUS_LABEL = {
  queued: ['排队', 'muted'],
  running: ['进行中', 'info'],
  success: ['成功', 'success'],
  failed: ['失败', 'danger'],
  cancelled: ['已取消', 'warning'],
};

let selectedTaskId = null;
/** 当前展开的 URL 分组(默认展开第一个) */
const expandedUrls = new Set();

/* ==========================================================================
   任务列表(按 URL 分组)
   ========================================================================== */
function taskNode(task) {
  const [label, variant] = STATUS_LABEL[task.status] || ['未知', 'muted'];
  const title = task.goal || KIND_LABEL[task.kind] || task.kind;
  const node = el(
    'div.task-item',
    {
      dataset: { id: task.id },
      onclick: () => selectTask(task.id),
    },
    [
      el('span.tag', { text: KIND_LABEL[task.kind] || task.kind }),
      el('div', { style: { flex: '1', minWidth: '0' } }, [
        el('div.task-item__title', { title, text: truncate(title, 58) }),
        el('div.task-item__meta', {
          text: [
            new Date(task.created_at).toLocaleTimeString('zh-CN'),
            task.duration_ms ? formatDuration(task.duration_ms) : '',
            task.item_count !== undefined && task.item_count !== null ? `${task.item_count} 条` : '',
            task.file_count ? `${task.file_count} 文件` : '',
          ]
            .filter(Boolean)
            .join(' · '),
        }),
      ]),
      el(`span.tag.tag--${variant}`, { text: label }),
      // 单条任务的删除(会连带清理它自己的产出文件)
      el('button.icon-btn.icon-btn--danger', {
        title: '删除这次记录(连同它的产出文件)',
        html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>',
        onclick: (event) => {
          event.stopPropagation();
          removeTask(task);
        },
      }),
    ],
  );
  if (task.id === selectedTaskId) node.classList.add('is-selected');
  return node;
}

/** 一个 URL 分组: 头部是 URL + 汇总, 展开后是它的任务与产出文件。 */
function urlGroupNode(group) {
  const url = group.url || '(未指定 URL)';
  const expanded = expandedUrls.has(group.url);
  const body = el('div.url-group__body', { hidden: !expanded }, [
    el(
      'div.url-group__section',
      {},
      [
        el('div.url-group__subtitle', { text: `任务记录(${group.task_count})` }),
        el('div.task-list', {}, group.tasks.map(taskNode)),
      ],
    ),
    el(
      'div.url-group__section',
      {},
      [
        el('div.url-group__subtitle', {
          text: `产出文件(${group.file_count})${group.bytes ? ' · ' + formatBytes(group.bytes) : ''}`,
        }),
        group.file_count
          ? el(
              'div.file-list',
              {},
              group.files.map((file) =>
                el('div.file-item', {}, [
                  el('span.tag.tag--ghost', { text: file.kind || 'file' }),
                  el('div', { style: { flex: '1', minWidth: '0' } }, [
                    el('div.file-item__name', { title: file.path, text: file.name }),
                    el('div.file-item__meta', {
                      text: `${file.exists ? formatBytes(file.bytes) : '文件已缺失'}${file.modified ? ' · ' + file.modified.replace('T', ' ') : ''}`,
                    }),
                  ]),
                  el('button.btn.btn--sm.btn--ghost', {
                    text: '预览',
                    onclick: () => previewFile(file),
                  }),
                  el('button.btn.btn--sm.btn--ghost', {
                    text: '下载',
                    disabled: !file.exists,
                    onclick: () => downloadFile(file),
                  }),
                  el('button.icon-btn.icon-btn--danger', {
                    title: '删除该文件',
                    html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>',
                    onclick: () => removeFile(file),
                  }),
                ]),
              ),
            )
          : el('p.empty', { text: '这个 URL 还没有产出文件' }),
      ],
    ),
  ]);

  const header = el(
    'div.url-group__head',
    {
      onclick: (event) => {
        // 点删除按钮时不要顺带折叠
        if (event.target.closest('button')) return;
        if (expandedUrls.has(group.url)) expandedUrls.delete(group.url);
        else expandedUrls.add(group.url);
        refreshTasks();
      },
    },
    [
      el('span.url-group__caret', { dataset: { open: String(expanded) }, text: '▸' }),
      // 主体: URL 一行(强制省略号), 汇总信息一行
      el('div.url-group__main', {}, [
        el('div.url-group__url', { title: url, text: url }),
        el('div.url-group__meta', {
          text: [
            group.host,
            `${group.task_count} 次`,
            group.item_count ? `${formatNumber(group.item_count)} 条` : '',
            group.file_count ? `${group.file_count} 个文件` : '',
            group.bytes ? formatBytes(group.bytes) : '',
          ]
            .filter(Boolean)
            .join(' · '),
        }),
      ]),
      // 操作区不允许被挤压、也不允许换行 ——
      // 之前长 URL 会把按钮文字顶出去, 出现"分析"和"删除此URL"叠在一起的样子
      el(
        'div.url-group__actions',
        {},
        [
          ...group.kinds.map((k) => el('span.tag.tag--ghost', { text: KIND_LABEL[k] || k })),
          el('button.btn.btn--sm.btn--danger-ghost', {
            text: '删除此 URL',
            title: '删除这个 URL 的全部记录, 并清理它对应的产出文件缓存',
            onclick: (event) => {
              event.stopPropagation();
              removeUrlGroup(group);
            },
          }),
        ],
      ),
    ],
  );

  return el('div.url-group', { dataset: { url: group.url } }, [header, body]);
}

export async function refreshTasks() {
  try {
    const { groups } = await api.tasksByUrl(200);
    set('urlGroups', groups);
    const list = $('#taskList');
    if (!list) return;

    if (!groups.length) {
      mount(list, el('p.empty', { text: '暂无任务记录' }));
      const badge0 = $('#resultBadge');
      if (badge0) badge0.hidden = true;
      return;
    }

    // 首次加载时默认展开第一个分组, 免得看到一片折叠标题
    if (!expandedUrls.size && groups[0]) expandedUrls.add(groups[0].url);

    mount(list, groups.map(urlGroupNode));

    const badge = $('#resultBadge');
    if (badge) {
      const total = groups.reduce((sum, g) => sum + g.task_count, 0);
      badge.hidden = total === 0;
      badge.textContent = String(total);
    }
  } catch (err) {
    console.error('[results] 任务列表加载失败', err);
  }
}

/* ==========================================================================
   删除: 单条任务 / 整个 URL
   ========================================================================== */
async function removeTask(task) {
  const name = task.goal ? `「${truncate(task.goal, 30)}」` : `任务 ${task.id}`;
  const ok = window.confirm(
    `删除 ${name}?\n\n它产出的文件(结果 JSON、导出文件、下载的图片/音频)会一并清理, 不可撤销。`,
  );
  if (!ok) return;
  try {
    const res = await api.deleteTask(task.id, true);
    toastSuccess(res.message || '已删除');
    if (selectedTaskId === task.id) {
      selectedTaskId = null;
      mount($('#historyDetail'), el('p.empty', { text: '请选择左侧的任务查看详情' }));
    }
    refreshTasks();
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

async function removeUrlGroup(group) {
  const url = group.url || '(未指定 URL)';
  const ok = window.confirm(
    `删除这个 URL 的全部记录?\n\n${url}\n\n` +
      `将删除 ${group.task_count} 条任务记录, 并清理 ${group.file_count} 个产出文件` +
      `${group.bytes ? `(约 ${formatBytes(group.bytes)})` : ''}。\n此操作不可撤销。`,
  );
  if (!ok) return;
  try {
    const res = await api.deleteUrlGroup(group.url, true);
    toastSuccess(res.message || '已删除');
    expandedUrls.delete(group.url);
    if (group.tasks.some((t) => t.id === selectedTaskId)) {
      selectedTaskId = null;
      mount($('#historyDetail'), el('p.empty', { text: '请选择左侧的任务查看详情' }));
    }
    refreshTasks();
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

/* ==========================================================================
   任务详情
   ========================================================================== */
async function selectTask(taskId) {
  selectedTaskId = taskId;
  for (const node of document.querySelectorAll('#taskList .task-item')) {
    node.classList.toggle('is-selected', node.dataset.id === taskId);
  }

  const detail = $('#historyDetail');
  mount(detail, el('div.card.glass', {}, [el('div.skeleton', { style: { height: '120px' } })]));

  let task;
  try {
    task = await api.getTask(taskId);
  } catch (err) {
    mount(detail, el('div.alert.alert--error', {}, [el('div.alert__body', { text: String(err.message || err) })]));
    return;
  }

  const result = task.result || {};
  syncFilesForTask(task);
  const children = [];

  children.push(
    el('div.card.glass', {}, [
      el('div.card__head', {}, [
        el('h2', { text: `任务 ${task.id} · ${KIND_LABEL[task.kind] || task.kind}` }),
        el('div.card__tools', {}, [
          el('span.tag', { text: (STATUS_LABEL[task.status] || ['未知'])[0] }),
          task.artifacts?.length
            ? el('button.btn.btn--sm.btn--ghost', {
                text: '下载产物',
                onclick: () => {
                  window.location.href = api.taskDownloadUrl(task.id, 'artifact');
                },
              })
            : null,
        ]),
      ]),
      el('dl.kv', {}, [
        el('dt', { text: '目标' }),
        el('dd', { text: task.params?.url || '—' }),
        el('dt', { text: '目标描述' }),
        el('dd', { text: task.params?.goal || '—' }),
        el('dt', { text: '开始 / 结束' }),
        el('dd', { text: `${task.started_at || task.created_at} → ${task.finished_at || '进行中'}` }),
        el('dt', { text: '耗时' }),
        el('dd', { text: formatDuration(task.duration_ms) }),
        task.result?.saved_to ? el('dt', { text: '已保存到' }) : null,
        task.result?.saved_to ? el('dd', { text: task.result.saved_to }) : null,
      ]),
      task.errors?.length
        ? el('div.alert.alert--warn', {}, [
            el('div.alert__body', {}, [
              el('div.alert__title', { text: '执行提示' }),
              el('div', { text: task.errors.join(' / ') }),
            ]),
          ])
        : null,
    ]),
  );

  // 抓取任务: 渲染条目表
  if (task.kind === 'crawl' && result.item_count > 0) {
    const items = result.items_preview || [];
    const total = result.item_count;

    const tableCard = el('div.card.glass', {}, [
      el('div.card__head', {}, [
        el('h2', { text: `数据(${formatNumber(total)} 条)` }),
        el('div.card__tools', {}, [
          items.length
            ? el('button.btn.btn--sm.btn--ghost', {
                text: '导出 CSV',
                onclick: () => {
                  const cols = result.columns?.length ? result.columns : Object.keys(items[0] || {});
                  downloadText(`task_${task.id}.csv`, toCsv(cols, items), 'text/csv;charset=utf-8');
                  toastSuccess('已导出预览数据');
                },
              })
            : null,
          el('button.btn.btn--sm.btn--ghost', {
            text: '查看规则',
            onclick: () => openModal({ title: '提取规则', body: jsonBlock(result.rule || {}, '60vh') }),
          }),
          total > items.length
            ? el('button.btn.btn--sm.btn--primary', {
                text: `在抓取页继续查看`,
                onclick: () => {
                  set('lastCrawl', result);
                  set('task', task);
                  goToPage('crawl');
                },
              })
            : null,
        ]),
      ]),
      renderTable({ columns: result.columns, items, limit: 300 }),
      total > items.length
        ? el('p.hint', {
            text: `该任务共 ${total} 条,此处仅展示前 ${items.length} 条预览。完整数据请下载产物文件。`,
          })
        : null,
    ]);
    children.push(tableCard);
  }

  // 分析任务: 概览 + 完整报告
  if (task.kind === 'analyze' && result.report) {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, [
          el('h2', { text: '结构报告' }),
          el('span.tag.tag--ghost', { text: `${result.report.candidate_lists?.length || 0} 个候选列表` }),
        ]),
        el('div.stats-row', {}, [
          el('div.stat', {}, [el('span.stat__label', { text: '标题' }), el('span.stat__value', { style: { fontSize: '15px' }, text: truncate(result.report.title || '—', 28) })]),
          el('div.stat', {}, [el('span.stat__label', { text: '候选列表' }), el('span.stat__value', { text: String(result.report.candidate_lists?.length || 0) })]),
          el('div.stat', {}, [el('span.stat__label', { text: '分页器' }), el('span.stat__value', { style: { fontSize: '15px' }, text: result.report.pagination?.next_selector ? '有' : '无' })]),
        ]),
        el('button.btn.btn--sm.btn--ghost', {
          text: '查看完整报告',
          onclick: () => openModal({ title: '完整结构报告', body: jsonBlock(result.report, '70vh') }),
        }),
      ]),
    );
  }

  // 抓包任务: 记录列表
  if (task.kind === 'requests' && result.records) {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, [
          el('h2', { text: `抓到的请求(${result.matched} / ${result.total})` }),
          el('button.btn.btn--sm.btn--ghost', {
            text: '查看完整记录',
            onclick: () => openModal({ title: '完整抓包记录', body: jsonBlock(result.records, '70vh') }),
          }),
        ]),
        el(
          'div.record-list',
          {},
          result.records.slice(0, 200).map((rec) =>
            el('div.record', {}, [
              el('span.record__method', { dataset: { m: rec.method }, text: rec.method }),
              el('span.record__status', { text: String(rec.status ?? '—') }),
              el('span.record__url', { text: truncate(rec.url, 110) }),
              el('span.record__tags', {}, [rec.body_json ? el('span.record__badge.record__badge--json', { text: 'JSON' }) : null]),
            ]),
          ),
        ),
      ]),
    );
  }

  mount(detail, children);
}

/* ==========================================================================
   文件列表(当前选中任务所属 URL 的产出)
   --------------------------------------------------------------------------
   文件已经按 URL 归到左侧分组里了, 这个面板只显示**当前选中任务**那一组,
   方便在看完详情后直接取文件, 不用再回左侧找。
   ========================================================================== */
let currentGroupFiles = [];

export async function refreshFiles() {
  const list = $('#fileList');
  if (!list) return;
  if (!currentGroupFiles.length) {
    mount(
      list,
      el('p.empty', { text: '选中左侧的某个 URL, 这里会列出它对应的产出文件' }),
    );
    return;
  }
  mount(
    list,
    currentGroupFiles.map((file) =>
      el('div.file-item', {}, [
        el('span.tag.tag--ghost', { text: file.kind || 'file' }),
        el('div', { style: { flex: '1', minWidth: '0' } }, [
          el('div.file-item__name', { title: file.path, text: file.name }),
          el('div.file-item__meta', {
            text: `${file.exists ? formatBytes(file.bytes) : '文件已缺失'}${file.modified ? ' · ' + file.modified.replace('T', ' ') : ''}`,
          }),
        ]),
        el('button.btn.btn--sm.btn--ghost', {
          text: '预览',
          onclick: () => previewFile(file),
        }),
        el('button.btn.btn--sm.btn--ghost', {
          text: '下载',
          disabled: !file.exists,
          onclick: () => downloadFile(file),
        }),
        el('button.icon-btn.icon-btn--danger', {
          title: '删除',
          html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg>',
          onclick: () => removeFile(file),
        }),
      ]),
    ),
  );
}

/** 选中任务时, 把它所属 URL 的产出文件同步到右侧文件面板。 */
function syncFilesForTask(task) {
  const groups = get('urlGroups') || [];
  const taskUrl = task?.params?.url ?? null;
  const group = groups.find((g) => (g.url || null) === taskUrl);
  currentGroupFiles = group ? group.files : [];
  if (group) expandedUrls.add(group.url);
  refreshFiles();
}

async function downloadFile(file) {
  try {
    await api.downloadFile(file.path, file.name);
    toastSuccess(`已开始下载 ${file.name}`);
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

async function previewFile(file) {
  const ext = file.name.split('.').pop()?.toLowerCase();
  if (!['json', 'jsonl', 'csv', 'txt', 'log', 'md', 'yaml', 'yml'].includes(ext)) {
    toastInfo(`.${ext} 文件不支持文本预览,请直接下载`);
    return;
  }
  try {
    const data = await api.readFileText(file.path, 80000);
    const body =
      ext === 'json'
        ? jsonBlock(safeParse(data.content) ?? data.content, '70vh')
        : el('pre.json', { style: { maxHeight: '70vh' }, text: data.content });

    openModal({
      title: `${file.name}${data.truncated ? '(已截断预览)' : ''}`,
      body,
      footer: [
        el('span.hint', { text: `${formatBytes(data.size)}${data.truncated ? ' · 仅显示前 80KB' : ''}` }),
        el('button.btn.btn--sm.btn--primary', { text: '下载完整文件', onclick: () => downloadFile(file) }),
      ],
    });
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

function safeParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function removeFile(file) {
  const ok = window.confirm(`确定删除 ${file.name}?此操作不可撤销。`);
  if (!ok) return;
  try {
    await api.deleteFile(file.path);
    toastSuccess('已删除');
    // 删的是产出文件, 左侧分组里的文件清单也要跟着更新
    currentGroupFiles = currentGroupFiles.filter((f) => f.path !== file.path);
    refreshTasks();
    refreshFiles();
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

/* ==========================================================================
   初始化
   ========================================================================== */
export function init() {
  $('#btnRefreshTasks')?.addEventListener('click', () => {
    refreshTasks();
    refreshFiles();
    toastInfo('已刷新');
  });
  $('#btnRefreshFiles')?.addEventListener('click', () => {
    refreshTasks();
    refreshFiles();
    toastInfo('已刷新');
  });

  refreshTasks();
  refreshFiles();

  // 每 20 秒自动刷新一次(任务落盘后能及时看到)
  setInterval(() => {
    if (get('ui.page') === 'results') refreshTasks();
  }, 20000);
}

export default { init, refreshTasks, refreshFiles };
