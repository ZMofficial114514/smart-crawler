/**
 * main.js —— 应用引导
 * ---------------------------------------------------------------------------
 * 启动顺序:
 *   1. 恢复界面偏好(折叠态/日志级别/主题),避免首帧闪动;
 *   2. 初始化弹层、日志面板、路由;
 *   3. 拉一次健康状态填顶栏;之后交给 /ws/health 低频推送;
 *   4. 绑定全局快捷键与一些交互细节(按钮光晕跟随鼠标、卡片错峰)。
 */

import { $, $$, debounce } from './utils.js';
import api from './api.js';
import { get, set, loadUiPreferences, persistUiPreferences, subscribe } from './store.js';
import { initRouter, goToPage } from './router.js';
import { initModal, closeModal, isModalOpen } from './ui/modal.js';
import { initLogPanel, toggleLogDrawer, closeLogDrawer } from './ui/logpanel.js';
import { toastError, toastSuccess, toastInfo } from './ui/toast.js';
import { connectHealth } from './ws.js';

/* ==========================================================================
   顶栏状态
   ========================================================================== */
function renderHealth(health) {
  if (!health) return;
  set('health', health);

  const ai = health.ai || {};
  const chipAi = $('#chipAi');
  if (chipAi) {
    const available = ai.available;
    chipAi.dataset.state = available ? 'ok' : ai.offline ? 'warn' : 'off';
    $('#chipAiValue').textContent = available ? ai.model || '就绪' : ai.offline ? '离线模式' : '未配置 Key';
    chipAi.dataset.tip = available
      ? `AI 已就绪: ${ai.provider} / ${ai.model}\n接口: ${ai.base_url}`
      : 'AI 不可用 —— 框架会自动退化为规则引擎,点击前往配置';
  }

  const chipBrowser = $('#chipBrowser');
  if (chipBrowser) {
    chipBrowser.dataset.state = 'ok';
    $('#chipBrowserValue').textContent = `${health.browser?.engine || '—'}${health.browser?.headless ? ' · 无头' : ''}`;
    chipBrowser.dataset.tip = health.browser?.ready || '';
  }

  const chipRobots = $('#chipRobots');
  if (chipRobots) {
    const on = health.compliance?.respect_robots;
    chipRobots.dataset.state = on ? 'ok' : 'warn';
    $('#chipRobotsValue').textContent = on ? '遵守中' : '已关闭';
    chipRobots.dataset.tip = on
      ? '正在遵守 robots.txt,并且默认限速 1~3 秒'
      : 'robots.txt 合规检查已关闭 —— 请确认你拥有采集授权';
  }

  const chipPlugins = $('#chipPlugins');
  if (chipPlugins) {
    const plugins = health.plugins || {};
    const enabled = plugins.enabled ?? 0;
    chipPlugins.dataset.state = plugins.ready === false ? 'off' : enabled > 0 ? 'ok' : 'warn';
    $('#chipPluginsValue').textContent = plugins.ready === false ? '不可用' : `${enabled} / ${plugins.total ?? 0}`;
    chipPlugins.dataset.tip = [
      `已启用 ${enabled} 个, 共 ${plugins.total ?? 0} 个插件`,
      plugins.user ? `其中用户插件 ${plugins.user} 个` : '',
      plugins.broken ? `⚠ ${plugins.broken} 个插件加载失败` : '',
      '点击进入插件管理',
    ]
      .filter(Boolean)
      .join('\n');
  }

  const compliance = $('#complianceText');
  if (compliance) {
    const parents = compliance.closest('.compliance');
    if (parents) parents.dataset.state = health.compliance?.respect_robots ? 'on' : 'off';
    compliance.textContent = health.compliance?.respect_robots
      ? `robots.txt 合规中 · 限速 ${(health.compliance?.delay_range || []).join('~')}s`
      : 'robots.txt 检查已关闭';
  }
}

async function refreshHealth() {
  try {
    renderHealth(await api.health());
  } catch (err) {
    console.warn('[health] 获取失败', err);
  }
}

/* ==========================================================================
   交互细节
   ========================================================================== */
/** 按钮光晕跟随鼠标: 写入 --mx/--my 供 CSS 的 radial-gradient 使用 */
function initButtonGlow() {
  document.addEventListener(
    'pointermove',
    (event) => {
      const button = event.target.closest('.btn');
      if (!button) return;
      const rect = button.getBoundingClientRect();
      button.style.setProperty('--mx', `${event.clientX - rect.left}px`);
      button.style.setProperty('--my', `${event.clientY - rect.top}px`);
    },
    { passive: true },
  );
}

/** 侧边栏折叠 */
function initSidebar() {
  const app = $('#app');
  const apply = (collapsed) => {
    app.classList.toggle('is-collapsed', collapsed);
    set('ui.sidebarCollapsed', collapsed);
    persistUiPreferences();
  };
  apply(Boolean(get('ui.sidebarCollapsed')));

  $('#collapseBtn')?.addEventListener('click', () => {
    apply(!app.classList.contains('is-collapsed'));
  });

  $('#menuBtn')?.addEventListener('click', () => {
    app.classList.toggle('is-drawer-open');
  });

  // 移动端点击内容区收起抽屉
  $('#content')?.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 820px)').matches) app.classList.remove('is-drawer-open');
  });
}

/** 主题切换(快捷键 Ctrl+J 循环切换深色/浅色/高对比) */
function initTheme() {
  const themes = ['aurora', 'daylight', 'contrast'];
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    set('ui.theme', theme);
    persistUiPreferences();
  };
  apply(get('ui.theme') || 'aurora');

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'j') {
      event.preventDefault();
      const next = themes[(themes.indexOf(get('ui.theme')) + 1) % themes.length];
      apply(next);
      toastInfo(`已切换到「${next === 'aurora' ? '深色' : next === 'daylight' ? '浅色' : '高对比'}」主题`);
    }
  });
}

/** 全局快捷键 */
function initShortcuts() {
  document.addEventListener('keydown', (event) => {
    const inField = event.target.closest('input, textarea, select, [contenteditable]');

    // Ctrl/Cmd + Enter: 在当前页提交主操作
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      const startBtn = $('#btnCrawlStart');
      if (get('ui.page') === 'crawl' && startBtn && !startBtn.hidden) {
        event.preventDefault();
        startBtn.click();
      }
      return;
    }

    if (event.ctrlKey || event.metaKey) {
      if (event.key.toLowerCase() === 'l') {
        event.preventDefault();
        toggleLogDrawer();
      } else if (event.key.toLowerCase() === 'b') {
        event.preventDefault();
        $('#collapseBtn')?.click();
      }
      return;
    }

    if (inField) return;

    if (event.key === 'Escape') {
      if (isModalOpen()) closeModal();
      else if ($('#logDrawer')?.classList.contains('is-open')) closeLogDrawer();
    }
  });
}

/* ==========================================================================
   启动
   ========================================================================== */
/**
 * 关闭服务 —— 从界面上把后台彻底停掉。
 *
 * **为什么要二次确认**: 正在跑的抓取会被中断, 而且关掉后当前页面就再也点不动了,
 * 误触的代价不小。确认框里把"会中断什么"说清楚, 而不是只问"确定吗"。
 */
async function shutdownService() {
  const ok = window.confirm(
    '关闭 SmartCrawler 服务?\n\n' +
      '· 正在运行的抓取/分析任务会被中断\n' +
      '· Playwright 的浏览器进程会被一并清理(不会留在后台占资源)\n' +
      '· 关闭后本页面将无法再操作, 需重新启动服务\n\n确定关闭?',
  );
  if (!ok) return;

  try {
    await api.shutdown();
    toastSuccess('服务正在关闭, 浏览器子进程已清理。可以关掉这个页面了。');
  } catch (err) {
    // 服务可能在响应写完之前就退出了, 这种情况下"请求失败"其实是成功
    toastInfo('服务已停止(连接随之中断)');
  }

  // 索性把界面切成不可用状态, 免得用户对着一个已经死掉的服务继续点
  document.body.classList.add('is-shutdown');
  const banner = document.createElement('div');
  banner.className = 'shutdown-banner';
  banner.textContent = '服务已关闭。重新启动请运行 start.bat 或 python -m smartcrawler web';
  document.body.appendChild(banner);
}

async function boot() {
  loadUiPreferences();

  initButtonGlow();
  initModal();
  initSidebar();
  initTheme();
  initLogPanel();
  initShortcuts();
  initRouter();

  // 顶栏芯片的点击行为
  $('#chipAi')?.addEventListener('click', () => goToPage('settings'));
  $('#chipBrowser')?.addEventListener('click', () => goToPage('settings'));
  $('#chipRobots')?.addEventListener('click', () => goToPage('settings'));
  $('#chipPlugins')?.addEventListener('click', () => goToPage('plugins'));
  $('#chipShutdown')?.addEventListener('click', shutdownService);

  await refreshHealth();
  connectHealth(renderHealth);

  // 页面重新可见时刷新一次状态(长时间后台后可能已过期)
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshHealth();
  });

  // 首次进入给一句引导
  const health = get('health');
  if (health && !health.ai?.available) {
    toastInfo('AI 尚未就绪,抓取会自动使用规则引擎。配置 API Key 后可用自然语言生成规则。', '提示');
  }

  // 移除首屏骨架遮罩
  document.body.classList.add('is-ready');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
