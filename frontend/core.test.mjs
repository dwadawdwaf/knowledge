import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiClient, ApiError, reindexTarget, jobLabel, locatorText } from './core.mjs';

test('未连接不能访问接口', async () => {
  const api = new ApiClient(() => assert.fail('不得发送请求'));
  await assert.rejects(api.request('/knowledge-bases'), error => error.status === 401);
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
