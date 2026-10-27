// 协议与展示转换保持独立，复用浏览器 Fetch，不引入客户端框架或存储。
const messages = {
  UNAUTHENTICATED: '访问令牌无效或已过期，请重新连接。',
  RESOURCE_NOT_FOUND: '内容不存在、已删除，或当前身份无权访问。',
  VERSION_CONFLICT: '文档状态已变化或已有处理任务，请刷新后再操作。',
  SOURCE_CHANGED: '回答期间资料发生变化，请重新提问。',
  FILE_TOO_LARGE: '文件超过后端大小上限。',
  UNSUPPORTED_FORMAT: '不支持该文件，请使用 TXT、MD、文本 PDF 或 DOCX。',
  VALIDATION_ERROR: '请求参数不符合要求，请检查输入。',
  MODEL_UNAVAILABLE: '模型服务暂不可用，未返回答案。',
  MODEL_TIMEOUT: '模型服务超时，未返回答案。',
  CITATION_VALIDATION_FAILED: '答案未通过引用校验，未返回正文。这不是正常拒答；已知同主题无答案场景也可能触发此错误。',
};
export class ApiError extends Error {
  constructor(code, status = 0, requestId = '') {
    super(messages[code] || '服务请求失败，请检查后端状态。');
    this.code = code; this.status = status; this.requestId = requestId;
  }
}
export class ApiClient {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) { this.fetcher = fetcher; this.token = ''; }
  async request(path, { method = 'GET', body, timeout = 120000 } = {}) {
    if (!this.token) throw new ApiError('UNAUTHENTICATED', 401);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const token = this.token;
    try {
      const headers = { Authorization: `Bearer ${token}` };
      const isFile = body instanceof FormData;
      if (body !== undefined && !isFile) headers['Content-Type'] = 'application/json';
      const response = await this.fetcher(`/api/v1${path}`, { method, headers,
        body: body === undefined ? undefined : isFile ? body : JSON.stringify(body),
        signal: controller.signal, credentials: 'omit', cache: 'no-store' });
      let data;
      try { data = await response.json(); } catch { throw new ApiError('INVALID_RESPONSE', response.status); }
      if (this.token !== token) throw new ApiError('STALE_SESSION');
      if (!response.ok) throw new ApiError(data?.error?.code, response.status, data?.meta?.request_id || '');
      if (!data || !Object.hasOwn(data, 'data')) throw new ApiError('INVALID_RESPONSE', response.status);
      return data.data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError('NETWORK_ERROR');
    } finally { clearTimeout(timer); }
  }
}
export function reindexTarget(document, receipts) {
  if (receipts.has(document.id)) return receipts.get(document.id);
  if (!document.pending_build_id && document.job?.state === 'succeeded') return document.current_version_id;
  return null; // 不将旧发布版本误认为失败的新版本。
}
export function jobLabel(document) {
  const job = document.job;
  if (!job) return '暂无任务';
  const stages = { parsing: '正在解析', chunking: '正在分段', embedding: '正在向量化', publishing: '正在发布' };
  if (job.state === 'running') return stages[job.stage] || '处理中';
  return { queued: '排队中', failed: '处理失败', cancelled: '已取消', succeeded: '已就绪' }[job.state] || '未知状态';
}
export function locatorText(locator = {}) {
  const parts = [];
  if (locator.page != null) parts.push(`第 ${locator.page} 页`);
  if (locator.paragraph != null) parts.push(`段落索引 ${locator.paragraph}`);
  if (locator.char_start != null) parts.push(`字符 [${locator.char_start}, ${locator.char_end})`);
  return parts.join(' · ') || '原文分块';
}
// 分块视图只做度量与相对定位；缺字符偏移时退化为等宽，不伪造覆盖比例。
export function chunkMetrics(items = []) {
  const spans = items.map(item => {
    const locator = item.locator || {};
    const start = Number.isFinite(locator.char_start) ? locator.char_start : null;
    const end = Number.isFinite(locator.char_end) ? locator.char_end : null;
    return { ordinal: item.ordinal, chars: (item.text || '').length, start, end, page: locator.page ?? null };
  });
  const chars = spans.reduce((sum, span) => sum + span.chars, 0);
  const usable = spans.length > 0 && spans.every(span => span.start !== null && span.end !== null && span.end > span.start);
  const maxEnd = usable ? Math.max(...spans.map(span => span.end)) : 0;
  let overlaps = 0;
  for (let index = 1; index < spans.length; index += 1) {
    if (usable && spans[index].start < spans[index - 1].end) overlaps += spans[index - 1].end - spans[index].start;
  }
  const pages = [...new Set(spans.map(span => span.page).filter(page => page != null))].sort((left, right) => left - right);
  const step = spans.length ? 100 / spans.length : 0;
  const segments = spans.map((span, index) => {
    if (!usable) return { ordinal: span.ordinal, left: index * step, width: Math.max(step - 0.5, 1), start: null, end: null, chars: span.chars, page: span.page };
    const left = (span.start / maxEnd) * 100;
    const width = Math.max(((span.end - span.start) / maxEnd) * 100, 1.2);
    return { ordinal: span.ordinal, left, width, start: span.start, end: span.end, chars: span.chars, page: span.page };
  });
  return { count: spans.length, chars, avg: spans.length ? Math.round(chars / spans.length) : 0,
    usable, maxEnd, overlaps, pages, segments };
}
