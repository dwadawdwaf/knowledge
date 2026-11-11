import test from 'node:test';
import assert from 'node:assert/strict';
import { createDocumentData } from './documents.mjs';

function setup(request) {
  globalThis.document = { hidden: false, activeElement: null, querySelector: () => null };
  const state = { connected: true, page: 'knowledge', library: { id: 'kb' }, libraries: [{ id: 'kb' }], documents: [], allFiles: [], fileErrors: [] };
  const api = { revision: 1, request };
  let draws = 0;
  const data = createDocumentData({ api, state, render() {}, drawDocuments() { draws += 1; }, drawFiles() { draws += 1; }, notify() {}, errorText: e => e.message, expired() {} });
  return { api, state, data, draws: () => draws };
}

test('空列表失败后结束加载，显示错误', async () => {
  const { state, data } = setup(async () => { throw new Error('synthetic failure'); });
  await data.loadDocuments();
  assert.equal(state.docLoading, false);
  assert.equal(state.docError.message, 'synthetic failure');
});

test('重复读取合并，切换页面后旧响应不会写回', async () => {
  let finish;
  let calls = 0;
  const { data, state } = setup(() => { calls += 1; return new Promise(resolve => { finish = resolve; }); });
  const first = data.loadDocuments();
  const second = data.loadDocuments();
  assert.equal(calls, 1);
  data.stopUpdates();
  state.library = { id: 'other' };
  finish({ items: [{ id: 'old', status: 'active' }] });
  await Promise.all([first, second]);
  assert.deepEqual(state.documents, []);
  assert.equal(state.docLoading, false);
});

test('全部文件刷新先更新知识库集合，并在断开身份后丢弃结果', async () => {
  let finish;
  const { data, state, api } = setup(async path => {
    if (path === '/knowledge-bases') return { items: [{ id: 'new', name: '新增库', status: 'active' }] };
    return new Promise(resolve => { finish = resolve; });
  });
  const pending = data.loadAllFiles();
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  assert.equal(state.libraries[0].id, 'new');
  data.stopUpdates(); api.revision += 1;
  finish({ items: [{ id: 'old', status: 'active' }] });
  await pending;
  assert.deepEqual(state.allFiles, []);
});

test('全部文件汇总限制并发且保留每个库的结果', async () => {
  let active = 0;
  let peak = 0;
  const libraries = Array.from({ length: 9 }, (_, id) => ({ id: String(id), name: String(id), status: 'active' }));
  const { data, state } = setup(async path => {
    if (path === '/knowledge-bases') return { items: libraries };
    active += 1;
    peak = Math.max(active, peak);
    await new Promise(resolve => setImmediate(resolve));
    active -= 1;
    return { items: [{ id: path, status: 'active' }] };
  });
  await data.loadAllFiles();
  assert.equal(peak, 4);
  assert.equal(state.allFiles.length, 9);
  assert.equal(state.fileLoading, false);
  assert.deepEqual(state.fileErrors, []);
});

test('旧快照读取期间收到事件，会在旧请求结束后再取新状态', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let finish;
  let stream;
  let reads = 0;
  const { data, state, api } = setup(async path => {
    if (path === '/knowledge-bases') return { items: [{ id: 'kb', status: 'active' }] };
    reads += 1;
    if (reads === 1) return new Promise(resolve => { finish = resolve; });
    return { items: [{ id: 'new', status: 'active' }] };
  });
  api.token = 'synthetic';
  api.fetcher = async (_, options) => new Response(new ReadableStream({ start(controller) {
    stream = controller;
    options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true });
  } }), { headers: { 'Content-Type': 'text/event-stream' } });
  t.after(() => data.stopUpdates());
  state.page = 'files';
  data.updateSubscription();
  const manual = data.loadAllFiles();
  while (!finish || !stream) await new Promise(resolve => setImmediate(resolve));
  stream.enqueue(new TextEncoder().encode('event: change\ndata: {"version":1}\n\n'));
  await new Promise(resolve => setImmediate(resolve));
  t.mock.timers.tick(120);
  finish({ items: [{ id: 'old', status: 'active' }] });
  await manual;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads, 2);
  assert.equal(state.allFiles[0].id, 'new');
});

test('隐藏与离开资料页关闭连接，恢复可见时只有一个新连接', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const { data, api } = setup(async () => ({ items: [] }));
  const signals = [];
  api.token = 'synthetic';
  api.fetcher = async (_, options) => {
    signals.push(options.signal);
    return new Response(new ReadableStream({ start(controller) {
      options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true });
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  };
  t.after(() => data.stopUpdates());
  data.updateSubscription(); data.updateSubscription();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(signals.length, 1);
  document.hidden = true; data.updateSubscription();
  assert.equal(signals[0].aborted, true);
  document.hidden = false; data.updateSubscription();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(signals.length, 2);
});
