/**
 * pages/crawl.js —— 抓取任务页
 * ---------------------------------------------------------------------------
 * 职责:
 *   1. 表单收集与本地校验(URL/规则 JSON),把界面参数翻成 /api/crawl 的请求体;
 *   2. 提交后建立任务 WebSocket,把 snapshot/status/result 事件渲染成时间线与结果表;
 *   3. 把最近一次结果写入 store,供"结果与历史"页复用。
 */

import { $, $$, el, mount, withLoading, truncate, downloadText, toCsv, flashScroll, formatBytes } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toast, toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, jsonBlock } from '../ui/modal.js';
import { renderTable } from '../ui/table.js';
import { connectTask, onTaskEvent, disconnectTask } from '../ws.js';
// 任务事件归属判断(三个页面共用同一条总线, 必须区分是谁的任务)
import { isOwnTaskEvent } from '../ui/task-owner.js';
// 人机验证入口(页面被挑战挡住时提示用户手动过一次)
import { renderChallenge } from '../ui/login-panel.js';

const SAMPLE_URL = 'https://books.toscrape.com';
const RULE_TEMPLATE = {
  mode: 'dom',
  list_rule: {
    item_selector: 'article.product_pod',
    fields: [
      { name: 'title', selector: 'h3 a', attribute: 'title', transform: ['strip'] },
      { name: 'price', selector: '.price_color', transform: ['price'] },
      { name: 'link', selector: 'h3 a', attribute: 'href', transform: ['url'] },
      { name: 'stock', selector: '.instock.availability', transform: ['strip'] },
    ],
  },
  pagination: { next_selector: 'li.next a', max_pages: 3 },
};

let activeTaskId = null;

/** 任务终态: 进入其中之一就不再是"进行中" */
const TERMINAL_STATUSES = ['success', 'failed', 'cancelled'];

/* ==========================================================================
   表单 <-> 请求体
   ========================================================================== */
function currentRuleMode() {
  return $('#ruleMode .segmented__item.is-active')?.dataset.mode || 'ai';
}

function setRuleMode(mode) {
  for (const btn of $$('#ruleMode .segmented__item')) {
    btn.classList.toggle('is-active', btn.dataset.mode === mode);
  }
  // 滑块跟随: 两个选项时 50% 宽度,用 translateX 移动
  const thumb = $('#ruleMode .segmented__thumb');
  if (thumb) thumb.style.transform = mode === 'manual' ? 'translateX(100%)' : 'translateX(0)';
  for (const panel of $$('[data-mode-panel]')) {
    panel.hidden = panel.dataset.modePanel !== mode;
  }
  const tag = $('#crawlModeTag');
  if (tag) tag.textContent = mode === 'manual' ? '手写规则' : 'AI 模式';
}

function collectPayload() {
  const mode = currentRuleMode();
  const url = $('#crawlUrl').value.trim();
  const payload = {
    url,
    goal: mode === 'ai' ? $('#crawlGoal').value.trim() || null : null,
    rule: mode === 'manual' ? $('#ruleJson').value.trim() || null : null,
    format: $('#crawlFormat').value || null,
    output: $('#crawlOutput').value.trim() || null,
    max_pages: numberOrNull($('#crawlMaxPages').value),
    incremental: $('#crawlIncremental').checked,
    use_ai: mode === 'ai' && $('#crawlUseAi').checked,
    wait: Number($('#crawlWait').value || 0),
    // 滚动加载: 用户可自己设定轮数; 留空则由后端配置决定
    scroll_rounds: numberOrNull($('#crawlScrollRounds')?.value),
    scroll_continue_rounds: numberOrNull($('#crawlScrollContinue')?.value),
    ask_scroll: $('#crawlAskScroll')?.checked !== false,
    // 下载数量: 留空则后端先从"抓取目标"里解析(如"爬取前三张"), 再退回插件配置
    media_limit: numberOrNull($('#crawlMediaLimit')?.value),
    // 抓取区域: 只在这一块里找候选列表, 页头导航/侧栏/页脚不会混进候选。
    // 留空 = 整页, 与旧行为一致。
    scope: ($('#crawlScope')?.value || '').trim(),
  };
  return payload;
}

function numberOrNull(raw) {
  const value = String(raw ?? '').trim();
  if (!value) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function validateForm() {
  const urlInput = $('#crawlUrl');
  const url = urlInput.value.trim();
  let ok = true;

  if (!/^https?:\/\/.+/i.test(url)) {
    urlInput.classList.add('is-invalid');
    ok = false;
    setTimeout(() => urlInput.classList.remove('is-invalid'), 1200);
    toastError('请填写以 http:// 或 https:// 开头的目标地址');
  }

  if (currentRuleMode() === 'manual') {
    const text = $('#ruleJson').value.trim();
    if (text) {
      try {
        JSON.parse(text);
      } catch (err) {
        $('#ruleJson').classList.add('is-invalid');
        setTimeout(() => $('#ruleJson').classList.remove('is-invalid'), 1200);
        toastError(`规则 JSON 语法错误: ${err.message}`);
        ok = false;
      }
    } else {
      toastError('手写规则模式下需要填写提取规则');
      ok = false;
    }
  } else if (!$('#crawlGoal').value.trim()) {
    toastInfo('未填写抓取目标,将退化为规则引擎自动提取');
  }

  return ok;
}

/* ==========================================================================
   时间线渲染
   ========================================================================== */
function renderTimeline(steps) {
  const list = $('#timeline');
  if (!list) return;
  if (!steps?.length) {
    mount(list, el('li.timeline__empty', { text: '尚未运行任务' }));
    return;
  }
  const frag = document.createDocumentFragment();
  steps.forEach((step, index) => {
    const item = el('li.timeline__item', {
      dataset: { status: step.status, key: step.key },
      style: { animationDelay: `${index * 40}ms` },
    }, [
      el('span.timeline__dot'),
      el('span.timeline__label', { text: step.label }),
      step.detail ? el('span.timeline__detail', { title: step.detail, text: step.detail }) : null,
    ]);
    frag.appendChild(item);
  });
  mount(list, frag);
}

/** 已渲染过的最大进度: 进度只前进不后退(避免旧快照/乱序事件让进度条回跳) */
let progressHighWater = 0;

function setProgress(progress) {
  const pct = Math.round((progress || 0) * 100);
  progressHighWater = Math.max(progressHighWater, pct);
  const fill = $('#progressFill');
  const label = $('#progressPct');
  if (fill) fill.style.width = `${progressHighWater}%`;
  if (label) label.textContent = `${progressHighWater}%`;
}

/** 开始一个新任务时重置进度水位 */
function resetProgress() {
  progressHighWater = 0;
  setProgress(0);
}

const STATUS_TEXT = {
  queued: ['排队中', 'idle'],
  running: ['进行中', 'running'],
  success: ['已完成', 'success'],
  failed: ['失败', 'failed'],
  cancelled: ['已取消', 'cancelled'],
};

/**
 * 切换任务状态显示。
 * @param {string} status
 * @param {?string} message 非空时会弹 toast
 * @param {?number} progress 0~1;传入时同时更新进度条(snapshot / status / ping 都带)
 */
function setTaskStatus(status, message, progress) {
  const tag = $('#taskStatusTag');
  const [text, state] = STATUS_TEXT[status] || ['未知', 'idle'];
  if (typeof progress === 'number') setProgress(progress);
  if (tag) {
    tag.textContent = text;
    tag.dataset.state = state;
  }
  const card = $('#progressCard');
  if (card) card.dataset.running = status === 'queued' || status === 'running' ? 'true' : 'false';

  const running = status === 'queued' || status === 'running';
  const startBtn = $('#btnCrawlStart');
  const cancelBtn = $('#btnCrawlCancel');
  if (startBtn) startBtn.hidden = running;
  if (cancelBtn) cancelBtn.hidden = !running;

  if (message) toastForStatus(status, message);
}

function toastForStatus(status, message) {
  if (status === 'success') toastSuccess(message, '抓取完成');
  else if (status === 'failed') toastError(message, '抓取失败');
  else if (status === 'cancelled') toastInfo(message, '任务已取消');
}

function renderTaskMeta(task) {
  const box = $('#taskMeta');
  if (!box || !task) return;
  const result = task.result || {};
  const cells = [
    ['条数', result.item_count ?? 0],
    ['页数', result.pages_crawled ?? 0],
    ['网络请求', result.network_record_count ?? 0],
    ['耗时', task.duration_ms ? `${(task.duration_ms / 1000).toFixed(1)}s` : '—'],
  ];
  box.hidden = false;
  mount(
    box,
    cells.map(([label, value]) =>
      el('div.task-meta__cell', {}, [el('dt', { text: label }), el('dd', { text: String(value) })]),
    ),
  );
}

/* ==========================================================================
   访问受限诊断
   ========================================================================== */

const ROLE_LABEL = {
  heading: '标题',
  error: '错误',
  form: '表单',
  button: '按钮',
  captcha: '验证码',
  message: '提示',
  label: '标签',
  link: '链接',
  meta: '元数据',
};

/** 每种问题类型的图标与配色, 让用户一眼看出该往哪个方向排查 */
const ISSUE_STYLE = {
  login_required: { icon: 'lock', tone: 'warn', label: '需要登录' },
  permission_denied: { icon: 'ban', tone: 'danger', label: '没有权限' },
  risk_control: { icon: 'shield', tone: 'danger', label: '风控拦截' },
  captcha: { icon: 'shield', tone: 'warn', label: '人机验证' },
  rate_limited: { icon: 'clock', tone: 'warn', label: '请求过频' },
  server_error: { icon: 'alert', tone: 'danger', label: '服务端错误' },
  not_found: { icon: 'search', tone: 'muted', label: '页面不存在' },
  spa_shell: { icon: 'clock', tone: 'info', label: '内容未渲染' },
  empty_page: { icon: 'layers', tone: 'info', label: '无数据' },
  unknown: { icon: 'alert', tone: 'warn', label: '原因不明' },
};

const ICON_PATHS = {
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="M12 8v4M12 16h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  layers: '<path d="M12 2 3 7l9 5 9-5-9-5Z"/><path d="m3 12 9 5 9-5"/>',
};

function svgIcon(paths, cls = 'i') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.9');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('class', cls);
  svg.innerHTML = paths;
  return svg;
}

/**
 * 渲染访问受限诊断卡。
 *
 * 三种"拿不到数据"的形态用不同视觉强度呈现:
 *   - `empty_page` / `spa_shell`: 并不是访问受限, 用普通提示条, 不喧哗;
 *   - 其余(登录/权限/风控/验证码/…): 用醒目警示卡, 并把**页面实际显示的内容**
 *     完整摊开 —— 这正是用户判断问题所需的原始信息。
 *
 * 特别地区分"需要登录"与"没有权限": 两者的表象都是 0 条数据, 但处置方式完全不同。
 * 洛谷训练页就是典型 —— 返回 HTTP 401 但**不跳转登录页**, 页面写的是"没有权限请求
 * 此资源"。把它当成登录问题会让用户白折腾半天。
 */
export function renderAccessIssue(issue) {
  if (!issue) return null;

  const style = ISSUE_STYLE[issue.issue_type] || ISSUE_STYLE.unknown;
  const isSoft = issue.issue_type === 'empty_page' || issue.issue_type === 'spa_shell';

  // ---- 轻量提示(不是访问受限, 或只是没数据) ----
  if (!issue.detected || isSoft) {
    return el('div.alert.alert--info', {}, [
      el('div.alert__body', {}, [
        el('div.alert__title', { text: issue.title || style.label }),
        el('div', { text: issue.explanation || '' }),
        (issue.suggestions || []).length
          ? el(
              'ul.login-wall__list',
              { style: { marginTop: '8px' } },
              issue.suggestions.map((s) => el('li', { text: s })),
            )
          : null,
      ]),
    ]);
  }

  // ---- 醒目警示卡 ----
  const reasons = el(
    'ul.login-wall__list',
    {},
    (issue.reasons || []).slice(0, 10).map((r) => el('li', { text: r })),
  );

  const elements = (issue.text_elements || []).length
    ? el(
        'div.login-wall__elements',
        {},
        issue.text_elements.slice(0, 14).map((item) =>
          el('div.login-wall__element', {}, [
            el('span.login-wall__role', {
              dataset: { role: item.role },
              text: ROLE_LABEL[item.role] || item.role || item.tag,
            }),
            el('div', {}, [
              el('span.login-wall__text', { text: truncate(item.text, 260) }),
              item.selector ? el('code.login-wall__sel', { text: item.selector }) : null,
            ]),
          ]),
        ),
      )
    : el('p.hint', { text: '未能从页面提取到可读文本元素。' });

  const redirectRow = el('div.login-wall__redirect', {}, [
    el('span.from', { text: issue.requested_url || '(未知)' }),
    issue.redirected ? el('span.arrow', { text: '→' }) : null,
    issue.redirected ? el('span.to', { text: issue.final_url || '(空页面)' }) : null,
    el('span.tag.tag--ghost', { text: `HTTP ${issue.http_status ?? '—'}` }),
    el('span.tag', {
      class: `tag--${style.tone === 'danger' ? 'danger' : 'warning'}`,
      text: style.label,
    }),
  ]);

  // 归因得分: 让用户明白"为什么归到这一类", 而不是只看到一个结论
  const scoreChips = Object.entries(issue.scores || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([kind, score]) =>
      el('span.field-chip', {
        text: `${ISSUE_STYLE[kind]?.label || kind} ${Math.round(score * 100)}%`,
      }),
    );

  const hasMeta = issue.metadata && Object.keys(issue.metadata).length > 0;

  return el('div.login-wall', { dataset: { type: issue.issue_type } }, [
    el('div.login-wall__head', {}, [
      el('div.login-wall__icon', {}, svgIcon(ICON_PATHS[style.icon] || ICON_PATHS.alert)),
      el('div', { style: { flex: '1', minWidth: '0' } }, [
        el('div.login-wall__title', {
          text: `${issue.title || style.label} · 置信度 ${Math.round((issue.confidence || 0) * 100)}%`,
        }),
        el('div.login-wall__sub', { text: issue.explanation || '' }),
      ]),
      el('span.tag.tag--ghost', { text: `type: ${issue.issue_type}` }),
    ]),
    el('div.login-wall__body', {}, [
      redirectRow,
      el('div.login-wall__section', {}, [el('h4', { text: '判定依据' }), reasons]),
      scoreChips.length
        ? el('div.login-wall__section', {}, [
            el('h4', { text: '类型归因得分' }),
            el('div.field-chips', {}, scoreChips),
          ])
        : null,
      el('div.login-wall__section', {}, [
        el('h4', { text: '页面关键文本元素' }),
        elements,
      ]),
      issue.error_codes && Object.keys(issue.error_codes).length
        ? el('div.login-wall__section', {}, [
            el('h4', { text: '提取到的错误码 / 请求 ID' }),
            el(
              'div.field-chips',
              {},
              Object.entries(issue.error_codes).map(([k, v]) =>
                el('span.field-chip', { text: `${k} = ${v}` }),
              ),
            ),
          ])
        : null,
      (issue.suggestions || []).length
        ? el('div.login-wall__section', {}, [
            el('h4', { text: '可以怎么做' }),
            el('ul.login-wall__list', {}, issue.suggestions.map((s) => el('li', { text: s }))),
          ])
        : null,
      // 原始文本默认折叠: 需要深挖时再看, 平时不占屏幕
      issue.visible_text
        ? el('details.tree', {}, [
            el('summary', { text: '展开页面完整可见文本' }),
            el('pre.json', { style: { maxHeight: '260px' }, text: issue.visible_text }),
          ])
        : null,
      hasMeta
        ? el('details.tree', {}, [
            el('summary', { text: '展开页面元数据 / 初始化变量' }),
            el('pre.json', {
              style: { maxHeight: '220px' },
              text: JSON.stringify(issue.metadata, null, 2),
            }),
          ])
        : null,
    ]),
  ]);
}

/** 兼容旧名称(分析页按这个别名导入, 避免两处渲染逻辑分叉) */
export const renderLoginWall = renderAccessIssue;

function renderDownloads(payload) {
  const downloads = payload.downloads || [];
  if (!downloads.length) return null;

  const ok = downloads.filter((d) => d.ok);
  return el('div.card.glass', {}, [
    el('div.card__head', {}, [
      el('h2', {}, [
        el('svg.i', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', html: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>' }),
        '插件下载产物',
      ]),
      el('div.card__tools', {}, [
        el('span.tag.tag--success', { text: `成功 ${ok.length} / ${downloads.length}` }),
        payload.plugins_used?.length
          ? el('span.tag.tag--ghost', { text: `插件: ${payload.plugins_used.join(', ')}` })
          : null,
        el('button.btn.btn--sm.btn--ghost', {
          text: '查看清单',
          onclick: () =>
            openModal({
              title: '下载清单',
              body: jsonBlock(downloads, '68vh'),
            }),
        }),
      ]),
    ]),
    el(
      'div.download-list',
      {},
      downloads.slice(0, 36).map((d) =>
        el('div.download-item', { dataset: { ok: d.ok ? '1' : '0' }, title: d.path || d.url }, [
          el('div.download-item__icon', {
            html: d.ok
              ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M20 6 9 17l-5-5"/></svg>'
              : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
          }),
          el('div.download-item__body', {}, [
            el('div.download-item__name', { text: d.filename || d.url }),
            el('div.download-item__meta', {
              text: d.ok
                ? `${formatBytes(d.size)} · ${(d.mime_type || '').split(';')[0] || '未知类型'}`
                : truncate(d.error || '下载失败', 46),
            }),
          ]),
        ]),
      ),
    ),
    downloads.length > 36
      ? el('p.hint', { text: `共 ${downloads.length} 个文件, 此处显示前 36 个。可在「结果与历史 → 产出文件」里浏览全部。` })
      : null,
  ]);
}

export function renderCrawlResult(payload) {
  const card = $('#crawlResultCard');
  const body = $('#resultBody');
  if (!card || !body) return;

  card.hidden = false;
  const countTag = $('#resultCount');
  if (countTag) countTag.textContent = `${payload.item_count ?? 0} 条`;

  const children = [];

  // 访问受限诊断放在最前面: 空结果时它是最需要被看到的信息
  // 人机验证优先展示: 页面被挑战挡住时, 先过验证再谈其它诊断
  const challengePanel = renderChallenge(payload.challenge, payload.url || $('#crawlUrl')?.value, {
    onResolved: (url, action) => {
      const target = url || payload.url || $('#crawlUrl')?.value;
      if (target && $('#crawlUrl')) $('#crawlUrl').value = target;
      toastInfo('验证已通过, 正在用新会话重新抓取…');
      if (action === 'crawl' || action === 'analyze') startCrawl();
    },
  });
  if (challengePanel) children.push(challengePanel);

  const accessIssue = renderAccessIssue(payload.access_issue);
  if (accessIssue) children.push(accessIssue);

  // 诊断卡已经完整解释了"为什么没数据", 再叠一条通用错误条只会重复刷屏。
  // 诊断未覆盖到的错误(如规则问题)仍然照常显示。
  const issueExplainsFailure =
    Boolean(payload.access_issue?.detected) && (payload.item_count ?? 0) === 0;

  if (payload.errors?.length && !issueExplainsFailure) {
    children.push(
      el('div.alert.alert--warn', {}, [
        el('div.alert__body', {}, [
          el('div.alert__title', { text: `执行过程中有 ${payload.errors.length} 条提示` }),
          el('div', { text: payload.errors.slice(0, 4).join(' / ') }),
        ]),
      ]),
    );
  }

  if (payload.plugin_errors?.length) {
    children.push(
      el('div.alert.alert--warn', {}, [
        el('div.alert__body', {}, [
          el('div.alert__title', { text: `插件执行有 ${payload.plugin_errors.length} 条错误(已隔离, 不影响抓取)` }),
          el('div', { text: payload.plugin_errors.slice(0, 3).join(' / ') }),
        ]),
      ]),
    );
  }

  // 规则来源提示: 让用户知道这次数据是怎么来的
  if (payload.rule) {
    const srcText = { ai: 'AI 生成', rule: '规则引擎', manual: '手写规则' }[payload.rule.source] || payload.rule.source;
    children.push(
      el('div.alert.alert--info', {}, [
        el('div.alert__body', {}, [
          el('div.alert__title', { text: `提取规则来自: ${srcText}` }),
          el('div.mono', { text: `item_selector: ${payload.rule.list_rule?.item_selector || '—'}` }),
          payload.rule.notes ? el('div.hint', { text: `推理说明: ${truncate(payload.rule.notes, 300)}` }) : null,
        ]),
      ]),
    );
  }

  children.push(
    renderTable({
      columns: payload.columns,
      items: payload.items_preview || [],
      limit: 300,
    }),
  );

  // 插件下载产物: 有下载时单独成卡, 让用户能直接看到文件已落地
  const downloadCard = renderDownloads(payload);
  if (downloadCard) children.push(downloadCard);

  mount(body, children);

  // 下载按钮的可用性
  const csvBtn = $('#btnResultCsv');
  if (csvBtn) csvBtn.hidden = !(payload.items_preview?.length);

  set('lastCrawl', payload);
}

/* ==========================================================================
   任务事件
   ========================================================================== */
function handleTaskEvent(event) {
  // 归属判断统一走 isOwnTaskEvent: 以任务 id 为准, 而不是 store.task 的 kind ——
  // store.task 可能来自别的页面(先在分析页跑过任务, 再来抓取页), 只看 kind 会把
  // 本页自己的事件挡掉。
  if (!isOwnTaskEvent(event, { activeTaskId, knownTask: get('task'), kind: 'crawl' })) return;

  switch (event.type) {
    case 'snapshot': {
      const task = event.task;
      // 快照可能来自别的类型的任务(共享 store 时的竞态), 直接忽略
      if (task.kind && task.kind !== 'crawl') break;
      activeTaskId = task.id;
      set('task', task);
      renderTimeline(task.steps);
      // 快照携带完整进度: 刷新页面后能立刻恢复到正确的进度位置
      progressHighWater = 0;
      setTaskStatus(task.status, null, task.progress);
      if (task.status === 'success' && task.result) {
        renderCrawlResult(task.result);
        renderTaskMeta(task);
      }
      break;
    }
    case 'status': {
      if (event.task_id) activeTaskId = event.task_id;
      set('task.status', event.status);
      setTaskStatus(event.status, event.message, event.progress);
      // 进入终态时重绘时间线, 让步骤圆点与 100% 的进度条保持一致
      if (TERMINAL_STATUSES.includes(event.status)) syncTimelineFromServer();
      break;
    }
    case 'warning': {
      toast(event.message || '任务提示', { type: 'warn' });
      break;
    }
    case 'plugin': {
      // 插件通过 on_progress 上报的进度(下载数量、清理统计等), 只走 toast 不刷表格
      const level = String(event.level || 'INFO').toUpperCase();
      if (level === 'WARNING' || level === 'ERROR') {
        toast(event.message || '插件提示', { type: 'warn' });
      } else {
        set('lastPluginMessage', event.message || '');
      }
      break;
    }
    case 'confirm_scroll': {
      // 内容仍在持续增长(无限流): 后端**停下来等用户回话**。
      // 用户可以一直点"继续", 直到点"停止"为止 —— 决定权在用户手里。
      showScrollPrompt(event);
      break;
    }
    case 'result': {
      renderCrawlResult(event.payload);
      const task = get('task');
      if (task) renderTaskMeta({ ...task, result: event.payload });

      // 兜底: 若终态 status 事件丢失, 任务会永远显示"进行中"(取消按钮不消失)。
      // 后端现在保证先推 result 再推 status, 但这里仍按结果自行判定一次终态,
      // 避免 UI 卡死 —— 显示正确比严格依赖事件顺序更重要。
      const current = get('task.status');
      if (!TERMINAL_STATUSES.includes(current)) {
        const payload = event.payload || {};
        const ok = payload.success !== false && (payload.item_count ?? 0) > 0;
        set('task.status', ok ? 'success' : 'failed');
        // 结果不完整(部分成功)时给出提示, 否则等随后的 status 事件带上正式文案
        setTaskStatus(
          ok ? 'success' : 'failed',
          ok && payload.errors?.length ? `完成, 但有 ${payload.errors.length} 条提示` : null,
        );
      }
      // 时间线需要一次"终态重绘": 后端的 finish() 会把未显式标记的步骤补成 done,
      // 但那份数据只存在于服务端。前端若不重新拉取, 步骤圆点会一直停在灰色,
      // 与已经 100% 的进度条自相矛盾。
      syncTimelineFromServer();
      break;
    }
    case 'ping': {
      // 心跳兜底: 若某次终态事件丢了, 最多 20 秒后也能通过心跳纠正状态与进度
      if (event.status) {
        setTaskStatus(event.status, null, event.progress);
        if (TERMINAL_STATUSES.includes(event.status)) syncTimelineFromServer();
      } else if (typeof event.progress === 'number') {
        setProgress(event.progress);
      }
      break;
    }
    default:
      break;
  }

  // 任务结束后刷新历史列表
  const task = get('task');
  if (task && TERMINAL_STATUSES.includes(task.status) && event.type !== 'ping') {
    const last = get('lastTaskRefresh') || 0;
    if (Date.now() - last > 1500) {
      set('lastTaskRefresh', Date.now());
      // 动态 import 避免与 results 页形成循环依赖
      import('./results.js').then((mod) => mod.refreshTasks?.()).catch(() => {});
    }
  }
}

/**
 * 从后端拉一次任务详情并重绘时间线。
 *
 * 任务结束时调用: 服务端 finish() 会把未显式标记的步骤补成 done(并填上进度),
 * 这些信息只在服务端。不重绘的话, 步骤圆点会保持任务结束前的灰色状态。
 */
async function syncTimelineFromServer() {
  const current = get('task');
  if (!current?.id) return;
  try {
    const fresh = await api.getTask(current.id);
    set('task', fresh);
    renderTimeline(fresh.steps);
    // 只更新进度条(不重复触发状态标签/toast)
    if (typeof fresh.progress === 'number') setProgress(fresh.progress);
    if (fresh.result?.items_preview && !current.result?.items_preview) {
      renderCrawlResult(fresh.result);
    }
  } catch {
    /* 任务可能已被回收, 忽略即可 —— 界面已有兜底终态 */
  }
}

/* ==========================================================================
   提交 / 取消
   ========================================================================== */
async function startCrawl() {
  if (!validateForm()) return;
  const payload = collectPayload();
  const restore = withLoading($('#btnCrawlStart'), '抓取中…');

  // 重置视图
  renderTimeline([]);
  resetProgress();
  mount($('#timeline'), el('li.timeline__empty', { text: '正在提交任务…' }));

  try {
    const { task } = await api.startCrawl(payload);
    activeTaskId = task.id;
    set('task', task);
    renderTimeline(task.steps);
    setTaskStatus('running', '任务已提交,正在启动浏览器…', task.progress);
    connectTask(task.id);
    expandResultIfNeeded();
  } catch (err) {
    setTaskStatus('failed', null);
    toastError(err instanceof ApiError ? err.message : String(err), '提交失败');
  } finally {
    restore();
  }
}

/**
 * 显示"是否继续向下滚动"的交互条。
 *
 * 关键点: 这是**阻塞式**的 —— 后端此刻正停在那里等回话, 所以:
 *   - 必须能一直点"继续", 每点一次后端就再滚一段, 然后再问一次;
 *   - 只有用户点"停止滚动"才结束(或后端等待超时)。
 * 这样"无上限页面"就完全由用户掌握节奏, 而不是被一个固定轮数截断。
 */
function showScrollPrompt(event) {
  const payload = event.payload || {};
  const suggested = Number(payload.suggested_rounds) || 10;
  const roundsInput = el('input.input.input--tiny', {
    type: 'number',
    min: '1',
    max: '200',
    value: String(suggested),
    title: '本次追加的滚动轮次',
  });

  const box = $('#scrollPrompt');
  if (!box) return;

  const busy = (on) => {
    box.querySelectorAll('button').forEach((b) => {
      b.disabled = on;
    });
  };

  const answer = async (cont) => {
    const task = get('task');
    if (!task?.id) return;
    busy(true);
    try {
      await api.answerScroll(task.id, cont, Number(roundsInput.value) || suggested);
      if (cont) {
        toastInfo(`继续向下滚动 ${roundsInput.value || suggested} 轮…`);
        box.hidden = false;
      } else {
        toastInfo('已停止滚动, 将按当前已加载的内容出结果');
        box.hidden = true;
      }
    } catch (err) {
      toastError(err?.message || '回话失败');
      busy(false);
    }
  };

  const children = [
    el('div', { style: { flex: '1', minWidth: '0' } }, [
      el('div', { text: event.message || '页面内容仍在持续增长, 是否继续向下滚动?' }),
      el('div.hint', {
        style: { marginTop: '4px' },
        text:
          `${payload.summary || ''}。继续会加载更多内容, 但也会更慢; ` +
          '可以一直点「继续」直到你满意, 或点「停止滚动」按当前内容出结果。',
      }),
    ]),
    el('div.scroll-prompt__actions', {}, [
      el('span.hint', { text: '追加' }),
      roundsInput,
      el('span.hint', { text: '轮' }),
      el('button.btn.btn--sm.btn--primary', {
        text: '继续向下滚动',
        onclick: () => answer(true),
      }),
      el('button.btn.btn--sm', {
        text: '停止滚动',
        onclick: () => answer(false),
      }),
    ]),
  ];

  mount(
    box,
    el('div.alert.alert--warning.alert--sticky', {}, [
      el(
        'div.alert__body',
        { style: { display: 'flex', gap: 'var(--sp-3)', flexWrap: 'wrap', alignItems: 'flex-start' } },
        children,
      ),
    ]),
  );
  box.hidden = false;
  flashScroll(box);
}

function expandResultIfNeeded() {
  const card = $('#crawlResultCard');
  if (card && !card.hidden) {
    flashScroll(card);
  }
}

async function cancelCrawl() {
  if (!activeTaskId) return;
  try {
    const res = await api.cancelTask(activeTaskId);
    toastInfo(res.message || '已发送取消信号');
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

function resetForm() {
  $('#crawlForm')?.reset();
  $('#crawlUrl').value = '';
  $('#crawlGoal').value = '';
  $('#ruleJson').value = '';
  $('#crawlFormat').value = 'csv';
  $('#crawlWait').value = '0';
  $('#crawlUseAi').checked = true;
  setRuleMode('ai');
  $('#ruleStatus').hidden = true;
  renderTimeline([]);
  resetProgress();
  setTaskStatus('idle', null);
  const card = $('#crawlResultCard');
  if (card) card.hidden = true;
  toastInfo('表单已重置');
}

/* ==========================================================================
   规则工具
   ========================================================================== */
async function validateRule() {
  const text = $('#ruleJson').value.trim();
  const status = $('#ruleStatus');
  if (!text) {
    status.hidden = false;
    status.dataset.ok = '0';
    status.textContent = '规则为空';
    return;
  }

  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    status.hidden = false;
    status.dataset.ok = '0';
    status.textContent = `JSON 语法错误: ${err.message}`;
    return;
  }

  try {
    const res = await api.validateRule(parsed);
    status.hidden = false;
    status.dataset.ok = res.ok ? '1' : '0';
    status.textContent = res.ok
      ? `✓ 规则结构合法(${res.field_count} 个字段)。${(res.notes || []).join(' ')}`
      : `✗ ${res.error}`;
  } catch (err) {
    status.hidden = false;
    status.dataset.ok = '0';
    status.textContent = err instanceof ApiError ? err.message : String(err);
  }
}

function formatRule() {
  const box = $('#ruleJson');
  try {
    box.value = JSON.stringify(JSON.parse(box.value), null, 2);
    toastSuccess('已格式化');
  } catch (err) {
    toastError(`无法格式化: ${err.message}`);
  }
}

/* ==========================================================================
   初始化
   ========================================================================== */
export function init() {
  setRuleMode('ai');

  // 分段控件
  for (const btn of $$('#ruleMode .segmented__item')) {
    btn.addEventListener('click', () => setRuleMode(btn.dataset.mode));
  }

  $('#btnSample')?.addEventListener('click', () => {
    $('#crawlUrl').value = SAMPLE_URL;
    if (!$('#crawlGoal').value.trim()) $('#crawlGoal').value = '抓取所有书籍的名称、价格和详情页链接';
    toastInfo('已填入示例站点 books.toscrape.com(专供爬虫练习,允许抓取)');
  });

  $('#crawlForm')?.addEventListener('submit', (event) => {
    event.preventDefault();
    startCrawl();
  });
  $('#btnCrawlStart')?.addEventListener('click', startCrawl);
  $('#btnCrawlCancel')?.addEventListener('click', cancelCrawl);
  $('#btnCrawlReset')?.addEventListener('click', resetForm);

  $('#btnRuleTemplate')?.addEventListener('click', () => {
    $('#ruleJson').value = JSON.stringify(RULE_TEMPLATE, null, 2);
    setRuleMode('manual');
    toastInfo('已插入模板,请按目标页面调整选择器');
  });
  $('#btnRuleValidate')?.addEventListener('click', validateRule);
  $('#btnRuleFormat')?.addEventListener('click', formatRule);

  // URL 实时提示
  const urlInput = $('#crawlUrl');
  urlInput?.addEventListener('input', () => {
    const hint = $('#urlHint');
    if (!hint) return;
    const value = urlInput.value.trim();
    if (!value) {
      hint.textContent = '支持 http/https;框架会先检查 robots.txt 再抓取。';
      hint.style.color = '';
    } else if (!/^https?:\/\//i.test(value)) {
      hint.textContent = '地址需要以 http:// 或 https:// 开头';
      hint.style.color = 'var(--warning)';
    } else {
      try {
        const u = new URL(value);
        hint.textContent = `目标主机: ${u.hostname}`;
        hint.style.color = '';
      } catch {
        hint.textContent = '地址格式似乎不完整';
        hint.style.color = 'var(--warning)';
      }
    }
  });

  // 结果操作
  $('#btnResultJson')?.addEventListener('click', () => {
    const payload = get('lastCrawl');
    if (!payload) return;
    openModal({
      title: '抓取结果 (JSON)',
      body: jsonBlock(
        {
          url: payload.url,
          goal: payload.goal,
          item_count: payload.item_count,
          pages_crawled: payload.pages_crawled,
          saved_to: payload.saved_to,
          rule: payload.rule,
          items: payload.items_preview,
        },
        '66vh',
      ),
    });
  });

  $('#btnResultCsv')?.addEventListener('click', () => {
    const payload = get('lastCrawl');
    if (!payload?.items_preview?.length) return;
    const cols = payload.columns?.length ? payload.columns : Object.keys(payload.items_preview[0]);
    downloadText(`smartcrawler_${Date.now()}.csv`, toCsv(cols, payload.items_preview), 'text/csv;charset=utf-8');
    toastSuccess('已导出当前预览数据为 CSV');
  });

  $('#btnResultDownload')?.addEventListener('click', () => {
    const task = get('task');
    if (!activeTaskId || !task?.artifacts?.length) {
      toastInfo('该任务暂无可下载的产物');
      return;
    }
    // 产物已随任务结果落盘,直接走任务下载端点(不依赖文件白名单)
    window.location.href = api.taskDownloadUrl(activeTaskId, 'artifact');
    toastSuccess('已开始下载完整结果');
  });

  // 快捷操作
  $('#quickAnalyze')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('app:navigate', { detail: 'analyze' })));
  $('#quickRequests')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('app:navigate', { detail: 'requests' })));
  $('#quickConfig')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('app:navigate', { detail: 'settings' })));
  $('#quickFiles')?.addEventListener('click', () => window.dispatchEvent(new CustomEvent('app:navigate', { detail: 'results' })));

  onTaskEvent(handleTaskEvent);

  // 恢复上次的任务视图(刷新页面后仍能看到真实进度)
  restoreLastTask();
}

/**
 * 页面加载时恢复任务视图。
 *
 * 这里**重新向后端拉一次任务详情**, 而不是只读 localStorage 里的旧快照 —— 旧快照
 * 是提交那一刻的副本(进度 0、步骤全 pending), 直接渲染会让刚刷新的页面看起来
 * "任务还没开始"。拿到权威状态后再连 WebSocket, 后续增量事件继续接管。
 */
async function restoreLastTask() {
  const stored = get('task');
  if (!stored?.id) return;
  // store.task 是三页共享的: 它可能是分析/抓包任务。抓取页只接管抓取任务,
  // 否则会去连别的任务的事件流, 并把它们的状态(如 running)显示在抓取页上,
  // 而后续事件又会被 kind 过滤器挡掉 —— 界面就会永远停在"进行中"。
  if (stored.kind && stored.kind !== 'crawl') return;
  activeTaskId = stored.id;

  try {
    const task = await api.getTask(stored.id);
    if (task.kind && task.kind !== 'crawl') return; // 二次确认, 防止竞态
    set('task', task);
    renderTimeline(task.steps);
    progressHighWater = 0;
    setTaskStatus(task.status, null, task.progress);
    if (task.result?.items_preview) {
      renderCrawlResult(task.result);
      renderTaskMeta(task);
    }
    // 仍在进行中的抓取任务: 重建事件流(建连即收到快照)
    if (!TERMINAL_STATUSES.includes(task.status)) connectTask(task.id);
  } catch {
    // 任务已被回收(服务重启): 用旧快照做降级显示即可
    renderTimeline(stored.steps);
    setTaskStatus(stored.status, null, stored.progress);
  }
}

export { startCrawl, activeTaskId };
export default { init };
