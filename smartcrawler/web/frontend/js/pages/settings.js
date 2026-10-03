/**
 * pages/settings.js —— 系统配置
 * ---------------------------------------------------------------------------
 * 面板完全由后端下发的 Schema 驱动(见 web/config_store.build_config_schema):
 * 后端往 Settings 里加一个字段,这里就会自动多出一个带校验的控件,前端无需改动。
 *
 * 交互约定:
 *   - 只有真正改动过的字段才会进入 patch;未动过的字段不会写进 .env;
 *   - API Key 以掩码回显,前端不回传掩码值(后端也会再拦一次);
 *   - 改动就地校验(数值范围、JSON 语法),错误在字段下方提示,不阻塞其他字段;
 *   - 保存前展示改动清单,保存后按后端返回决定是否提示"需重启浏览器会话"。
 */

import { $, $$, el, mount, debounce, withLoading, formatBytes } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, jsonBlock } from '../ui/modal.js';

/** 本地待提交的改动 { path: rawValue } */
let dirty = {};
/** 原始取值(用于判断是否真的改了) */
let originals = {};
/** 后端 Schema */
let schema = null;
let sources = {};

/* ==========================================================================
   控件构造
   ========================================================================== */
const ICONS = {
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18Z"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  sparkles: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  layers: '<path d="M12 2 3 7l9 5 9-5-9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/>',
  plug: '<path d="M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0V8ZM12 17v5"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.65 1.65 0 0 0 15 19.4a1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6 1.65 1.65 0 0 0 10 3.09V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/>',
};

function svgIcon(paths) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.innerHTML = paths;
  return svg;
}

/** 生成本地校验提示节点(无错误时隐藏) */
function hintNode() {
  return el('p.hint', { style: { color: 'var(--danger)' }, hidden: true });
}

/** 根据 Schema 描述构造一个控件 */
function buildControl(field, hint) {
  const raw = originals[field.path];
  const value = raw === undefined || raw === null ? '' : raw;

  const markDirty = (next) => {
    if (next === originals[field.path]) delete dirty[field.path];
    else dirty[field.path] = next;
    updateDirtyUI();
    const row = document.querySelector(`[data-config-path="${field.path}"]`);
    row?.classList.toggle('is-changed', Object.prototype.hasOwnProperty.call(dirty, field.path));
  };

  if (field.type === 'bool') {
    const input = el('input', { type: 'checkbox', checked: Boolean(value) });
    const label = el('label.switch', {}, [
      input,
      el('span.switch__track', {}, el('span.switch__thumb')),
      el('span.switch__text', { text: value ? '开启' : '关闭' }),
    ]);
    input.addEventListener('change', () => {
      $('.switch__text', label).textContent = input.checked ? '开启' : '关闭';
      markDirty(input.checked);
    });
    return label;
  }

  if (field.type === 'enum') {
    const select = el('select');
    for (const option of field.options || []) {
      select.appendChild(el('option', { value: option, text: String(option), selected: option === value }));
    }
    select.addEventListener('change', () => markDirty(select.value));
    return select;
  }

  if (field.type === 'textarea' || field.type === 'list' || field.type === 'json') {
    const text =
      field.type === 'list' && Array.isArray(value)
        ? value.join(', ')
        : typeof value === 'object'
          ? JSON.stringify(value, null, 2)
          : String(value ?? '');
    const isCode = field.type === 'json';
    const area = el('textarea', {
      class: isCode ? 'code' : '',
      rows: field.type === 'list' ? 2 : 4,
      spellcheck: 'false',
      placeholder: field.type === 'list' ? '逗号分隔,例如 http://user:pass@host:port, socks5://host:port' : '',
    });
    area.value = text;

    const validate = debounce(() => {
      const raw2 = area.value.trim();
      let error = '';
      if (field.type === 'json' && raw2) {
        try {
          JSON.parse(raw2);
        } catch (err) {
          error = `JSON 语法错误: ${err.message}`;
        }
      }
      if (field.type === 'list' && raw2 && !raw2.includes(',') && !raw2.includes('[') && !raw2.includes('\n')) {
        // 单个元素也是合法的,只是提醒
        error = '';
      }
      hint.textContent = error;
      hint.hidden = !error;
      area.classList.toggle('is-invalid', Boolean(error));
      if (!error) markDirty(raw2);
    }, 320);

    area.addEventListener('input', validate);
    return area;
  }

  // str / int / float
  const isNumber = field.type === 'int' || field.type === 'float';
  const input = el('input', {
    type: isNumber ? 'number' : field.sensitive ? 'password' : 'text',
    value: field.sensitive ? '' : String(value ?? ''),
    placeholder: field.sensitive
      ? value
        ? '已配置(留空表示不修改)'
        : '尚未配置'
      : '',
    step: field.type === 'float' ? '0.1' : isNumber ? '1' : undefined,
    min: field.min,
    max: field.max,
    spellcheck: 'false',
    autocomplete: field.sensitive ? 'new-password' : 'off',
  });
  if (field.sensitive) input.value = '';

  const validate = () => {
    const text = input.value;
    let error = '';
    if (isNumber && text !== '') {
      const num = Number(text);
      if (!Number.isFinite(num)) error = '需要一个数字';
      else if (field.min !== undefined && num < field.min) error = `不能小于 ${field.min}`;
      else if (field.max !== undefined && num > field.max) error = `不能大于 ${field.max}`;
    }
    hint.textContent = error;
    hint.hidden = !error;
    input.classList.toggle('is-invalid', Boolean(error));
    if (!error) {
      if (field.sensitive && text === '') {
        // 留空 = 不改动密钥
        delete dirty[field.path];
        updateDirtyUI();
        return;
      }
      markDirty(field.type === 'bool' ? input.checked : text);
    }
  };
  input.addEventListener('input', debounce(validate, 320));
  return input;
}

/* ==========================================================================
   渲染
   ========================================================================== */
function configRow(field) {
  const hint = hintNode();
  const control = buildControl(field, hint);

  const labelChildren = [el('span', { text: field.label || field.name })];
  if (sources[field.path] && sources[field.path] !== 'default') {
    labelChildren.push(el('span.badge-src', { dataset: { src: sources[field.path] }, text: sources[field.path] }));
  }
  if (field.restart_required) {
    labelChildren.push(el('span.badge-src', { title: '修改后下次任务会重建浏览器会话', text: '需重启会话' }));
  }

  const meta = [
    el('div.config-row__label', {}, labelChildren),
    el('div.config-row__key', { text: field.path }),
  ];
  if (field.description) meta.push(el('div.config-row__desc', { text: field.description }));

  const controlChildren = [control];
  if (field.default !== undefined && field.type !== 'bool') {
    controlChildren.push(
      el('span.config-row__desc', {
        text: `默认: ${typeof field.default === 'object' ? JSON.stringify(field.default) : field.default}`,
      }),
    );
  }
  controlChildren.push(hint);

  return el(
    'div.config-row',
    { dataset: { configPath: field.path, search: `${field.label} ${field.path} ${field.description || ''}`.toLowerCase() } },
    [el('div.config-row__meta', {}, meta), el('div.config-row__control', {}, controlChildren)],
  );
}

function sectionCard(section) {
  return el('div.card.glass.config-section', { dataset: { section: section.key } }, [
    el('div.section-head', {}, [
      el('div.section-head__icon', {}, svgIcon(ICONS[section.icon] || ICONS.settings)),
      el('div.section-head__text', {}, [
        el('h2', { text: section.title }),
        el('p', { text: section.description || '' }),
      ]),
    ]),
    el('div.config-rows', {}, section.fields.map(configRow)),
  ]);
}

function renderSchema() {
  const container = $('#configSections');
  if (!container || !schema) return;
  mount(container, schema.sections.map(sectionCard));
  applyFilter($('#configSearch')?.value || '');
}

function applyFilter(query) {
  const q = String(query || '').trim().toLowerCase();
  for (const row of $$('.config-row')) {
    row.classList.toggle('is-filtered', Boolean(q) && !row.dataset.search.includes(q));
  }
  // 整组都被过滤时把分组也隐藏,避免出现空卡片
  for (const section of $$('.config-section')) {
    const visible = $$('.config-row:not(.is-filtered)', section).length;
    section.hidden = visible === 0;
  }
}

/* ==========================================================================
   脏状态
   ========================================================================== */
function updateDirtyUI() {
  const count = Object.keys(dirty).length;
  const tag = $('#configDirtyTag');
  if (tag) {
    tag.hidden = count === 0;
    tag.textContent = `${count} 项未保存`;
  }
  const saveBtn = $('#btnConfigSave');
  if (saveBtn) saveBtn.disabled = count === 0;
}

/* ==========================================================================
   加载 / 保存
   ========================================================================== */
export async function loadConfig() {
  const container = $('#configSections');
  mount(container, [
    el('div.card.glass', {}, [
      el('div.skeleton', { style: { height: '26px', width: '30%' } }),
      el('div.skeleton', { style: { height: '18px' } }),
      el('div.skeleton', { style: { height: '18px', width: '80%' } }),
      el('div.skeleton', { style: { height: '18px', width: '62%' } }),
    ]),
  ]);

  try {
    const data = await api.getConfig();
    schema = data.schema;
    originals = { ...data.values };
    sources = data.sources || {};
    dirty = {};
    renderSchema();
    updateDirtyUI();

    if (data.shadowed?.length) {
      const note = el('div.alert.alert--warn', {}, [
        el('div.alert__body', {}, [
          el('div.alert__title', { text: '以下配置被系统环境变量锁定' }),
          el('div', {
            text: `${data.shadowed.join(', ')} —— 真实环境变量优先级高于 .env,这里的修改不会生效。请修改对应的环境变量后重启服务。`,
          }),
        ]),
      ]);
      $('#configSections')?.prepend(note);
    }
  } catch (err) {
    mount(container, el('div.alert.alert--error', {}, [el('div.alert__body', { text: err instanceof ApiError ? err.message : String(err) })]));
  }
}

async function saveConfig() {
  const count = Object.keys(dirty).length;
  if (!count) {
    toastInfo('没有需要保存的改动');
    return;
  }

  const restore = withLoading($('#btnConfigSave'), '保存中…');
  try {
    const res = await api.patchConfig(dirty, true);
    dirty = {};
    updateDirtyUI();
    await loadConfig();
    if (res.restart_required) {
      toastSuccess(`已保存 ${res.changed.length} 项;浏览器会话将在下次任务时重建`, '配置已更新');
    } else {
      toastSuccess(`已保存 ${res.changed.length} 项配置`);
    }
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err), '保存失败');
  } finally {
    restore();
  }
}

/* ==========================================================================
   自检工具
   ========================================================================== */
function toolOutput(title, content) {
  const box = $('#toolOutput');
  if (!box) return;
  box.hidden = false;
  mount(box, [
    el('div', { style: { marginBottom: '6px', fontWeight: '600', color: 'var(--text-1)' }, text: title }),
    content,
  ]);
}

async function runTool(button, label, fn) {
  const restore = withLoading(button, label);
  try {
    await fn();
  } catch (err) {
    toolOutput('执行失败', el('div', { text: err instanceof ApiError ? err.message : String(err) }));
    toastError(err instanceof ApiError ? err.message : String(err));
  } finally {
    restore();
  }
}

function initTools() {
  $('#btnTestAi')?.addEventListener('click', (event) =>
    runTool(event.currentTarget, '测试中…', async () => {
      const res = await api.testAi();
      toolOutput(
        res.ok ? '✓ AI 连通正常' : '✗ AI 不可用',
        el('pre', { style: { margin: '0', whiteSpace: 'pre-wrap' }, text: JSON.stringify(res, null, 2) }),
      );
      (res.ok ? toastSuccess : toastError)(res.message);
    }),
  );

  $('#btnTestProxy')?.addEventListener('click', (event) =>
    runTool(event.currentTarget, '探测中…', async () => {
      const res = await api.testProxies();
      toolOutput(
        res.message,
        el('pre', { style: { margin: '0', whiteSpace: 'pre-wrap' }, text: JSON.stringify(res.results, null, 2) }),
      );
      (res.ok ? toastSuccess : toastError)(res.message);
    }),
  );

  $('#btnClearCache')?.addEventListener('click', (event) =>
    runTool(event.currentTarget, '清理中…', async () => {
      const res = await api.clearCache();
      toolOutput('AI 缓存已处理', el('div', { text: `删除文件: ${res.removed ? '是' : '否(本就不存在)'} · 释放 ${formatBytes(res.bytes)} · ${res.path}` }));
      toastSuccess('AI 缓存已清空');
    }),
  );

  $('#btnResetState')?.addEventListener('click', (event) =>
    runTool(event.currentTarget, '重置中…', async () => {
      const res = await api.resetState();
      toolOutput('增量状态已重置', el('div', { text: `清除历史哈希 ${res.cleared} 条 · ${res.path}` }));
      toastSuccess(`已清除 ${res.cleared} 条历史哈希,下次抓取视为全量`);
    }),
  );
}

/* ==========================================================================
   初始化
   ========================================================================== */
let initialized = false;

export function init() {
  if (initialized) {
    // 二次进入只需刷新一次取值,避免重复绑定事件
    loadConfig();
    return;
  }
  initialized = true;

  $('#configSearch')?.addEventListener('input', debounce((event) => applyFilter(event.target.value), 160));

  $('#btnConfigSave')?.addEventListener('click', saveConfig);

  $('#btnConfigReload')?.addEventListener('click', async (event) => {
    if (Object.keys(dirty).length && !window.confirm('有未保存的改动,重载会丢弃它们。继续?')) return;
    const restore = withLoading(event.currentTarget, '重载中…');
    try {
      await api.reloadConfig();
      await loadConfig();
      toastSuccess('已从 .env 重新加载配置');
    } catch (err) {
      toastError(err instanceof ApiError ? err.message : String(err));
    } finally {
      restore();
    }
  });

  // 离开页面前提醒未保存改动
  window.addEventListener('beforeunload', (event) => {
    if (Object.keys(dirty).length) {
      event.preventDefault();
      event.returnValue = '';
    }
  });

  initTools();
  loadConfig();
}

export default { init, loadConfig };
