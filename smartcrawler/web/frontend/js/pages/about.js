/**
 * pages/about.js —— 关于与帮助
 * ---------------------------------------------------------------------------
 * 内容全部来自后端 /api/usage 与 /api/health,因此文档与实现不会漂移:
 * 新增一个能力端点,这里改后端常量即可。
 */

import { $, el, mount, formatBytes } from '../utils.js';
import api, { ApiError } from '../api.js';
import { get } from '../store.js';
import { toastError } from '../ui/toast.js';
import { openModal, jsonBlock } from '../ui/modal.js';

function renderCapabilities(capabilities) {
  const list = $('#capList');
  if (!list) return;
  mount(
    list,
    capabilities.map((cap, index) =>
      el('div.cap', { style: { animationDelay: `${index * 50}ms` } }, [
        el('div.cap__idx', { text: String(index + 1) }),
        el('div.cap__body', {}, [
          el('div.cap__title', { text: cap.title }),
          el('div.cap__detail', { text: cap.detail }),
          el('div.cap__endpoint', {}, el('span.code-inline', { text: cap.endpoint })),
        ]),
      ]),
    ),
  );
}

function renderEnv(health) {
  const box = $('#envInfo');
  if (!box || !health) return;
  const rows = [
    ['版本', health.version],
    ['项目根目录', health.paths?.project_root],
    ['配置文件', health.paths?.env_file],
    ['输出目录', health.paths?.output_dir],
    ['浏览器', `${health.browser?.engine} · ${health.browser?.headless ? '无头' : '有头'}`],
    ['并发页面', String(health.browser?.max_pages ?? '—')],
    ['内核状态', health.browser?.ready],
    ['AI 服务商', `${health.ai?.provider} · ${health.ai?.model}`],
    ['AI 接口', health.ai?.base_url],
    ['API Key', health.ai?.has_key ? health.ai?.api_key_masked : '未配置'],
    ['响应缓存', health.ai?.cache_enabled ? '启用' : '关闭'],
    ['robots.txt', health.compliance?.respect_robots ? '遵守(推荐)' : '已关闭'],
    ['限速区间', `${(health.compliance?.delay_range || []).join(' ~ ')} 秒`],
    ['代理池', `${health.compliance?.proxies?.count ?? 0} 个`],
    ['最大翻页 / 条数', `${health.max_depth} 页 / ${health.max_items} 条`],
  ];
  mount(
    box,
    rows.flatMap(([key, value]) => [el('dt', { text: key }), el('dd', { text: String(value ?? '—') })]),
  );
}

function renderCli(cli) {
  const box = $('#cliList');
  if (box) box.textContent = (cli || []).join('\n');
}

export async function init() {
  try {
    const [usage, health] = await Promise.all([api.usage(), api.health()]);
    renderCapabilities(usage.capabilities || []);
    renderCli(usage.cli || []);
    const notice = $('#noticeText');
    if (notice) notice.textContent = usage.notice || '';
    renderEnv(health);
  } catch (err) {
    toastError(err instanceof ApiError ? err.message : String(err), '信息加载失败');
  }
}

export default { init };
