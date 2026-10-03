/**
 * ws.js —— WebSocket 客户端: 实时日志 + 任务进度
 * ---------------------------------------------------------------------------
 * 断线自动重连(指数退避,封顶 12 秒),页面隐藏时降低重连频率,避免后台标签页
 * 疯狂重试。事件通过简单的回调注册分发,不依赖 store,以便日志走独立的高频通道。
 */

import { set } from './store.js';

const RECONNECT_BASE = 800;
const RECONNECT_MAX = 12000;

class ReconnectingSocket {
  /**
   * @param {string} url
   * @param {{ onMessage: (data:any)=>void, onOpen?: ()=>void, onClose?: ()=>void, name?: string }} opts
   */
  constructor(url, opts) {
    this.url = url;
    this.opts = opts;
    this.attempt = 0;
    this.socket = null;
    this.closedByUser = false;
    this.timer = null;
    this.connect();
  }

  connect() {
    try {
      this.socket = new WebSocket(this.url);
    } catch (err) {
      this.scheduleReconnect();
      return;
    }

    this.socket.addEventListener('open', () => {
      this.attempt = 0;
      this.opts.onOpen?.();
    });

    this.socket.addEventListener('message', (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.type === 'ping') {
        this.opts.onPing?.(data);
        return;
      }
      this.opts.onMessage?.(data);
    });

    this.socket.addEventListener('close', () => {
      this.opts.onClose?.();
      if (!this.closedByUser) this.scheduleReconnect();
    });

    this.socket.addEventListener('error', () => {
      // error 之后必然触发 close,统一在 close 里做重连
      try {
        this.socket.close();
      } catch {
        /* noop */
      }
    });
  }

  scheduleReconnect() {
    if (this.timer) return;
    // 页面不可见时拉长间隔,减少无谓请求
    const hiddenFactor = document.hidden ? 3 : 1;
    const delay = Math.min(RECONNECT_BASE * 2 ** this.attempt, RECONNECT_MAX) * hiddenFactor;
    this.attempt = Math.min(this.attempt + 1, 6);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }

  send(data) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }

  close() {
    this.closedByUser = true;
    clearTimeout(this.timer);
    this.timer = null;
    try {
      this.socket?.close();
    } catch {
      /* noop */
    }
  }
}

/* ==========================================================================
   日志流
   ========================================================================== */
let logSocket = null;
const logHandlers = new Set();

export function connectLogs() {
  if (logSocket) return logSocket;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  logSocket = new ReconnectingSocket(`${protocol}//${window.location.host}/ws/logs`, {
    name: 'logs',
    onMessage: (data) => {
      if (data.type !== 'log') return;
      for (const handler of logHandlers) {
        try {
          handler(data);
        } catch (err) {
          console.error('[ws] 日志处理异常', err);
        }
      }
    },
    onOpen: () => set('ui.logConnected', true),
    onClose: () => set('ui.logConnected', false),
  });
  return logSocket;
}

/** 订阅日志行;返回取消函数 */
export function onLog(handler) {
  logHandlers.add(handler);
  return () => logHandlers.delete(handler);
}

/* ==========================================================================
   任务进度流
   ========================================================================== */
let taskSocket = null;
let taskId = null;
const taskHandlers = new Set();

export function connectTask(id) {
  if (taskId === id && taskSocket) return taskSocket;
  disconnectTask();
  taskId = id;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  taskSocket = new ReconnectingSocket(`${protocol}//${window.location.host}/ws/tasks/${id}`, {
    name: 'task',
    onMessage: (data) => {
      for (const handler of taskHandlers) {
        try {
          handler(data);
        } catch (err) {
          console.error('[ws] 任务事件处理异常', err);
        }
      }
    },
  });
  return taskSocket;
}

export function onTaskEvent(handler) {
  taskHandlers.add(handler);
  return () => taskHandlers.delete(handler);
}

export function disconnectTask() {
  taskSocket?.close();
  taskSocket = null;
  taskId = null;
}

/* ==========================================================================
   健康状态低频推送
   ========================================================================== */
let healthSocket = null;

export function connectHealth(onData) {
  if (healthSocket) return healthSocket;
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  healthSocket = new ReconnectingSocket(`${protocol}//${window.location.host}/ws/health`, {
    name: 'health',
    onMessage: (data) => {
      if (data.type === 'health') onData(data.data);
    },
  });
  return healthSocket;
}

export function socketStats() {
  const stateOf = (s) => (!s?.socket ? 'closed' : ['connecting', 'open', 'closing', 'closed'][s.socket.readyState] || '?');
  return { logs: stateOf(logSocket), task: stateOf(taskSocket), health: stateOf(healthSocket) };
}

export default { connectLogs, onLog, connectTask, onTaskEvent, disconnectTask, connectHealth, socketStats };
