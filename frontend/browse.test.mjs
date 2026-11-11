import test from 'node:test';
import assert from 'node:assert/strict';
import { emptyBrowse, loadBrowse, loadOriginal, disposeBrowse, downloadOriginal } from './browse.mjs';

test('退出浏览后迟到的详情不会重绘其他页面', async () => {
  globalThis.document = { querySelector: () => null };
  const finish = [];
  let renders = 0;
  const browse = { ...emptyBrowse(), doc: { id: 'd' } };
  const app = { state: { page: 'browse', browse }, render() { renders += 1; }, api: { request: () => new Promise(resolve => finish.push(resolve)) } };
  const pending = loadBrowse(app);
  disposeBrowse(app);
  app.state.page = 'files'; app.state.browse = emptyBrowse();
  finish[0]({ media_type: 'text/plain' }); finish[1]({ items: [] });
  await pending;
  assert.equal(renders, 0);
});

test('离开文件后原件字节返回不会创建 Blob URL', async t => {
  globalThis.document = { querySelector: () => null };
  let finish;
  const allocated = t.mock.method(URL, 'createObjectURL', () => 'blob:synthetic');
  const browse = { ...emptyBrowse(), doc: { id: 'd' }, detail: { media_type: 'application/pdf' } };
  const app = { state: { page: 'browse', browse }, render() { assert.fail('不应重绘'); }, api: { raw: async () => ({ headers: new Headers({ 'content-type': 'application/pdf' }), blob: () => new Promise(resolve => { finish = resolve; }) }) } };
  const pending = loadOriginal(app);
  while (!finish) await new Promise(resolve => setImmediate(resolve));
  disposeBrowse(app); app.state.page = 'files';
  finish(new Blob(['synthetic']));
  await pending;
  assert.equal(allocated.mock.callCount(), 0);
});

test('TIFF 下载读取原件，独立于已经转为 PNG 的预览', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let endpoint;
  let clicked = false;
  const link = { click() { clicked = true; }, remove() {} };
  globalThis.document = { querySelector: () => null, createElement: () => link, body: { append() {} } };
  const allocated = t.mock.method(URL, 'createObjectURL', () => 'blob:original');
  const revoked = t.mock.method(URL, 'revokeObjectURL', () => {});
  const browse = { ...emptyBrowse(), doc: { id: 'tiff', title: 'synthetic.tiff' } };
  browse.original.url = 'blob:png-preview';
  const app = { state: { page: 'browse', browse }, api: { raw: async path => {
    endpoint = path;
    return new Response('synthetic-tiff', { headers: { 'content-type': 'image/tiff' } });
  } } };
  await downloadOriginal(app);
  assert.equal(endpoint, '/documents/tiff/file');
  assert.equal(allocated.mock.calls[0].arguments[0].type, 'image/tiff');
  assert.equal(link.download, 'synthetic.tiff');
  assert.equal(clicked, true);
  t.mock.timers.tick(1000);
  assert.equal(revoked.mock.callCount(), 1);
  assert.deepEqual(browse.downloadUrls, []);
});
