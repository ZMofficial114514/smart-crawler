/**
 * pages/plugins.js —— 插件管理
 * ---------------------------------------------------------------------------
 * 两条扩展路径在这里合流:
 *   - 内置/用户 Python 插件: 开关 + 按 config_schema 自动生成的配置表单;
 *   - 声明式插件("声明式扩展"): 同样通过配置表单完成, 用户无需写代码。
 *
 * 配置表单与「系统配置」页共用同一套控件约定(bool/int/float/str/textarea/list/json/enum),
 * 因此后端往插件的 config_schema 里加一个字段, 界面就会自动多出控件。
 *
 * 安全: 用户插件是本机代码执行。页面顶部常驻警示, 不试图营造"插件是沙箱"的错觉。
 */

import { $, $$, el, mount, debounce, withLoading, truncate, formatBytes } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get, set } from '../store.js';
import { toastError, toastSuccess, toastInfo } from '../ui/toast.js';
import { openModal, closeModal, jsonBlock } from '../ui/modal.js';
import { goToPage } from '../router.js';

const CATEGORY_LABEL = {
  download: '下载',
  'anti-bot': '反爬',
  cleanup: '清洗',
  storage: '存储',
  other: '其它',
};

const CATEGORY_VARIANT = {
  download: 'info',
  'anti-bot': 'warning',
  cleanup: 'success',
  storage: 'muted',
  other: 'ghost',
};

let plugins = [];
let stats = {};
let notice = '';
let query = '';

/* ==========================================================================
   渲染
   ========================================================================== */
function statCard(label, value, hint = '') {
  return el('div.stat', { title: hint }, [
    el('span.stat__label', { text: label }),
    el('span.stat__value', { text: String(value) }),
  ]);
}

function renderStats() {
  const box = $('#pluginStats');
  if (!box) return;
  mount(box, [
    statCard('插件总数', stats.total ?? 0),
    statCard('已启用', stats.enabled ?? 0),
    statCard('用户插件', stats.user ?? 0, '放在 plugins/ 目录下的自定义插件'),
    statCard('加载失败', stats.broken ?? 0, '文件存在但导入出错'),
  ]);
}

function renderNotice() {
  const box = $('#pluginNotice');
  if (!box) return;
  mount(
    box,
    el('div.alert.alert--warn', {}, [
      el('div.alert__body', {}, [
        el('div.alert__title', { text: '安全提示' }),
        el('div', { text: notice || '用户插件等同于在本机运行的 Python 代码。' }),
        el('div.hint', {
          style: { marginTop: '6px' },
          text: `插件目录: ${stats.user_dir || 'plugins/'} · 配置: ${stats.config_path || 'data/plugins.json'} · 下载产物: ${stats.output_dir || 'data/plugin_output/'}`,
        }),
      ]),
    ]),
  );
}

function fieldControl(field, current, onChange) {
  const hint = el('p.hint', { style: { color: 'var(--danger)' }, hidden: true });

  const mark = (value) => {
    hint.hidden = true;
    onChange(field.key, value);
  };

  let control;
  if (field.type === 'bool') {
    const input = el('input', { type: 'checkbox', checked: Boolean(current) });
    const label = el('label.switch', {}, [
      input,
      el('span.switch__track', {}, el('span.switch__thumb')),
      el('span.switch__text', { text: current ? '开启' : '关闭' }),
    ]);
    input.addEventListener('change', () => {
      $('.switch__text', label).textContent = input.checked ? '开启' : '关闭';
      mark(input.checked);
    });
    control = label;
  } else if (field.type === 'enum') {
    const select = el('select');
    for (const option of field.options || []) {
      select.appendChild(el('option', { value: option, text: String(option), selected: String(option) === String(current) }));
    }
    select.addEventListener('change', () => mark(select.value));
    control = select;
  } else if (field.type === 'textarea' || field.type === 'list' || field.type === 'json') {
    const text =
      field.type === 'list' && Array.isArray(current)
        ? current.join(', ')
        : typeof current === 'object'
          ? JSON.stringify(current, null, 2)
          : String(current ?? '');
    const area = el('textarea', { class: field.type === 'json' ? 'code' : '', rows: 3, spellcheck: 'false' });
    area.value = text;
    area.addEventListener(
      'input',
      debounce(() => {
        if (field.type === 'json' && area.value.trim()) {
          try {
            JSON.parse(area.value);
          } catch (err) {
            hint.textContent = `JSON 语法错误: ${err.message}`;
            hint.hidden = false;
            area.classList.add('is-invalid');
            return;
          }
        }
        area.classList.remove('is-invalid');
        mark(area.value);
      }, 320),
    );
    control = area;
  } else if (field.type === 'int' || field.type === 'float') {
    const input = el('input', {
      type: 'number',
      value: String(current ?? ''),
      step: field.type === 'float' ? '0.1' : '1',
      min: field.min,
      max: field.max,
    });
    input.addEventListener(
      'input',
      debounce(() => {
        const num = Number(input.value);
        let error = '';
        if (input.value !== '' && !Number.isFinite(num)) error = '需要一个数字';
        else if (field.min !== undefined && input.value !== '' && num < field.min) error = `不能小于 ${field.min}`;
        else if (field.max !== undefined && input.value !== '' && num > field.max) error = `不能大于 ${field.max}`;
        hint.textContent = error;
        hint.hidden = !error;
        input.classList.toggle('is-invalid', Boolean(error));
        if (!error) mark(input.value);
      }, 320),
    );
    control = input;
  } else {
    const input = el('input', { type: 'text', value: String(current ?? ''), spellcheck: 'false' });
    input.addEventListener('input', debounce(() => mark(input.value), 300));
    control = input;
  }

  return el('div.field', {}, [
    el('label', {}, [
      field.label,
      field.description ? el('span.hint', { style: { marginLeft: '6px', fontWeight: '400' }, text: field.description }) : null,
    ]),
    control,
    hint,
  ]);
}

function pluginCard(info) {
  const node = el(
    'div.card.glass.plugin-card',
    { dataset: { id: info.id, search: `${info.name} ${info.description} ${info.id} ${info.tags.join(' ')}`.toLowerCase() } },
    [
      el('div.plugin-card__head', {}, [
        el('div.plugin-card__title', {}, [
          el('strong', { text: info.name }),
          el('span.tag', { class: `tag--${CATEGORY_VARIANT[info.category] || 'ghost'}`, text: CATEGORY_LABEL[info.category] || info.category }),
          info.source === 'user' ? el('span.tag.tag--warning', { text: '用户插件' }) : el('span.tag.tag--ghost', { text: '内置' }),
          info.default_enabled ? el('span.tag.tag--success', { text: '默认启用' }) : null,
        ]),
        el('label.switch', { title: info.enabled ? '点击停用' : '点击启用' }, [
          (() => {
            const input = el('input', { type: 'checkbox', checked: info.enabled });
            input.addEventListener('change', () => togglePlugin(info, input));
            return input;
          })(),
          el('span.switch__track', {}, el('span.switch__thumb')),
          el('span.switch__text', { text: '' }),
        ]),
      ]),
      el('p.plugin-card__desc', { text: info.description || '(无说明)' }),
      el('div.plugin-card__meta', {}, [
        el('span', { text: `v${info.version}` }),
        info.author ? el('span', { text: info.author }) : null,
        el('span', { text: `钩子: ${info.hooks.join(', ') || '无'}` }),
        info.requires?.length ? el('span', { text: `依赖: ${info.requires.join(', ')}` }) : null,
      ]),
      info.tags?.length ? el('div.field-chips', {}, info.tags.map((t) => el('span.field-chip', { text: t }))) : null,
      info.load_error
        ? el('div.alert.alert--error', {}, [el('div.alert__body', { text: `加载失败: ${info.load_error}` })])
        : null,
      info.last_error
        ? el('div.alert.alert--warn', {}, [el('div.alert__body', { text: `最近一次运行出错: ${info.last_error}` })])
        : null,
      el('div.plugin-card__foot', {}, [
        info.config_schema?.length
          ? el('button.btn.btn--sm.btn--ghost', { text: '配置', onclick: () => openConfig(info) })
          : el('span.hint', { text: '该插件没有可配置项' }),
        info.source === 'user' && info.file
          ? el('button.btn.btn--sm.btn--ghost', { text: '查看源码', onclick: () => viewSource(info) })
          : null,
        info.source === 'user' && info.file
          ? el('button.btn.btn--sm.btn--ghost', { text: '删除', onclick: () => removePlugin(info) })
          : null,
      ]),
    ],
  );
  return node;
}

function renderList() {
  const list = $('#pluginList');
  if (!list) return;

  if (!plugins.length) {
    mount(
      list,
      el('div.empty-state', {}, [
        el('div.empty-state__icon', { html: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 2v6M12 16v6M4.9 4.9l4.2 4.2M14.9 14.9l4.2 4.2M2 12h6M16 12h6M4.9 19.1l4.2-4.2M14.9 9.1l4.2-4.2"/></svg>' }),
        el('h3', { text: '还没有发现插件' }),
        el('p', { text: '内置插件应随框架分发。若为空, 请检查 smartcrawler/plugins/builtin/ 是否存在。' }),
      ]),
    );
    return;
  }

  const filtered = plugins.filter((p) => {
    if (!query) return true;
    const haystack = `${p.name} ${p.description} ${p.id} ${(p.tags || []).join(' ')}`.toLowerCase();
    return haystack.includes(query);
  });

  if (!filtered.length) {
    mount(list, el('p.empty', { text: `没有匹配「${query}」的插件` }));
    return;
  }

  const groups = new Map();
  for (const info of filtered) {
    if (!groups.has(info.category)) groups.set(info.category, []);
    groups.get(info.category).push(info);
  }

  const children = [];
  for (const [category, items] of groups) {
    children.push(
      el('div.plugin-group', {}, [
        el('div.plugin-group__head', {}, [
          el('h3', { text: CATEGORY_LABEL[category] || category }),
          el('span.tag.tag--ghost', { text: `${items.length} 个` }),
        ]),
        el('div.grid.grid--plugins', {}, items.map(pluginCard)),
      ]),
    );
  }
  mount(list, children);
}

/* ==========================================================================
   操作
   ========================================================================== */
async function togglePlugin(info, input) {
  const next = input.checked;
  input.disabled = true;
  try {
    const res = await api.togglePlugin(info.id, next);
    stats = res.stats;
    const index = plugins.findIndex((p) => p.id === info.id);
    if (index >= 0) plugins[index] = res.plugin;
    renderStats();
    renderList();
    toastSuccess(`${info.name} 已${next ? '启用' : '停用'}`);
  } catch (err) {
    input.checked = !next; // 回滚
    toastError(err instanceof ApiError ? err.message : String(err));
  } finally {
    input.disabled = false;
  }
}

function openConfig(info) {
  const pending = {};
  const fields = (info.config_schema || []).map((field) =>
    fieldControl(field, info.config?.[field.key] ?? field.default, (key, value) => {
      pending[key] = value;
    }),
  );

  const body = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } }, [
    el('dl.kv', {}, [
      el('dt', { text: '插件 id' }),
      el('dd', { text: info.id }),
      el('dt', { text: '来源' }),
      el('dd', { text: info.source === 'user' ? `用户插件 ${truncate(info.file, 60)}` : '框架内置' }),
      el('dt', { text: '钩子' }),
      el('dd', { text: info.hooks.join(', ') || '无' }),
    ]),
    el('p.hint', { text: '留空表示不改动该字段; 保存后立即生效, 无需重启。' }),
    ...fields,
  ]);

  const footer = [
    el('button.btn.btn--sm.btn--ghost', {
      text: '恢复默认值',
      onclick: async (event) => {
        const restore = withLoading(event.currentTarget, '恢复中…');
        try {
          const res = await api.resetPluginConfig(info.id);
          Object.assign(info, res.plugin);
          closeModal();
          renderList();
          toastSuccess(`${info.name} 已恢复默认配置`);
        } catch (err) {
          toastError(err instanceof ApiError ? err.message : String(err));
        } finally {
          restore();
        }
      },
    }),
    el('button.btn.btn--sm.btn--primary', {
      text: '保存配置',
      onclick: async (event) => {
        if (!Object.keys(pending).length) {
          toastInfo('没有改动');
          return;
        }
        const restore = withLoading(event.currentTarget, '保存中…');
        try {
          const res = await api.updatePluginConfig(info.id, pending);
          Object.assign(info, res.plugin);
          closeModal();
          renderList();
          toastSuccess(`已保存 ${Object.keys(pending).length} 项配置`);
        } catch (err) {
          toastError(err instanceof ApiError ? err.message : String(err));
        } finally {
          restore();
        }
      },
    }),
  ];

  openModal({ title: `配置插件 · ${info.name}`, body, footer });
}

async function viewSource(info) {
  try {
    const data = await api.pluginSource(info.file);
    openModal({
      title: `${info.name} · 源码 (${data.lines} 行)`,
      body: el('pre.json', { style: { maxHeight: '68vh' }, text: data.content }),
      footer: [
        el('span.hint', { text: data.path }),
        el('button.btn.btn--sm.btn--ghost', {
          text: '重新扫描',
          onclick: async () => {
            closeModal();
            await refreshPlugins();
          },
        }),
      ],
    });
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

async function removePlugin(info) {
  if (!window.confirm(`确定删除用户插件 ${info.name}?\n文件: ${info.file}\n此操作不可撤销。`)) return;
  try {
    await api.deletePlugin(info.file);
    toastSuccess(`已删除 ${info.name}`);
    await refreshPlugins();
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
  }
}

async function refreshPlugins() {
  try {
    const res = await api.refreshPlugins();
    plugins = res.plugins || [];
    stats = res.stats || {};
    renderStats();
    renderList();
    set('plugins', plugins);
    return true;
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err));
    return false;
  }
}

/* ==========================================================================
   新建插件
   ========================================================================== */
async function openCreateDialog() {
  let template = '';
  try {
    const res = await api.pluginTemplate();
    template = res.template || '';
  } catch {
    /* 模板接口失败也能继续, 只是看不到预览 */
  }

  const filename = el('input', { type: 'text', placeholder: 'my_plugin.py', spellcheck: 'false' });
  const name = el('input', { type: 'text', placeholder: '我的插件' });
  const description = el('input', { type: 'text', placeholder: '一句话说明这个插件做什么' });

  const body = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } }, [
    el('div.field', {}, [el('label', { text: '文件名' }), filename, el('p.hint', { text: '只能包含字母、数字与下划线, 会保存到 plugins/ 目录' })]),
    el('div.field', {}, [el('label', { text: '插件显示名' }), name]),
    el('div.field', {}, [el('label', { text: '说明' }), description]),
    template
      ? el('details.accordion', {}, [
          el('summary', { text: '预览将生成的代码骨架' }),
          el('div.accordion__body', {}, el('pre.json', { style: { maxHeight: '300px' }, text: template })),
        ])
      : null,
    el('div.alert.alert--info', {}, [
      el('div.alert__body', {
        text: '创建后可以用任意编辑器打开该文件继续编写。写完后回到本页点「重新扫描」即可启用。',
      }),
    ]),
  ]);

  const footer = [
    el('button.btn.btn--sm.btn--primary', {
      text: '创建',
      onclick: async (event) => {
        if (!filename.value.trim()) {
          toastError('请填写文件名');
          return;
        }
        const restore = withLoading(event.currentTarget, '创建中…');
        try {
          const res = await api.createPlugin({
            filename: filename.value.trim(),
            name: name.value.trim() || '我的插件',
            description: description.value.trim(),
          });
          closeModal();
          toastSuccess(res.message || '已创建');
          await refreshPlugins();
        } catch (err) {
          toastError(err instanceof ApiError ? err.message : String(err));
        } finally {
          restore();
        }
      },
    }),
  ];

  openModal({ title: '新建用户插件', body, footer });
}

/* ==========================================================================
   初始化
   ========================================================================== */
let initialized = false;

export async function refresh() {
  try {
    const res = await api.listPlugins();
    plugins = res.plugins || [];
    stats = res.stats || {};
    notice = res.notice || '';
    renderStats();
    renderNotice();
    renderList();
    set('plugins', plugins);
  } catch (err) {
    const list = $('#pluginList');
    if (list) {
      mount(list, el('div.alert.alert--error', {}, [el('div.alert__body', { text: err instanceof ApiError ? err.message : String(err) })]));
    }
  }
}

export function init() {
  if (initialized) {
    refresh();
    return;
  }
  initialized = true;

  $('#btnPluginRefresh')?.addEventListener('click', async (event) => {
    const restore = withLoading(event.currentTarget, '扫描中…');
    const ok = await refreshPlugins();
    restore();
    if (ok) toastSuccess(`已重新扫描插件目录, 共 ${plugins.length} 个插件`);
  });

  $('#btnPluginCreate')?.addEventListener('click', openCreateDialog);

  $('#pluginSearch')?.addEventListener(
    'input',
    debounce((event) => {
      query = event.target.value.trim().toLowerCase();
      renderList();
    }, 160),
  );

  $('#btnPluginOpenDir')?.addEventListener('click', () => {
    openModal({
      title: '插件目录说明',
      body: el('div', { style: { display: 'flex', flexDirection: 'column', gap: '12px' } }, [
        el('p', { text: '把任意 .py 文件放进下面的目录, 框架会在启动时自动发现, 或在本页点「重新扫描」立即加载。' }),
        el('pre.json', { text: `用户插件目录: ${stats.user_dir || 'plugins/'}\n配置文件: ${stats.config_path || 'data/plugins.json'}\n下载产物: ${stats.output_dir || 'data/plugin_output/'}` }),
        el('p.hint', { text: '参考模板: plugins/example_enrich.py, 或点「新建插件」由界面生成骨架。' }),
        el('div.alert.alert--warn', {}, [el('div.alert__body', { text: notice })]),
      ]),
    });
  });

  refresh();
}

export default { init, refresh };
