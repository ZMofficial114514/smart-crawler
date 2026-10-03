/**
 * ui/login-panel.js —— 登录状态提示与手动登录流程
 * ---------------------------------------------------------------------------
 * 为什么需要它: 有些站点**登录前后页面结构完全不同**。pixiv 首页匿名时是一个注册/
 * 登录引导页, 登录后才是作品瀑布流。如果不先把这件事讲清楚, 用户会拿着匿名视图的
 * 结构去调选择器, 完全找错方向。
 *
 * 交互: 分析完成后若判定为未登录, 就在报告顶部放一条提示, 带一个"登录一次"按钮。
 * 点开后会启动一个**可见**的浏览器窗口让用户自己操作(框架不接触密码), 登录完成后
 * 由用户点确认, 会话保存下来供后续抓取复用。
 */

import { el } from '../utils.js';
import api from '../api.js';
import { toastError, toastSuccess, toastInfo } from './toast.js';
import { openModal, closeModal } from './modal.js';

const STATE_STYLE = {
  anonymous: { tone: 'warning', label: '当前为未登录访问' },
  logged_in: { tone: 'ok', label: '当前为已登录访问' },
  unknown: { tone: 'muted', label: '登录状态不明确' },
};

/**
 * 渲染登录状态提示条。
 *
 * @param {object|null} loginState 后端返回的 login_state
 * @param {string} pageUrl 被分析的页面地址(用于预填登录入口)
 * @param {object} options
 * @param {Function} [options.onSessionSaved] 会话保存成功后的回调, 参数 (url, action)。
 *        action 为 `'saved'`(刚保存完)或 `'analyze'`/`'crawl'`(用户在第 2 步选的去向)。
 *        由调用方决定"下一步"怎么做 —— 例如分析页据此用新会话重新分析一次。
 * @returns {HTMLElement|null} 不需要提示时返回 null
 */
export function renderLoginState(loginState, pageUrl = '', options = {}) {
  if (!loginState || !loginState.state) return null;

  const style = STATE_STYLE[loginState.state] || STATE_STYLE.unknown;

  // 已登录就不必打扰用户, 只在需要时提示
  if (loginState.state === 'logged_in') return null;

  // **被挑战挡住时不引导登录**。挑战页/风控页往往也长得像登录页(有的直接是登录页),
  // 于是登录状态判定会给出"匿名", 界面就把『登录一次』当成主行动高亮出来 —— 但用户
  // 真正卡住的是人机验证: 他手动打开浏览器是好好的(因为人过了验证), 重新分析却被拦,
  // 于是反复被要求登录, 越试越糊涂。这时应只提示"验证挡住了", 由验证面板主导。
  if (options.challengeDetected) {
    return el('div.alert.alert--info', { style: { marginBottom: 'var(--sp-4)' } }, [
      el('div.alert__body', {}, [
        el('div.alert__title', { text: '登录状态暂时无法判断' }),
        el('div', {
          text:
            '这个页面正被人机验证挡住, 现在看到的内容并不是登录前后的真实差异 —— ' +
            '请先完成上方的验证, 再重新分析。',
        }),
        loginState.reasons?.length
          ? el('div.hint', {
              style: { marginTop: '4px' },
              text: `(供参考的判定依据: ${loginState.reasons.slice(0, 2).join('; ')})`,
            })
          : null,
      ]),
    ]);
  }

  const isAnonymous = loginState.state === 'anonymous';
  const confidence = Math.round((loginState.confidence || 0) * 100);

  const children = [
    el('div', { style: { flex: '1', minWidth: '0' } }, [
      el('div', {
        text: isAnonymous
          ? `这个页面看起来是「未登录」的版本(置信度 ${confidence}%)`
          : '无法确定这个页面是登录态还是匿名态',
      }),
      el('div.hint', {
        style: { marginTop: '4px' },
        text: isAnonymous
          ? '登录后页面结构可能与现在完全不同 —— 若抓不到想要的列表, 建议先登录一次再重新分析。'
          : '若你确认该页面需要登录才能看到内容, 可以直接登录一次再重新分析。',
      }),
    ]),
  ];

  // 判定依据: 让用户理解"为什么说我没登录"
  if (loginState.reasons?.length) {
    children.push(
      el('details.tree', { style: { width: '100%' } }, [
        el('summary', { text: '判定依据' }),
        el(
          'ul.login-wall__list',
          {},
          loginState.reasons.map((r) => el('li', { text: r })),
        ),
        loginState.login_entries?.length
          ? el('div.field-chips', { style: { marginTop: '8px' } }, [
              ...loginState.login_entries.map((e) =>
                el('span.field-chip', { text: `${e.text} → ${e.href || '(无)'}` }),
              ),
            ])
          : null,
      ]),
    );
  }

  children.push(
    el('button.btn.btn--sm', {
      text: '登录一次',
      title: '打开一个可见的浏览器窗口, 由你手动登录, 完成后保存会话',
      onclick: () => openLoginModal(pageUrl, loginState, options),
    }),
  );

  return el(
    `div.alert.alert--${style.tone === 'ok' ? 'info' : style.tone}`,
    { style: { marginBottom: 'var(--sp-4)' } },
    [el('div.alert__body', { style: { display: 'flex', gap: 'var(--sp-3)', flexWrap: 'wrap', alignItems: 'flex-start' } }, children)],
  );
}

/**
 * 渲染"页面要求人机验证"提示条。
 *
 * 这类验证只有人能过, 框架不尝试绕过; 界面给出「手动过验证」入口: 打开一个可见窗口,
 * 用户过完验证后会话里就带上了通关凭据, 之后可以继续原来的任务。
 *
 * @param {object|null} challenge 后端返回的 challenge 判定
 * @param {string} pageUrl 目标地址
 * @param {object} options
 * @param {Function} [options.onResolved] 验证通过并保存会话后的回调
 */
export function renderChallenge(challenge, pageUrl = '', options = {}) {
  if (!challenge || !challenge.detected) return null;

  const confidence = Math.round((challenge.confidence || 0) * 100);
  const children = [
    el('div', { style: { flex: '1', minWidth: '0' } }, [
      el('div', { text: `这个页面要求完成${challenge.kind}(置信度 ${confidence}%)` }),
      el('div.hint', {
        style: { marginTop: '4px' },
        text:
          '人机验证只有人能完成, 框架不会尝试绕过。点右侧按钮会打开一个可见窗口, ' +
          '你在里面过完验证即可 —— 通关凭据会保存进会话, 之后的抓取自动带上。',
      }),
    ]),
  ];

  if (challenge.reasons?.length) {
    children.push(
      el('details.tree', { style: { width: '100%' } }, [
        el('summary', { text: '判定依据' }),
        el('ul.login-wall__list', {}, challenge.reasons.map((r) => el('li', { text: r }))),
      ]),
    );
  }

  children.push(
    el('button.btn.btn--sm.btn--primary', {
      text: '手动过验证',
      title: '打开一个可见的浏览器窗口, 由你完成人机验证, 完成后保存会话',
      onclick: () => openChallengeModal(pageUrl, challenge, options),
    }),
  );

  return el(
    // sticky: 报告很长时用户会往下滚, 而"过验证"是必须先解决的事 —— 让这条提示始终
    // 贴在内容区顶部, 否则用户往下看一会儿就找不到入口了(实测滚到底后按钮在视口外
    // y=-1107, 根本点不到)。
    'div.alert.alert--warning.alert--sticky',
    { style: { marginBottom: 'var(--sp-4)' } },
    [
      el(
        'div.alert__body',
        { style: { display: 'flex', gap: 'var(--sp-3)', flexWrap: 'wrap', alignItems: 'flex-start' } },
        children,
      ),
    ],
  );
}

/**
 * 打开"手动过人机验证"弹层(challenge 模式的登录弹层)。
 *
 * 复用同一套「打开窗口 → 轮询状态 → 确认保存」机制, 只是文案与判据不同:
 * challenge 模式看的是"挑战是否消失", 而不是"是否已登录"。
 */
export async function openChallengeModal(pageUrl = '', challenge = null, options = {}) {
  const { onResolved = null } = options;

  let overview = null;
  try {
    overview = await api.getSession();
  } catch {
    overview = null;
  }

  const urlInput = el('input.input', { type: 'text', value: pageUrl || 'https://', spellcheck: 'false' });
  const statusBox = el('div.login-flow__status', {
    text: challenge ? `检测到${challenge.kind} —— 尚未开始处理` : '尚未开始',
  });
  const stepsBox = el('div.login-flow__steps');
  const verifyBox = el('div.login-flow__verify', { hidden: true });

  const nextStep = el('div.login-flow__next', { hidden: true }, [
    el('div.login-flow__next-head', { text: '第 2 步 · 用这个会话继续' }),
    el('p.hint', { text: '验证已通过, 通关凭据已保存进会话。可以继续原来的任务了。' }),
    el('div.login-flow__actions', {}, [
      el('button.btn.btn--primary', {
        text: '用通过验证的身份重新分析',
        onclick: () => {
          closeModal();
          if (onResolved) onResolved(urlInput.value, 'analyze');
        },
      }),
      el('button.btn.btn--ghost', {
        text: '去抓取这个页面',
        onclick: () => {
          closeModal();
          if (onResolved) onResolved(urlInput.value, 'crawl');
        },
      }),
    ]),
  ]);

  const body = el('div.login-flow', {}, [
    el('p.hint', {
      text:
        '点「打开验证窗口」会弹出一个可见的浏览器, 请在窗口中完成人机验证' +
        '(点选图片 / 滑块 / 勾选均可)。过完后回到这里点『验证已完成, 保存会话』。' +
        '框架不会读取或代填任何凭据。',
    }),
    el('div.field', {}, [el('label', { text: '要过验证的站点' }), urlInput]),
    el('div.login-flow__meta', {}, [
      el('span.tag.tag--ghost', {
        text: overview?.session?.exists
          ? `已有会话: ${overview.session.cookies} 个 Cookie`
          : '尚未保存任何会话',
      }),
      el('span.hint', { text: '会话文件等同于凭据, 请勿分享(已在 .gitignore 中)' }),
    ]),
    stepsBox,
    statusBox,
    el('div.login-flow__actions', {}, [
      el('button.btn.btn--primary', {
        text: '打开验证窗口',
        onclick: (event) => startChallenge(event.target, urlInput.value, statusBox, stepsBox),
      }),
      el('button.btn.btn--ghost', {
        text: '验证已完成, 保存会话',
        disabled: true,
        dataset: { role: 'confirm' },
        onclick: (event) =>
          confirmLogin(event.target, statusBox, stepsBox, nextStep, verifyBox, onResolved),
      }),
      el('button.btn.btn--ghost', {
        text: '取消',
        disabled: true,
        dataset: { role: 'cancel' },
        onclick: (event) => cancelLogin(event.target, statusBox),
      }),
    ]),
    nextStep,
    verifyBox,
  ]);

  openModal({ title: '手动完成人机验证', body, width: '660px' });
}

async function startChallenge(button, url, statusBox, stepsBox) {
  if (!url || !/^https?:\/\//i.test(url)) {
    toastError('请填写以 http:// 或 https:// 开头的站点地址');
    return;
  }
  button.disabled = true;
  button.textContent = '正在启动…';
  try {
    await api.startLogin(url, 'challenge');
    statusBox.textContent = '验证窗口已打开, 请在其中完成验证…';
    setButtons(true);
    stepsBox.textContent = '';
    startPolling(statusBox, stepsBox);
    toastInfo('已打开验证窗口 —— 请在弹出的浏览器里完成人机验证');
  } catch (err) {
    toastError(err?.message || '无法启动验证窗口');
    statusBox.textContent = `启动失败: ${err?.message || err}`;
  } finally {
    button.disabled = false;
    button.textContent = '重新打开验证窗口';
  }
}

/**
 * 渲染"懒加载/无限流"提示条。
 *
 * 场景: 页面内容靠滚动懒加载, 而**没有加载上限**(无限流)。框架不该替用户决定抓多久,
 * 所以按配置上限滚完后如实报告"内容仍在增长", 并由用户决定是否继续。
 *
 * @param {object|null} lazy 后端返回的 lazy_load 结果
 * @param {object} options
 * @param {Function} [options.onContinue] 用户选择继续时回调(参数为要追加的轮次)
 */
export function renderLazyLoad(lazy, options = {}) {
  if (!lazy || !lazy.rounds) return null;
  const { onContinue = null } = options;

  const grew = (lazy.node_gain || 0) > 0;
  const infinite = Boolean(lazy.infinite);

  // 滚完就到底了, 而且确实加载出了内容: 给一条轻量提示就够了, 不必打扰
  if (!infinite) {
    if (!grew) return null;
    return el('div.alert.alert--info', { style: { marginBottom: 'var(--sp-4)' } }, [
      el('div.alert__body', {}, [
        el('div.alert__title', { text: '已自动滚动加载懒加载内容' }),
        el('div', { text: lazy.summary || '' }),
      ]),
    ]);
  }

  const extra = 12;
  const children = [
    el('div', { style: { flex: '1', minWidth: '0' } }, [
      el('div', { text: '这个页面的内容似乎没有加载上限(无限流)' }),
      el('div.hint', {
        style: { marginTop: '4px' },
        text:
          `${lazy.summary || ''}。已经滚到设定的上限就停了 —— ` +
          '再往下还有更多内容, 是否继续由你决定(继续会显著增加耗时)。',
      }),
    ]),
  ];

  if (onContinue) {
    children.push(
      el('button.btn.btn--sm.btn--primary', {
        text: `继续向下滚动 ${extra} 轮`,
        title: '再模拟滚动若干轮, 把更多内容加载出来',
        onclick: (event) => {
          event.target.disabled = true;
          onContinue(extra);
        },
      }),
    );
  }

  return el(
    'div.alert.alert--warning.alert--sticky',
    { style: { marginBottom: 'var(--sp-4)' } },
    [
      el(
        'div.alert__body',
        { style: { display: 'flex', gap: 'var(--sp-3)', flexWrap: 'wrap', alignItems: 'flex-start' } },
        children,
      ),
    ],
  );
}

/* ==========================================================================
   登录弹层
   ========================================================================== */

let pollTimer = null;

/**
 * 打开"手动登录"弹层。
 *
 * @param {string} pageUrl 预填的登录入口
 * @param {object|null} loginState 当前页面的登录状态判定
 * @param {object} options
 * @param {Function} [options.onSessionSaved] 会话保存成功后回调(参数为会话摘要),
 *        由调用方决定"重新分析/继续抓取"具体怎么做。
 */
export async function openLoginModal(pageUrl = '', loginState = null, options = {}) {
  const { onSessionSaved = null } = options;

  // 先取当前会话状态, 让用户知道之前是否登录过
  let overview = null;
  try {
    overview = await api.getSession();
  } catch {
    overview = null;
  }

  const urlInput = el('input.input', {
    type: 'text',
    value: pageUrl || 'https://',
    placeholder: 'https://www.pixiv.net/',
    spellcheck: 'false',
  });

  const statusBox = el('div.login-flow__status', { text: '尚未开始' });
  const stepsBox = el('div.login-flow__steps');
  const verifyBox = el('div.login-flow__verify', { hidden: true });

  const savedInfo = overview?.session?.exists
    ? `已有会话: ${overview.session.cookies} 个 Cookie, 保存于 ${overview.session.saved_at || '未知时间'}`
    : '尚未保存任何会话';

  // ---- 第 2 步: 登录成功后才会出现 ----
  // 这是上一版缺的东西: 保存完会话就没了下文, 用户不知道接下来该怎么办。
  const nextStep = el('div.login-flow__next', { hidden: true }, [
    el('div.login-flow__next-head', { text: '第 2 步 · 用这个会话继续' }),
    el('p.hint', {
      text: '会话已保存。接下来可以用登录后的身份重新访问页面 —— 登录前后结构不同的站点(如 pixiv)会拿到完全不同的内容。',
    }),
    el('div.login-flow__actions', {}, [
      el('button.btn.btn--primary', {
        text: '用登录后的身份重新分析',
        onclick: (event) => {
          closeModal();
          if (onSessionSaved) onSessionSaved(urlInput.value, 'analyze');
        },
      }),
      el('button.btn.btn--ghost', {
        text: '去抓取这个页面',
        onclick: (event) => {
          closeModal();
          if (onSessionSaved) onSessionSaved(urlInput.value, 'crawl');
        },
      }),
      el('button.btn.btn--ghost', {
        text: '先核实会话是否生效',
        onclick: (event) => verifySession(event.target, urlInput.value, verifyBox),
      }),
    ]),
  ]);

  const body = el('div.login-flow', {}, [
    el('p.hint', {
      text:
        '点击「打开登录窗口」后会弹出一个可见的浏览器, 请在那个窗口里正常登录(验证码/扫码都可以)。' +
        '框架不会接触你的密码, 只保存登录后服务端签发的会话凭据。',
    }),
    el('div.field', {}, [el('label', { text: '要登录的站点' }), urlInput]),
    el('div.login-flow__meta', {}, [
      el('span.tag.tag--ghost', { text: savedInfo }),
      el('span.hint', { text: '会话文件等同于凭据, 请勿分享(已在 .gitignore 中)' }),
    ]),
    stepsBox,
    statusBox,
    el('div.login-flow__actions', {}, [
      el('button.btn.btn--primary', {
        text: '打开登录窗口',
        onclick: (event) => startLogin(event.target, urlInput.value, statusBox, stepsBox),
      }),
      el('button.btn.btn--ghost', {
        text: '我已登录, 保存会话',
        disabled: true,
        dataset: { role: 'confirm' },
        onclick: (event) =>
          confirmLogin(event.target, statusBox, stepsBox, nextStep, verifyBox, onSessionSaved),
      }),
      el('button.btn.btn--ghost', {
        text: '取消登录',
        disabled: true,
        dataset: { role: 'cancel' },
        onclick: (event) => cancelLogin(event.target, statusBox),
      }),
    ]),
    nextStep,
    verifyBox,
    el('button.link-btn', {
      text: '删除已保存的会话(退出登录)',
      style: { marginTop: 'var(--sp-3)' },
      onclick: () => clearSession(statusBox, nextStep),
    }),
  ]);

  openModal({ title: '手动登录并保存会话', body, width: '660px' });

  // 若已有流程在跑(比如用户刷新过页面), 直接接上进度
  if (overview?.flow?.alive) {
    setButtons(true);
    statusBox.textContent = '已有登录流程在进行中, 正在同步状态…';
    startPolling(statusBox, stepsBox, nextStep, verifyBox, onSessionSaved);
  } else if (overview?.session?.exists) {
    // 已经有会话了: 直接给出"用它继续", 不让用户再登录一遍
    nextStep.hidden = false;
    statusBox.textContent = `已保存会话(${overview.session.cookies} 个 Cookie), 可直接使用`;
  }
}

function setButtons(enabled) {
  document.querySelectorAll('[data-role="confirm"], [data-role="cancel"]').forEach((b) => {
    b.disabled = !enabled;
  });
}

async function startLogin(button, url, statusBox, stepsBox) {
  if (!url || !/^https?:\/\//i.test(url)) {
    toastError('请填写以 http:// 或 https:// 开头的站点地址');
    return;
  }
  button.disabled = true;
  button.textContent = '正在启动…';
  try {
    await api.startLogin(url);
    statusBox.textContent = '浏览器窗口已打开, 请在其中完成登录…';
    setButtons(true);
    stepsBox.textContent = '';
    startPolling(statusBox, stepsBox);
    toastInfo('已打开登录窗口 —— 请在弹出的浏览器里完成登录');
  } catch (err) {
    toastError(err?.message || '无法启动登录窗口');
    statusBox.textContent = `启动失败: ${err?.message || err}`;
  } finally {
    button.disabled = false;
    button.textContent = '重新打开登录窗口';
  }
}

/**
 * 确认保存会话。
 *
 * 后端会**等会话真正落盘**再返回, 所以这里的 `session_saved` 是可信的 —— 不再出现
 * "界面说保存成功、实际没写出来"的情况。保存成功后立刻展开第 2 步。
 */
async function confirmLogin(button, statusBox, stepsBox, nextStep, verifyBox, onSessionSaved) {
  button.disabled = true;
  button.textContent = '正在保存…';
  try {
    const result = await api.confirmLogin();
    statusBox.textContent = result?.message || '已处理';

    if (result?.session_saved) {
      stopPolling();
      setButtons(false);
      const cookies = result?.status?.cookies ?? 0;
      stepsBox.textContent = cookies
        ? `会话文件已写入(${cookies} 个 Cookie), 之后的抓取会自动带上它。`
        : '会话文件已写入。';
      nextStep.hidden = false;
      toastSuccess('会话已保存 —— 可以接着用登录后的身份访问了');
      nextStep.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      if (onSessionSaved) onSessionSaved(null, 'saved');
      return;
    }

    // 没成功: 把原因说清楚, 而不是让用户猜
    toastError(result?.message || '未能保存会话');
    stepsBox.textContent =
      '没有拿到会话数据。请在浏览器窗口里确认已经登录成功, 然后重新点击「我已登录, 保存会话」。';
    if (result?.helper_log) {
      verifyBox.hidden = false;
      verifyBox.textContent = `辅助进程日志:\n${String(result.helper_log).slice(-600)}`;
    }
  } catch (err) {
    toastError(err?.message || '保存失败');
    statusBox.textContent = `保存失败: ${err?.message || err}`;
  } finally {
    button.disabled = false;
    button.textContent = '我已登录, 保存会话';
  }
}

/** 用已保存的会话真的访问一次, 核实登录是否生效。 */
async function verifySession(button, url, verifyBox) {
  if (!url || !/^https?:\/\//i.test(url)) {
    toastError('请填写站点地址');
    return;
  }
  button.disabled = true;
  const original = button.textContent;
  button.textContent = '正在核实…';
  verifyBox.hidden = false;
  verifyBox.textContent = '正在用已保存的会话打开该页面…';
  try {
    const result = await api.verifySession(url);
    const lines = [
      result?.message || '',
      `判定: ${result?.login_state || '?'} · 置信度 ${Math.round((result?.confidence || 0) * 100)}%`,
      `本次是否带上会话: ${result?.session_restored ? '是' : '否'}`,
      result?.page_title ? `页面标题: ${result.page_title}` : '',
      ...(result?.reasons || []).slice(0, 4).map((r) => `· ${r}`),
    ].filter(Boolean);
    verifyBox.textContent = lines.join('\n');
    verifyBox.dataset.state = result?.logged_in ? 'ok' : 'warn';
    if (result?.logged_in) toastSuccess('会话已生效: 这次访问是登录态');
    else toastInfo('会话已带上, 但该页面仍被判定为未登录');
  } catch (err) {
    verifyBox.textContent = `核实失败: ${err?.message || err}`;
    verifyBox.dataset.state = 'warn';
    toastError(err?.message || '核实失败');
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function cancelLogin(button, statusBox) {
  button.disabled = true;
  try {
    await api.cancelLogin();
    stopPolling();
    statusBox.textContent = '已取消';
    setButtons(false);
    toastInfo('已取消登录');
  } catch (err) {
    toastError(err?.message || '取消失败');
  } finally {
    button.disabled = false;
  }
}

async function clearSession(statusBox, nextStep) {
  try {
    const result = await api.deleteSession();
    statusBox.textContent = result?.message || '会话已删除';
    if (nextStep) nextStep.hidden = true;
    toastSuccess('已删除保存的会话');
  } catch (err) {
    toastError(err?.message || '删除失败');
  }
}

/**
 * 轮询登录流程进度。
 *
 * 用轮询而不是 WebSocket: 进度来自一个独立进程写的状态文件, 轮询更简单也更稳,
 * 断线不会让界面卡在"进行中"。
 *
 * 停止条件用 `session_saved`(由**会话文件本身**证明)而不是辅助进程写的
 * `state === 'saved'` —— 后者可能领先于会话文件真正落盘。
 */
function startPolling(statusBox, stepsBox, nextStep = null, verifyBox = null, onSessionSaved = null) {
  stopPolling();
  const tick = async () => {
    try {
      const status = await api.loginStatus();
      renderFlowStatus(status, statusBox, stepsBox);

      if (status.session_saved) {
        stopPolling();
        setButtons(false);
        if (nextStep) nextStep.hidden = false;
        toastSuccess(
          status.merged_cookies || status.cookies
            ? `会话已保存(${status.merged_cookies || status.cookies} 个 Cookie), 下次抓取自动带上`
            : '会话已保存',
        );
        if (onSessionSaved) onSessionSaved(null, 'saved');
        return;
      }
      if (['error', 'cancelled'].includes(status.state) || (!status.alive && status.exit_code !== undefined)) {
        stopPolling();
        setButtons(false);
        if (verifyBox && status.helper_log) {
          verifyBox.hidden = false;
          verifyBox.textContent = `辅助进程日志:\n${String(status.helper_log).slice(-600)}`;
        }
        return;
      }
    } catch {
      // 单次轮询失败忽略, 下一个周期再试
    }
  };
  tick();
  pollTimer = setInterval(tick, 1500);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function renderFlowStatus(status, statusBox, stepsBox) {
  if (!status) return;

  const loginLabel = {
    logged_in: '已在浏览器中检测到登录状态',
    anonymous: '浏览器中仍是未登录状态',
    unknown: '登录状态未知(可能页面不典型)',
  }[status.login_state];

  statusBox.textContent = [
    status.step || '',
    loginLabel ? `· ${loginLabel}` : '',
    status.stale ? '· 状态更新已暂停(窗口可能已关闭)' : '',
    status.error ? `· ${status.error}` : '',
  ]
    .filter(Boolean)
    .join(' ');

  if (status.hint) {
    stepsBox.textContent = status.hint;
  } else if (status.login_state === 'logged_in') {
    stepsBox.textContent = '检测到已登录 —— 现在可以点「我已登录, 保存会话」了。';
  } else if (status.state === 'waiting') {
    stepsBox.textContent = '等待你在浏览器窗口中完成登录…';
  } else if (status.state === 'saved') {
    stepsBox.textContent = `已保存到 ${status.output || status.session_path || '会话文件'}`;
  } else {
    stepsBox.textContent = '';
  }
}

export { closeModal };
