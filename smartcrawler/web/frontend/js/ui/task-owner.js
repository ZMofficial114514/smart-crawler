/**
 * ui/task-owner.js —— 判断一条任务事件是否属于"当前页面正在跑的任务"
 * ---------------------------------------------------------------------------
 * 背景: 抓取 / 结构分析 / 网络抓包三个页面共用**同一条**任务事件总线, 而 store.task
 * 保存的是"全局最近一次任务", 所以每条事件都必须判断归属, 否则会出现"分析页拿到抓取
 * 结果"这类错乱。
 *
 * 曾经的写法是只看 store.task.kind::
 *
 *     if (known && known.kind && known.kind !== 'crawl') return;   // ✗ 有 bug
 *
 * 问题在于 store.task 可能来自**别的页面**: 先抓取、再去结构分析时, store.task 仍是
 * 那个抓取任务(kind='crawl'), 于是分析任务的结果事件被这行守卫直接丢掉, 界面永远停在
 * "分析中…"。任务 id 才是权威依据 —— 它是服务端为本次请求生成的, 不可能张冠李戴。
 *
 * 因此判定顺序是:
 *   1. 本页已启动过任务 → 只认这个 id;
 *   2. 本页没启动过(例如从历史恢复) → 用 store 里的 id 兜底;
 *   3. 只有 id 完全无从判断时, 才退回 kind 比较。
 */

/**
 * @param {object} event WebSocket 任务事件(含 task_id、type、status…)
 * @param {object} options
 * @param {string|null} options.activeTaskId 本页当前任务的 id
 * @param {object|null} options.knownTask store 里的最近任务(可为 null)
 * @param {string} options.kind 本页任务类型('crawl' | 'analyze' | 'requests')
 * @returns {boolean} true 表示这条事件属于本页任务, 应当处理
 */
export function isOwnTaskEvent(event, { activeTaskId, knownTask, kind }) {
  const eventId = event?.task_id;

  if (activeTaskId) {
    return !eventId || eventId === activeTaskId;
  }

  if (knownTask && knownTask.id) {
    return !eventId || eventId === knownTask.id;
  }

  // id 无从判断时才看类型(例如刷新后 store 是空的)
  if (knownTask && knownTask.kind && knownTask.kind !== kind) return false;
  return true;
}

export default { isOwnTaskEvent };
