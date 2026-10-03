/**
 * pages/analyze.js —— 页面结构分析
 * ---------------------------------------------------------------------------
 * 把 /api/analyze 返回的结构报告渲染成"可操作的建议":
 *   - 候选列表按重复项数排序,直接给出 item_selector 与字段选择器(可一键复制);
 *   - 分页器、元数据、DOM 统计以卡片呈现;
 *   - 提供"把这份结构交给抓取页"的入口,省去手工抄选择器。
 */

import {
  $,
  el,
  mount,
  withLoading,
  truncate,
  copyText,
  flashScroll,
  formatNumber,
  renderPlainText,
} from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, jsonBlock } from '../ui/modal.js';
import { connectTask, onTaskEvent, disconnectTask } from '../ws.js';
import { goToPage } from '../router.js';
// 复用抓取页的诊断卡渲染, 保证两个页面的提示完全一致
import { renderAccessIssue } from './crawl.js';
// 登录状态提示 + 登录流程弹层(未登录时询问用户是否登录), 以及人机验证/懒加载入口
import { renderLoginState, renderChallenge, renderLazyLoad } from '../ui/login-panel.js';
// 任务事件归属判断(三个页面共用同一条总线, 必须区分是谁的任务)
import { isOwnTaskEvent } from '../ui/task-owner.js';

let activeTaskId = null;

/* ==========================================================================
   渲染
   ========================================================================== */
function copyable(text, label = null) {
  const node = el('span.copyable.mono', { title: '点击复制', text: label ?? text });
  node.addEventListener('click', async () => {
    const ok = await copyText(text);
    node.classList.add('is-copied');
    setTimeout(() => node.classList.remove('is-copied'), 700);
    if (ok) toastSuccess(`已复制: ${truncate(text, 60)}`);
    else toastError('复制失败,请手动选择文本');
  });
  return node;
}

function statCard(label, value, suffix = '') {
  return el('div.stat', {}, [
    el('span.stat__label', { text: label }),
    el('span.stat__value', {}, [String(value), suffix ? el('small', { text: suffix }) : null]),
  ]);
}

function renderStats(report, networkStats) {
  const dom = report.dom_stats || {};
  return el('div.stats-row', {}, [
    statCard('候选列表', (report.candidate_lists || []).length),
    statCard('DOM 节点', formatNumber(dom.total_nodes ?? 0)),
    statCard('链接数', formatNumber(dom.links ?? 0)),
    statCard('表单/输入', formatNumber(dom.inputs ?? 0)),
    statCard('网络请求', formatNumber(networkStats?.total ?? 0)),
    statCard('JSON 响应', formatNumber(networkStats?.json ?? 0)),
  ]);
}

function candidateCard(candidate, index) {
  const fields = candidate.sample_fields || [];
  return el('div.candidate', { style: { animationDelay: `${index * 60}ms` } }, [
    el('div.candidate__head', {}, [
      el('span.tag', { text: `${candidate.count} 项` }),
      copyable(candidate.item_selector),
    ]),
    el('div.candidate__body', {}, [
      candidate.container_selector
        ? el('div.hint', {}, ['容器: ', copyable(candidate.container_selector)])
        : null,
      fields.length
        ? el(
            'div.field-chips',
            {},
            fields.map((f) => {
              const spec = `${f.selector}${f.attribute ? ` @${f.attribute}` : ''}`;
              return el('span.field-chip', {}, [`${f.name}: `, copyable(spec, spec)]);
            }),
          )
        : el('p.hint', { text: '未能从样本推断出字段,可切换到"网络抓包"看看接口数据。' }),
      candidate.sample_html
        ? el('details.tree', {}, [
            el('summary', { text: '查看样本 HTML' }),
            // 用纯文本视图而不是 JSON 着色: HTML 不是 JSON, 套 JSON 着色器会乱染色,
            // 而且压缩过的 HTML 单行可能上万字符, 需要按属性折断才看得清。
            el('pre.json.plain', {
              style: { maxHeight: '220px' },
              html: renderPlainText(candidate.sample_html, { maxLineLength: 120 }),
            }),
          ])
        : null,
    ]),
  ]);
}

function renderReport(payload) {
  const out = $('#analyzeOutput');
  if (!out) return;
  const report = payload.report || {};
  const children = [];

  // 人机验证置顶(优先级高于登录提示): 页面被挑战挡住时, 先过验证才有意义。
  // 同时把 challengeDetected 告诉登录提示 —— 被验证挡住时不应引导用户去登录。
  const challengePanel = renderChallenge(report.challenge, report.url, {
    onResolved: (url, action) => {
      const target = url || report.url || $('#analyzeUrl')?.value;
      if (!target) return;
      const field = $('#analyzeUrl');
      if (field) field.value = target;
      toastInfo('验证已通过, 正在用新会话重新分析…');
      startAnalyze();
    },
  });
  const challengeDetected = Boolean(report.challenge?.detected);

  // 登录状态: 有些站点登录前后**页面结构完全不同**(pixiv 首页匿名时是注册引导页)。
  // 这时最该先问用户"要不要登录", 而不是让他拿着匿名视图的结构去调选择器 ——
  // 但若页面正被人机验证挡住, 就不是登录问题, 见 challengeDetected 的处理。
  const loginPanel = renderLoginState(report.login_state, report.url, {
    challengeDetected,
    onSessionSaved: (url, action) => {
      if (action !== 'saved' && action !== 'analyze') return;
      const target = url || report.url || $('#analyzeUrl')?.value;
      if (!target) return;
      const field = $('#analyzeUrl');
      if (field) field.value = target;
      toastInfo('正在用登录后的身份重新分析…');
      startAnalyze();
    },
  });

  if (loginPanel) children.push(loginPanel);

  // 人机验证提示**不放进报告堆栈** —— 放到页面顶部的专属槽位(.alert-slot, sticky 在
  // .content 顶部)。放堆栈里会两头不讨好: 不 sticky 就随报告滚出视口、按钮点不到;
  // sticky 又会浮在其它卡片上把它们遮住。挪到滚动容器之外就没有这个矛盾了。
  const alertSlot = $('#analyzeAlertSlot');
  if (alertSlot) {
    if (challengePanel) {
      mount(alertSlot, challengePanel);
      alertSlot.hidden = false;
    } else {
      mount(alertSlot, []);
      alertSlot.hidden = true;
    }
  }

  // 懒加载/无限流: 内容没有上限时由用户决定是否继续滚(框架不替他决定抓多久)
  const lazyPanel = renderLazyLoad(report.lazy_load, {
    onContinue: (extra) => {
      const field = $('#analyzeUrl');
      const target = report.url || field?.value;
      if (target && field) field.value = target;
      toastInfo(`正在继续向下滚动 ${extra} 轮, 加载更多内容…`);
      startAnalyze({ deepScroll: extra });
    },
  });
  if (lazyPanel) children.push(lazyPanel);

  // 访问受限诊断: 分析一个"看起来正常"的页面却拿不到列表结构时, 用户最需要
  // 立刻知道是需登录、无权限还是风控, 而不是页面真的没有列表。
  const accessIssue = renderAccessIssue(report.access_issue);
  if (accessIssue) children.push(accessIssue);

  // 概览
  children.push(
    el('div.card.glass', {}, [
      el('div.card__head', {}, [
        el('h2', { text: report.title || '未取到标题' }),
        el('span.tag.tag--ghost', { text: report.url || '' }),
      ]),
      renderStats(report, payload.network_stats),
      report.pagination?.next_selector
        ? el('div.pager-info', {}, [
            el('span', { text: '检测到分页器:' }),
            copyable(report.pagination.next_selector),
            report.pagination.next_text
              ? el('span.hint', { text: `按钮文字: ${report.pagination.next_text}` })
              : null,
          ])
        : el('div.alert.alert--info', {}, [
            el('div.alert__body', {
              text: '未检测到"下一页"按钮 —— 该页面可能是无限滚动,或数据来自 XHR 接口。',
            }),
          ]),
      el('div.card__tools', {}, [
        el('button.btn.btn--sm.btn--primary', {
          text: '用这份结构去抓取',
          onclick: () => handoffToCrawl(report),
        }),
        el('button.btn.btn--sm.btn--ghost', {
          text: '查看完整报告',
          onclick: () => openModal({ title: '完整结构报告', body: jsonBlock(report, '70vh') }),
        }),
      ]),
    ]),
  );

  // 候选列表
  if (report.candidate_lists?.length) {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, [
          el('h2', { text: `候选列表区(${report.candidate_lists.length})` }),
          el('span.tag.tag--ghost', { text: '按重复项数排序' }),
        ]),
        el('div.stack', {}, report.candidate_lists.map(candidateCard)),
      ]),
    );
  } else {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, el('h2', { text: '候选列表区' })),
        el('div.empty-state', {}, [
          el('h3', { text: '没有识别到重复列表结构' }),
          el('p', {
            text: '这通常意味着数据由 JavaScript 异步渲染。建议改用网络抓包,找到返回 JSON 的接口后用 mode:"json" 规则提取。',
          }),
          el('button.btn.btn--sm.btn--primary', {
            text: '前往网络抓包',
            onclick: () => goToPage('requests'),
          }),
        ]),
      ]),
    );
  }

  // 元数据
  const meta = report.metadata || {};
  const metaKeys = Object.keys(meta).filter((k) => meta[k] && (Array.isArray(meta[k]) ? meta[k].length : true));
  if (metaKeys.length) {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, el('h2', { text: '结构化元数据' })),
        el(
          'div.meta-grid',
          {},
          metaKeys.map((key) =>
            el('div.meta-card', {}, [
              el('h4', { text: key }),
              el('pre.json', {
                style: { maxHeight: '200px' },
                text: truncate(JSON.stringify(meta[key], null, 2), 4000),
              }),
            ]),
          ),
        ),
      ]),
    );
  }

  // 简化 DOM 树
  if (report.simplified_tree) {
    children.push(
      el('div.card.glass', {}, [
        el('div.card__head', {}, [
          el('h2', { text: '简化 DOM 树' }),
          report.simplified_tree_truncated ? el('span.tag.tag--warning', { text: '已截断' }) : null,
        ]),
        el('details.tree', { open: false }, [
          el('summary', { text: '展开查看(可用来手写选择器)' }),
          // 逐行渲染 + 还原字面量转义: 早先直接把整段树塞进 <pre>, 于是
          // `body\n  div\n    span` 里的 \n 原样显示成两个字符, 一整棵树挤成一行;
          // 而且套了 JSON 着色器, 把标签里的引号/数字染成了莫名其妙的颜色。
          el('pre.json.plain.dom-tree', {
            style: { maxHeight: '440px' },
            html: renderPlainText(report.simplified_tree, { maxLineLength: 200 }),
          }),
        ]),
      ]),
    );
  }

  mount(out, children);
  flashScroll(out.firstElementChild);
}

/** 把结构报告里的最佳候选回填到抓取页的手写规则里 */
function handoffToCrawl(report) {
  const best = report.candidate_lists?.[0];
  if (!best) {
    toastInfo('没有可用的候选列表可以回填');
    return;
  }
  const rule = {
    mode: 'dom',
    list_rule: {
      item_selector: best.item_selector,
      fields: (best.sample_fields || []).map((f) => ({
        name: f.name,
        selector: f.selector,
        attribute: f.attribute || null,
        transform: f.transform || ['strip'],
      })),
    },
  };
  if (report.pagination?.next_selector) {
    rule.pagination = { next_selector: report.pagination.next_selector, max_pages: 3 };
  }

  const box = $('#ruleJson');
  if (box) box.value = JSON.stringify(rule, null, 2);
  const urlInput = $('#crawlUrl');
  if (urlInput && report.url) urlInput.value = report.url;

  toastSuccess('已把候选结构填入抓取页的规则编辑器');
  goToPage('crawl');
  // 切页后把规则区滚动到视野内
  setTimeout(() => {
    const modeBtn = document.querySelector('#ruleMode .segmented__item[data-mode="manual"]');
    modeBtn?.click();
    flashScroll($('#ruleJson'));
  }, 260);
}

/* ==========================================================================
   任务
   ========================================================================== */
/**
 * @param {object} [opts]
 * @param {number} [opts.deepScroll] 额外滚动轮次(用户在无限流提示里选择"继续"时传入)
 */
async function startAnalyze(opts = {}) {
  const url = $('#analyzeUrl').value.trim();
  if (!/^https?:\/\/.+/i.test(url)) {
    toastError('请填写以 http:// 或 https:// 开头的目标地址');
    return;
  }

  // 重新分析前先断开旧任务的订阅: 否则上一轮的结果事件可能在本轮渲染后才到达,
  // 把刚渲染出来的新报告覆盖成旧的。
  disconnectTask();
  activeTaskId = null;

  // 重新分析时先清掉上一轮的验证提示 —— 否则过期的"手动过验证"会一直贴在顶部,
  // 用户会以为新页面也被挡住了。
  const alertSlot = $('#analyzeAlertSlot');
  if (alertSlot) {
    mount(alertSlot, []);
    alertSlot.hidden = true;
  }

  const out = $('#analyzeOutput');
  mount(
    out,
    el('div.card.glass', {}, [
      el('div.card__head', {}, [el('h2', { text: '分析中…' }), el('span.tag[data-state=running]', { text: '进行中' })]),
      el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } }, [
        el('div.skeleton', { style: { width: '78%' } }),
        el('div.skeleton', { style: { width: '58%' } }),
        el('div.skeleton', { style: { width: '66%' } }),
      ]),
    ]),
  );

  const restore = withLoading($('#btnAnalyzeStart'), '分析中…');
  try {
    const { task } = await api.startAnalyze({
      url,
      deep_scroll: opts.deepScroll || 0,
    });
    activeTaskId = task.id;
    set('lastAnalyzeTaskId', task.id);
    // 记进共享 store: 让其他页面(以及本页刷新后)知道"最近一次任务"是分析任务。
    // 不记的话 store.task 会一直停留在上一个抓取任务上, 事件归属判断只能靠 id。
    set('task', task);
    connectTask(task.id);
  } catch (err) {
    restore();
    mount(out, el('div.alert.alert--error', {}, [el('div.alert__body', { text: err instanceof ApiError ? err.message : String(err) })]));
    toastError(err instanceof ApiError ? err.message : String(err), '分析失败');
    return;
  }
  // 结果事件到达后恢复按钮
  setTimeout(restore, 400);
}

function handleTaskEvent(event) {
  // 归属判断统一走 isOwnTaskEvent(以任务 id 为准)。曾经的写法只看 store.task.kind,
  // 而 store.task 可能来自别的页面 —— 先抓取再去分析时它是那个抓取任务, 结果分析的
  // 结果事件被直接丢掉, 界面永远停在"分析中…"。
  if (!isOwnTaskEvent(event, { activeTaskId, knownTask: get('task'), kind: 'analyze' })) return;
  if (event.type === 'result') {
    renderReport(event.payload);
    set('lastAnalyze', event.payload);
    // 兜底: 结果已到, 任务即已结束, 避免界面停留在"进行中"
    if (get('task.status') === 'running') set('task.status', 'success');
    const count = event.payload?.report?.candidate_lists?.length ?? 0;
    toastSuccess(`分析完成,识别到 ${count} 个候选列表`);
  } else if (event.type === 'status' && event.status === 'failed') {
    toastError(event.message || '分析失败');
    const out = $('#analyzeOutput');
    mount(
      out,
      el('div.alert.alert--error', {}, [
        el('div.alert__body', { text: event.message || '分析失败,请查看实时日志' }),
      ]),
    );
  } else if (event.type === 'snapshot' && event.task?.status === 'success' && event.task.result) {
    renderReport(event.task.result);
  }
}

export function init() {
  $('#btnAnalyzeStart')?.addEventListener('click', startAnalyze);
  $('#analyzeUrl')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      startAnalyze();
    }
  });
  onTaskEvent(handleTaskEvent);
}

export default { init };
