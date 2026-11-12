import test from 'node:test';
import assert from 'node:assert/strict';
import { setImmediate } from 'node:timers/promises';
import { DocumentEvents, readEvents } from './events.mjs';

test('SSE 支持 UTF-8 分片、CRLF、多行数据和连续事件', async () => {
  const bytes = new TextEncoder().encode('event: sync\r\ndata: {"version":1,\r\ndata: "text":"中文"}\r\n\r\nevent: change\ndata: {"version":1}\n\n');
  const body = new ReadableStream({ start(controller) {
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  } });
  const seen = [];
  await readEvents(body, (kind, data) => seen.push([kind, data]));
  assert.deepEqual(seen, [['sync', { version: 1, text: '中文' }], ['change', { version: 1 }]]);
});

test('过大的 SSE 数据会终止读取并释放流', async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: ' + 'a'.repeat(17000))); },
    cancel() { cancelled = true; },
  });
  await assert.rejects(readEvents(body, () => {}), /上限/);
  assert.equal(cancelled, true);
});

test('重复启动仅有一条连接，停止会取消读取，令牌只进请求头', async () => {
  const calls = [];
  let notify;
  const changed = new Promise(resolve => { notify = resolve; });
  const api = { token: 'synthetic-token', revision: 1, fetcher: async (url, options) => {
    calls.push({ url, options });
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('event: sync\ndata: {"version":1}\n\n'));
      options.signal.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true });
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  } };
  const events = new DocumentEvents(api, { onChange: notify, onStatus: () => {} });
  events.start(); events.start();
  await changed;
  events.stop();
  await setImmediate();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/v1/events');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-token');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.signal.aborted, true);
});

test('断线退避重连，停止后没有延迟重连', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let calls = 0;
  const api = { revision: 1, token: 'synthetic', fetcher: async () => {
    calls += 1;
    throw new Error('synthetic outage');
  } };
  const events = new DocumentEvents(api, { onChange: () => {}, onStatus: () => {} });
  events.start();
  await setImmediate();
  assert.equal(calls, 1);
  t.mock.timers.tick(1000);
  await setImmediate();
  assert.equal(calls, 2);
  events.stop();
  t.mock.timers.tick(60000);
  await setImmediate();
  assert.equal(calls, 2);
});

test('401 结束连接并提示身份失效', async () => {
  let status;
  const api = { revision: 1, token: 'synthetic', fetcher: async () => new Response('', { status: 401 }) };
  const events = new DocumentEvents(api, { onChange: () => {}, onStatus: value => { status = value; } });
  events.start();
  await setImmediate();
  assert.equal(status, 'expired');
  assert.equal(events.running, false);
});
