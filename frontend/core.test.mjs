import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiClient, ApiError, reindexTarget, jobLabel, locatorText, chunkMetrics, warningLabel,
  readableSize, mediaLabel, parserLabel, groupByPage, splitHighlight, sharedWarnings,
  visibleWarnings,
  turnsFromMessages, uploadForm, PARSER_OPTIONS } from './core.mjs';

test('跨页片段展示全部来源页，不冒充单一字符区间', () => {
  const locator = { type: 'pdf', precision: 'composite', page: 12, pages: [12, 13], sources: [
    { chunk_start: 0, chunk_end: 5, locator: { page: 12, char_start: 100, char_end: 105 } },
    { chunk_start: 6, chunk_end: 9, locator: { page: 13, char_start: 0, char_end: 3 } },
  ] };
  assert.equal(locatorText(locator), '第 12、13 页 · 2 处原文');
  const metrics = chunkMetrics([{ ordinal: 0, text: '跨页自制片段', locator }]);
  assert.deepEqual(metrics.pages, [12, 13]);
  assert.equal(metrics.usable, false);
  assert.equal(locatorText({ type: 'pdf', page: 13, precision: 'page', char_start: 0, char_end: 8 }), '第 13 页 · 块内 [0, 8)');
  assert.equal(locatorText({ type: 'pdf', page: 13, pdf_column: 'left' }), '第 13 页 · 左栏');
  assert.equal(locatorText({ type: 'pdf', page: 13, pdf_column: 'right' }), '第 13 页 · 右栏');
});

test('上传和新版本显式选择解析器，默认请求不偷偷改策略', () => {
  const file = new Blob(['synthetic']);
  for (const [profile] of PARSER_OPTIONS) {
    const form = uploadForm(file, profile);
    assert.equal(form.get('parser_profile'), profile || null);
    assert.ok(form.get('file'));
  }
  assert.throws(() => uploadForm(file, 'unknown'));
  assert.equal(parserLabel('mineru-api4.1-v1', 'text/csv'), 'MinerU 文档解析');
  assert.equal(parserLabel('legacy-v1', 'application/pdf'), '');
});

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
test('复合分块的原文间隙不计入覆盖或重叠', () => {
  const char = (char_start, char_end) => ({ precision: 'char', char_start, char_end });
  const metrics = chunkMetrics([
    { ordinal: 0, text: '甲乙', locator: { precision: 'composite', sources: [
      { locator: char(0, 1) }, { locator: char(5, 6) },
    ] } },
    { ordinal: 1, text: '丙丁乙', locator: char(3, 6) },
  ]);
  assert.equal(metrics.usable, true);
  assert.deepEqual(metrics.segments[0].ranges, [[0, 1], [5, 6]]);
  assert.equal(metrics.overlaps, 1);
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

test('块内偏移不被说成文档字符位置', () => {
  const block = locatorText({ page: 2, char_start: 0, char_end: 20, precision: 'block' });
  assert.equal(block, '第 2 页 · 块内 [0, 20)');
  const markdown = locatorText({ char_start: 76, char_end: 119, precision: 'char' });
  assert.equal(markdown, '字符 [76, 119)');
  const docx = locatorText({ paragraph: 0, char_start: 0, char_end: 8, precision: 'block' });
  assert.equal(docx, '段落索引 0 · 段落内 [0, 8)');
});
test('未知警告码原样显示，不隐藏', () => {
  assert.equal(warningLabel('mineru_table_linearized'), 'MinerU 表格/HTML已转纯文本');
  assert.match(warningLabel('mineru_nul_replaced'), /NUL.*需复核/);
  assert.equal(warningLabel('something_new'), 'something_new');
});

test('块内偏移不按文档比例排列，避免给出错误的覆盖关系', () => {
  const metrics = chunkMetrics([
    { ordinal: 0, text: 'a'.repeat(17), locator: { char_start: 0, char_end: 17, precision: 'block', page: 1 } },
    { ordinal: 1, text: 'b'.repeat(74), locator: { char_start: 0, char_end: 74, precision: 'block', page: 1 } },
  ]);
  assert.equal(metrics.usable, false);
  assert.equal(metrics.blockPrecision, true);
  assert.equal(metrics.segments[0].width, 49.5);
  assert.equal(metrics.segments[1].left, 50);
});

test('体积与格式标签按人读的单位呈现', () => {
  assert.equal(readableSize(0), '—');
  assert.equal(readableSize(900), '900 B');
  assert.equal(readableSize(113126), '110 KB');
  assert.equal(readableSize(2.4 * 1024 * 1024), '2.4 MB');
  assert.equal(mediaLabel('application/pdf'), 'PDF');
  assert.equal(mediaLabel('application/vnd.openxmlformats-officedocument.wordprocessingml.document'), 'DOCX');
  assert.equal(mediaLabel('application/vnd.openxmlformats-officedocument.presentationml.presentation'), 'PPTX');
  assert.equal(mediaLabel('text/csv'), 'CSV');
  assert.equal(mediaLabel(null), '未知格式');
  assert.equal(parserLabel('mineru-api4.1-v1', 'application/pdf'), 'MinerU 文档解析');
  assert.equal(parserLabel('legacy-v1', 'application/pdf'), '');
  assert.equal(parserLabel(null), '');
});
test('按页分组保持顺序，无页码单独成组', () => {
  const groups = groupByPage([
    { ordinal: 0, locator: { page: 1 } },
    { ordinal: 1, locator: { page: 1 } },
    { ordinal: 2, locator: { page: 2 } },
  ]);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(group => group.page), [1, 2]);
  assert.equal(groups[0].items.length, 2);
  const plain = groupByPage([{ ordinal: 0, locator: {} }, { ordinal: 1, locator: {} }]);
  assert.equal(plain.length, 1);
  assert.equal(plain[0].page, null);
  assert.deepEqual(groupByPage([]), []);
});
test('关键词切片不返回 HTML，且大小写不敏感', () => {
  const parts = splitHighlight('UPS 电量偏低', 'ups');
  assert.deepEqual(parts, [{ text: 'UPS', hit: true }, { text: ' 电量偏低', hit: false }]);
  const none = splitHighlight('正文', '');
  assert.deepEqual(none, [{ text: '正文', hit: false }]);
  const tags = splitHighlight('<img src=x onerror=alert(1)>', 'img');
  assert.ok(tags.every(part => typeof part.text === 'string' && !('html' in part)));
  assert.equal(tags.filter(part => part.hit).length, 1);
});

test('共有警告只上提一次，块内保留特有条目', () => {
  const shared = sharedWarnings([
    { warnings: ['mineru_table_linearized', 'mineru_requires_review'] },
    { warnings: ['mineru_table_linearized'] },
  ]);
  assert.deepEqual(shared, ['mineru_table_linearized']);
  assert.deepEqual(sharedWarnings([{ warnings: [] }, { warnings: ['x'] }]), []);
  assert.deepEqual(sharedWarnings([]), []);
});

test('解析实现细节的警告码不再展示给使用者', () => {
  assert.deepEqual(visibleWarnings([
    'mineru_requires_review', 'mineru_geometry_unavailable',
    'mineru_native_location_unavailable', 'mineru_table_linearized',
  ]), ['mineru_table_linearized']);
  assert.deepEqual(visibleWarnings(), []);
  assert.deepEqual(sharedWarnings([{ warnings: ['mineru_requires_review'] }]), []);
});

test('取文件字节时 404 仍抛统一错误，不把 JSON 当文件', async () => {
  const api = new ApiClient(async () => Response.json(
    { error: { code: 'RESOURCE_NOT_FOUND' }, meta: { request_id: 'r' } }, { status: 404 }));
  await assert.rejects(api.raw('/documents/x/file'), error => error.status === 404 && error.code === 'RESOURCE_NOT_FOUND');
});
test('取文件字节成功时返回原始响应', async () => {
  const api = new ApiClient(async () => new Response(new Blob(['%PDF-1.4']), { status: 200 }));
  const response = await api.raw('/documents/x/file');
  assert.equal(response.status, 200);
  assert.equal((await response.blob()).size, 8);
});

test('表格按工作表与行定位，不冒充字符区间', () => {
  const text = locatorText({
    type: 'xlsx', sheet: '资产清单', row_start: 1, row_end: 9,
    char_start: 0, char_end: 307, precision: 'sheet',
  });
  assert.equal(text, '工作表《资产清单》 · 第 1–9 行');
  assert.equal(mediaLabel('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), 'XLSX');
  assert.equal(warningLabel('xlsx_rows_truncated'), '表格行数超上限，仅索引前若干行');
  assert.equal(warningLabel('csv_rows_truncated'), '表格行数超上限，仅索引前若干行');
  assert.equal(locatorText({ type: 'pptx', page: 2 }), '第 2 张幻灯片');
});

test('服务端消息还原为对话轮次，未配对的回答不冒充成功', () => {
  const turns = turnsFromMessages([
    { id: 'u1', role: 'user', body: '问题一', created_at: '2026-10-28T10:00:00+08:00' },
    {
      id: 'a1', role: 'assistant', body: '回答一', answer_status: 'answered',
      citations: [{ index: 1, chunk_id: 'c1', title: '资料' }],
      created_at: '2026-10-28T10:00:01+08:00',
    },
    { id: 'u2', role: 'user', body: '问题二', created_at: '2026-10-28T10:01:00+08:00' },
  ]);
  assert.equal(turns.length, 2);
  assert.equal(turns[0].question, '问题一');
  assert.equal(turns[0].answer.answer, '回答一');
  assert.equal(turns[0].answer.citations[0].chunk_id, 'c1');
  assert.equal(turns[1].status, 'error');
});
