// 问答流式协议：把后端事件归约为可渲染的轮次状态，并驱动 Fetch ReadableStream。
// 事件只描述真实系统阶段，不含模型思维链；正文增量与最终校验结果分开处理。
import { readEvents } from './events.mjs';
import { ApiError } from './core.mjs';

export const STAGE_LABEL = {
  context: '正在加载会话上下文…',
  rewrite: '正在构造检索问题…',
  retrieval: '正在检索知识库…',
  generation: '正在生成回答…',
};

export const STAGE_DONE_LABEL = {
  context: '会话上下文',
  rewrite: '检索问题',
  retrieval: '知识库检索',
  generation: '生成回答',
  citations: '引用校验',
};

export const STAGE_ORDER = ['context', 'rewrite', 'retrieval', 'generation', 'citations'];

// 一轮执行的对外状态：停止与失败都不算完成，避免把未通过校验的回答显示成正常结果。
export const EXECUTION_STATE = {
  running: { label: '正在回答', icon: 'sparkle' },
  completed: { label: '回答完成', icon: 'check' },
  stopped: { label: '已停止', icon: 'stop' },
  error: { label: '执行失败', icon: 'alert' },
};

/** 发送按钮的两种视图：同一个按钮承担发送与停止，不会同时出现两个按钮。 */
export function sendButtonView(streaming) {
  return streaming
    ? { icon: 'stop', label: '停止生成', className: 'send-btn is-stop' }
    : { icon: 'arrow-up', label: '发送问题', className: 'send-btn' };
}

/** 一轮流式问答的客户端状态。`text` 是临时正文，`answer` 是校验后的权威结果。 */
export function createRun() {
  return {
    status: 'running',
    startedAt: 0,
    endedAt: 0,
    stages: [],
    text: '',
    citations: [],
    answer: null,
    error: null,
    completed: false,
  };
}

/** 本轮实际耗时（客户端观测，含检索与生成），用于「用时 N 秒」。 */
export function durationText(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '';
  if (ms < 1000) return '用时 1 秒内';
  return `用时 ${Math.round(ms / 1000)} 秒`;
}

function closeEarlier(run, key) {
  // 阶段是按顺序推进的：后一个阶段一开始，前一个就必然已经结束。
  const position = STAGE_ORDER.indexOf(key);
  run.stages.forEach(item => {
    if (item.state === 'active' && STAGE_ORDER.indexOf(item.key) < position) {
      item.state = 'done';
      item.label = STAGE_DONE_LABEL[item.key] || item.label;
    }
  });
}

function stage(run, key) {
  const found = run.stages.find(item => item.key === key);
  if (found) return found;
  closeEarlier(run, key);
  const created = { key, label: STAGE_LABEL[key], detail: '', state: 'active' };
  run.stages.push(created);
  return created;
}

function finish(run, key, detail = '') {
  const found = stage(run, key);
  found.state = 'done';
  found.label = STAGE_DONE_LABEL[key] || found.label;
  found.detail = detail;
  closeEarlier(run, key);
  return found;
}

export function contextDetail(messages, limit) {
  if (!messages) return limit ? '本轮无历史消息' : '已关闭上下文';
  return `最近 ${messages} 条历史已加入上下文`;
}

/**
 * 归约一个事件。返回本次变化的类型，供调用方决定重绘范围：
 * `stage` 只更新阶段列表，`delta` 追加正文，`final` 用完成事件覆盖整轮。
 */
export function applyQaEvent(run, kind, data = {}) {
  switch (kind) {
    case 'run_started':
      run.status = 'running';
      run.startedAt = run.startedAt || Date.now();
      run.endedAt = 0;
      run.stages = [];
      run.text = '';
      run.citations = [];
      run.answer = null;
      run.error = null;
      run.completed = false;
      stage(run, 'context');
      return 'stage';
    case 'context_started':
      stage(run, 'context');
      return 'stage';
    case 'context_completed':
      finish(run, 'context', contextDetail(data.messages, data.limit));
      return 'stage';
    case 'query_rewrite_started':
      stage(run, 'rewrite');
      return 'stage';
    case 'query_rewrite_completed':
      finish(run, 'rewrite', data.rewritten ? '已结合上文补全检索问题' : '按当前问题检索');
      return 'stage';
    case 'retrieval_started':
      stage(run, 'retrieval');
      return 'stage';
    case 'retrieval_completed':
      finish(run, 'retrieval', data.hits > 0 ? `命中 ${data.hits} 个片段` : '未命中相关片段');
      return 'stage';
    case 'generation_started':
      stage(run, 'generation');
      return 'stage';
    case 'answer_delta':
      if (typeof data.text === 'string' && data.text) {
        run.text += data.text;
        return 'delta';
      }
      return null;
    case 'citations': {
      run.citations = Array.isArray(data.citations) ? data.citations : [];
      const count = run.citations.length;
      // 服务端确实完成了引用校验：这是独立阶段，不能并入"生成回答"。
      finish(run, 'citations', count ? `${count} 条引用通过校验` : '未返回引用');
      return 'stage';
    }
    case 'completed':
      run.answer = {
        answer_status: data.answer_status || '',
        answer: typeof data.answer === 'string' ? data.answer : run.text,
        citations: run.citations,
        retrieval: {
          hits: data.hits ?? 0,
          mode: 'hybrid',
          degraded: Boolean(data.degraded),
          context_messages: data.context_messages ?? 0,
          query_rewritten: Boolean(data.query_rewritten),
        },
        warnings: Array.isArray(data.warnings) ? data.warnings : [],
      };
      // 以服务端校验后的正文覆盖流式期间显示的临时文本，标记与无依据段落已被处理。
      run.text = run.answer.answer;
      run.completed = true;
      run.status = 'completed';
      run.endedAt = Date.now();
      run.stages.forEach(item => { if (item.state === 'active') item.state = 'done'; });
      return 'final';
    case 'error': {
      run.error = new ApiError(data.code || 'INTERNAL_ERROR');
      run.status = 'error';
      run.endedAt = Date.now();
      run.stages.forEach(item => { if (item.state === 'active') item.state = 'failed'; });
      return 'error';
    }
    default:
      // 未知事件按扩展处理：忽略载荷但不打断本轮，避免新增事件导致旧前端崩掉。
      return null;
  }
}

/** 用户主动停止：保留已到达的正文与阶段，但状态是"已停止"，不是完成也不是失败。 */
export function stopRun(run) {
  run.status = 'stopped';
  run.endedAt = Date.now();
  run.stages.forEach(item => {
    if (item.state === 'active') item.state = 'stopped';
  });
  return run;
}

/**
 * 发送问题并消费事件流。连接中断、协议错误与后端 error 事件都落成 run.error，
 * 由调用方统一展示；AbortSignal 用于「停止生成」。
 */
export async function streamAnswer(api, body, { signal, onEvent } = {}) {
  const run = createRun();
  let handle = null;
  try {
    handle = await api.stream('/qa', { method: 'POST', body: { ...body, stream: true }, signal });
    await readEvents(handle.response.body, (kind, data) => {
      const change = applyQaEvent(run, kind, data);
      if (change && onEvent) onEvent(change, kind, data, run);
    });
    if (!run.completed && !run.error) {
      if (signal?.aborted) stopRun(run);
      else run.error = new ApiError('STREAM_INTERRUPTED');
    }
  } catch (error) {
    if (signal?.aborted) {
      // 停止是用户意图，不记成失败：只把未完成的阶段标成 stopped。
      stopRun(run);
    } else {
      run.error = error instanceof ApiError ? error : new ApiError('STREAM_INTERRUPTED');
      run.status = 'error';
      run.stages.forEach(item => { if (item.state === 'active') item.state = 'failed'; });
    }
    if (onEvent) onEvent(run.status === 'stopped' ? 'stopped' : 'error', 'abort', {}, run);
  } finally {
    if (handle) handle.release();
  }
  return run;
}
