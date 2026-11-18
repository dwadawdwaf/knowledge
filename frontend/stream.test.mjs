import test from 'node:test';
import assert from 'node:assert/strict';
import { applyQaEvent, createRun, contextDetail, streamAnswer, stopRun, sendButtonView, EXECUTION_STATE } from './stream.mjs';

function sse(frames) {
  const bytes = new TextEncoder().encode(frames);
  return new ReadableStream({
    start(controller) {
      controller.enqueue(bytes);
      controller.close();
    },
  });
}

function fakeApi(chunks) {
  return {
    async stream() {
      const body = new ReadableStream({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
          controller.close();
        },
      });
      return { response: { body }, release: () => { released += 1; } };
    },
  };
}

let released = 0;

test('阶段事件按顺序推进并带上完成说明', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  applyQaEvent(run, 'context_completed', { messages: 5, limit: 12 });
  applyQaEvent(run, 'query_rewrite_completed', { rewritten: true });
  applyQaEvent(run, 'retrieval_completed', { hits: 3 });

  assert.deepEqual(run.stages.map(item => item.state), ['done', 'done', 'done']);
  assert.equal(run.stages[0].detail, '最近 5 条历史已加入上下文');
  assert.equal(run.stages[1].detail, '已结合上文补全检索问题');
  assert.equal(run.stages[2].detail, '命中 3 个片段');
  assert.equal(run.stages[2].label, '知识库检索');
});

test('上一阶段未显式结束也会被下一阶段标记完成', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  applyQaEvent(run, 'retrieval_started', {});
  applyQaEvent(run, 'generation_started', {});
  assert.deepEqual(
    run.stages.map(item => [item.key, item.state]),
    [['context', 'done'], ['retrieval', 'done'], ['generation', 'active']],
  );
  assert.equal(run.stages[0].label, '会话上下文');
});

test('正文增量累积，完成事件用校验后的答案覆盖', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  assert.equal(applyQaEvent(run, 'answer_delta', { text: '第一段' }), 'delta');
  applyQaEvent(run, 'answer_delta', { text: '，第二段' });
  assert.equal(run.text, '第一段，第二段');

  applyQaEvent(run, 'citations', { citations: [{ index: 1, chunk_id: 'c1' }] });
  const change = applyQaEvent(run, 'completed', {
    answer_status: 'answered',
    answer: '校验后正文[1]',
    hits: 4,
    context_messages: 6,
    warnings: [],
  });
  assert.equal(change, 'final');
  assert.equal(run.text, '校验后正文[1]');
  assert.equal(run.answer.citations.length, 1);
  assert.equal(run.answer.retrieval.hits, 4);
  assert.equal(run.answer.retrieval.context_messages, 6);
  assert.equal(run.completed, true);
});

test('错误事件保留阶段并把当前阶段标为失败', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  applyQaEvent(run, 'generation_started', {});
  applyQaEvent(run, 'error', { code: 'MODEL_UNAVAILABLE', message: '模型不可用' });
  assert.equal(run.error.code, 'MODEL_UNAVAILABLE');
  assert.equal(run.error.message, '模型服务暂不可用，未返回答案。');
  assert.equal(run.stages.at(-1).state, 'failed');
  assert.equal(run.completed, false);
});

test('未知事件被忽略，不影响本轮状态', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  assert.equal(applyQaEvent(run, 'future_event', { value: 1 }), null);
  assert.equal(run.text, '');
});

test('无历史与关闭上下文的说明不同', () => {
  assert.equal(contextDetail(0, 12), '本轮无历史消息');
  assert.equal(contextDetail(0, 0), '已关闭上下文');
});

test('流式消费：事件驱动状态并在结束时释放响应', async () => {
  released = 0;
  const frames = [
    'event: run_started\ndata: {"run_id":"r1"}\n\n',
    'event: context_completed\ndata: {"messages":2,"limit":12}\n\n',
    'event: generation_started\ndata: {}\n\n',
    'event: answer_delta\ndata: {"text":"答"}\n\n',
    'event: answer_delta\ndata: {"text":"案"}\n\n',
    'event: completed\ndata: {"answer_status":"answered","answer":"答案","hits":1}\n\n',
  ];
  const seen = [];
  const run = await streamAnswer(fakeApi(frames), { question: 'q' }, {
    onEvent: change => seen.push(change),
  });
  assert.equal(run.text, '答案');
  assert.equal(run.completed, true);
  assert.equal(run.answer.answer_status, 'answered');
  assert.deepEqual(seen, ['stage', 'stage', 'stage', 'delta', 'delta', 'final']);
  assert.equal(released, 1);
});

test('流在完成事件前结束时报连接中断，不把半段正文当成答案', async () => {
  released = 0;
  const frames = [
    'event: run_started\ndata: {}\n\n',
    'event: answer_delta\ndata: {"text":"半段"}\n\n',
  ];
  const run = await streamAnswer(fakeApi(frames), { question: 'q' });
  assert.equal(run.completed, false);
  assert.equal(run.error.code, 'STREAM_INTERRUPTED');
  assert.equal(released, 1);
});

test('主动停止：状态是 stopped，保留已到达的正文，不记成失败', async () => {
  released = 0;
  const controller = new AbortController();
  let served = false;
  const api = {
    async stream() {
      // 先投递一批事件，下一次取数据时模拟客户端中断（与 abort 后的 read 拒绝一致）。
      const body = new ReadableStream({
        pull(ctrl) {
          if (!served) {
            served = true;
            ctrl.enqueue(new TextEncoder().encode(
              'event: run_started\ndata: {}\n\n'
              + 'event: generation_started\ndata: {}\n\n'
              + 'event: answer_delta\ndata: {"text":"半段正文"}\n\n',
            ));
            return;
          }
          controller.abort();
          ctrl.error(new Error('aborted'));
        },
      });
      return { response: { body }, release: () => { released += 1; } };
    },
  };
  const run = await streamAnswer(api, { question: 'q' }, { signal: controller.signal });
  assert.equal(run.status, 'stopped');
  assert.equal(run.error, null);
  assert.equal(run.completed, false);
  assert.equal(run.text, '半段正文');
  assert.equal(run.stages.find(item => item.key === 'generation').state, 'stopped');
  assert.equal(released, 1);
});

test('引用校验是独立阶段，生成阶段在此之前已结束', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  applyQaEvent(run, 'generation_started', {});
  applyQaEvent(run, 'citations', { citations: [{ index: 1 }, { index: 2 }] });

  const keys = run.stages.map(item => item.key);
  assert.deepEqual(keys, ['context', 'generation', 'citations']);
  assert.equal(run.stages[1].state, 'done');
  assert.equal(run.stages[1].label, '生成回答');
  assert.equal(run.stages[2].label, '引用校验');
  assert.equal(run.stages[2].detail, '2 条引用通过校验');
});

test('完成后保留全部执行阶段，状态为 completed', () => {
  const run = createRun();
  for (const event of ['run_started', 'context_completed', 'query_rewrite_completed',
    'retrieval_completed', 'generation_started', 'citations']) {
    applyQaEvent(run, event, {});
  }
  applyQaEvent(run, 'completed', { answer_status: 'answered', answer: '最终答案' });

  assert.equal(run.status, 'completed');
  assert.deepEqual(run.stages.map(item => item.key),
    ['context', 'rewrite', 'retrieval', 'generation', 'citations']);
  assert.ok(run.stages.every(item => item.state === 'done'));
  // 完成事件不会清空阶段：结束后仍可回看
  assert.equal(run.stages.length, 5);
});

test('stopRun 把进行中的阶段标为 stopped，不动已完成的阶段', () => {
  const run = createRun();
  applyQaEvent(run, 'run_started', {});
  applyQaEvent(run, 'retrieval_completed', { hits: 2 });
  applyQaEvent(run, 'generation_started', {});
  stopRun(run);

  assert.equal(run.status, 'stopped');
  assert.deepEqual(run.stages.map(item => item.state), ['done', 'done', 'stopped']);
});

test('多轮执行状态互不覆盖', () => {
  const first = createRun();
  applyQaEvent(first, 'run_started', {});
  applyQaEvent(first, 'retrieval_completed', { hits: 3 });
  applyQaEvent(first, 'completed', { answer_status: 'answered', answer: '第一轮' });

  const second = createRun();
  applyQaEvent(second, 'run_started', {});
  applyQaEvent(second, 'generation_started', {});

  assert.equal(first.stages.length, 2);
  assert.equal(first.status, 'completed');
  assert.equal(second.stages.length, 2);
  assert.equal(second.status, 'running');
  assert.notEqual(first.stages, second.stages);
});

test('发送按钮只有一个：空闲是发送，生成中是停止', () => {
  assert.deepEqual(sendButtonView(false), {
    icon: 'arrow-up', label: '发送问题', className: 'send-btn',
  });
  assert.deepEqual(sendButtonView(true), {
    icon: 'stop', label: '停止生成', className: 'send-btn is-stop',
  });
  // 两种状态复用同一个 class 基础名，说明是同一个按钮换图标而不是新增按钮
  assert.ok(sendButtonView(true).className.startsWith(sendButtonView(false).className));
});

test('执行状态文案覆盖运行、完成、停止与失败四种', () => {
  assert.deepEqual(Object.keys(EXECUTION_STATE), ['running', 'completed', 'stopped', 'error']);
  assert.equal(EXECUTION_STATE.completed.label, '回答完成');
  assert.equal(EXECUTION_STATE.stopped.label, '已停止');
  assert.equal(EXECUTION_STATE.error.label, '执行失败');
});
