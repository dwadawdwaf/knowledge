import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiClient, ApiError, reindexTarget, jobLabel, locatorText, chunkMetrics } from './core.mjs';

test('无令牌也发起请求，由服务端决定是否放行', async () => {
  let sent;
  const api = new ApiClient(async (_, options) => {
    sent = options.headers;
    return Response.json({ data: { items: [] } });
  });
  await api.request('/knowledge-bases');
  assert.equal(sent.Authorization, undefined);
});
test('服务端要求令牌时把 401 抛给页面', async () => {
  const api = new ApiClient(async () => Response.json({ error: { code: 'UNAUTHENTICATED' }, meta: { request_id: 'r' } }, { status: 401 }));
  await assert.rejects(api.request('/knowledge-bases'), error => error.status === 401 && error.code === 'UNAUTHENTICATED');
});
test('身份仅由 Authorization 传递，不使用 Cookie', async () => {
  const api = new ApiClient(async (url, options) => {
    assert.equal(url, '/api/v1/qa'); assert.equal(options.headers.Authorization, 'Bearer synthetic-token');
    assert.equal(options.credentials, 'omit'); assert.equal(options.cache, 'no-store');
    assert.deepEqual(JSON.parse(options.body), { question: '自制问题', knowledge_base_ids: ['kb'] });
    return Response.json({ data: { answer_status: 'no_evidence' } });
  }); api.token = 'synthetic-token';
  assert.equal((await api.request('/qa', { method: 'POST', body: { question: '自制问题', knowledge_base_ids: ['kb'] } })).answer_status, 'no_evidence');
});
test('引用失败保持错误，不伪装无证据或暴露原始错误', async () => {
  const api = new ApiClient(async () => Response.json({ error: { code: 'CITATION_VALIDATION_FAILED', message: 'private-key SQL /internal' }, meta: { request_id: 'test-rid' } }, { status: 502 }));
  api.token = 'synthetic';
  await assert.rejects(api.request('/qa'), error => error instanceof ApiError && error.status === 502 && !error.message.includes('private-key') && error.requestId === 'test-rid');
});
test('切换身份后丢弃旧响应', async () => {
  let resolve;
  const api = new ApiClient(() => new Promise(done => { resolve = done; }));
  api.token = 'old'; const pending = api.request('/knowledge-bases'); api.token = 'new';
  resolve(Response.json({ data: { items: [{ name: 'old private data' }] } }));
  await assert.rejects(pending, error => error.code === 'STALE_SESSION');
});
test('上传让浏览器自行生成 multipart boundary', async () => {
  const api = new ApiClient(async (_, options) => {
    assert.ok(options.body instanceof FormData); assert.equal(options.headers['Content-Type'], undefined);
    return Response.json({ data: { job_id: 'j' } });
  }); api.token = 'synthetic'; await api.request('/upload', { method: 'POST', body: new FormData() });
});
test('不能将失败新版本的旧发布版本用作重建目标', () => {
  const doc = { id: 'd', pending_build_id: 'failed-new', current_version_id: 'old', job: { state: 'failed' } };
  assert.equal(reindexTarget(doc, new Map()), null);
  assert.equal(reindexTarget(doc, new Map([['d', 'latest']])), 'latest');
  assert.equal(reindexTarget({ ...doc, pending_build_id: null, job: { state: 'succeeded' } }, new Map()), 'old');
});
test('处理状态和定位不虚构百分比', () => {
  assert.equal(jobLabel({ job: { state: 'running', stage: 'embedding' } }), '正在向量化');
  assert.equal(locatorText({ page: 2, char_start: 3, char_end: 9 }), '第 2 页 · 字符 [3, 9)');
});
test('分块度量按字符区间计算重叠与覆盖', () => {
  const items = [
    { ordinal: 0, text: 'a'.repeat(100), locator: { char_start: 0, char_end: 100, page: 1 } },
    { ordinal: 1, text: 'b'.repeat(80), locator: { char_start: 80, char_end: 160, page: 1 } },
    { ordinal: 2, text: 'c'.repeat(40), locator: { char_start: 160, char_end: 200, page: 2 } },
  ];
  const metrics = chunkMetrics(items);
  assert.equal(metrics.count, 3); assert.equal(metrics.chars, 220);
  assert.equal(metrics.avg, 73); assert.equal(metrics.usable, true);
  assert.equal(metrics.maxEnd, 200); assert.equal(metrics.overlaps, 20);
  assert.deepEqual(metrics.pages, [1, 2]);
  assert.equal(metrics.segments[0].left, 0); assert.equal(metrics.segments[0].width, 50);
  assert.equal(metrics.segments[1].left, 40);
});
test('缺字符偏移时退化为等宽且不声明重叠', () => {
  const metrics = chunkMetrics([
    { ordinal: 0, text: 'x', locator: { page: 1 } },
    { ordinal: 1, text: 'xx', locator: { page: 1 } },
  ]);
  assert.equal(metrics.usable, false); assert.equal(metrics.overlaps, 0);
  assert.equal(metrics.segments[1].left, 50);
});
test('空分块不产生除零或假装有内容', () => {
  const metrics = chunkMetrics([]);
  assert.equal(metrics.count, 0); assert.equal(metrics.avg, 0);
  assert.deepEqual(metrics.segments, []);
});
