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
    // 开发态服务端不校验令牌，无令牌也照常发起；需要令牌时由 401 引导用户连接。
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const token = this.token;
    try {
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
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

  /**
   * 取原始响应（文件字节用）。错误仍然按统一错误信封解析，
   * 避免把 404 的 JSON 当成文件内容。
   */
  async raw(path, { method = 'GET', timeout = 120000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const token = this.token;
    try {
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const response = await this.fetcher(`/api/v1${path}`, {
        method, headers, signal: controller.signal, credentials: 'omit', cache: 'no-store',
      });
      if (this.token !== token) throw new ApiError('STALE_SESSION');
      if (!response.ok) {
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        throw new ApiError(payload?.error?.code, response.status, payload?.meta?.request_id || '');
      }
      return response;
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
  if (locator.sheet) {
    const rows = locator.row_start != null && locator.row_end != null
      ? ` · 第 ${locator.row_start}–${locator.row_end} 行`
      : '';
    parts.push(`工作表《${locator.sheet}》${rows}`);
  }
  // 表格按工作表与行定位，块内字符偏移没有阅读意义，不展示。
  if (locator.char_start != null && locator.precision !== 'sheet') {
    const range = `[${locator.char_start}, ${locator.char_end})`;
    // DeepDoc 与 DOCX 只给块/段内偏移，标成“字符”会被误读成文档级位置。
    const scope = locator.precision !== 'block' ? '字符'
      : locator.paragraph != null ? '段落内' : '块内';
    parts.push(`${scope} ${range}`);
  }
  return parts.join(' · ') || '原文分块';
}

const WARNING_LABELS = {
  deepdoc_ocr_requires_review: 'OCR 识别，需复核',
  deepdoc_table_linearized: '表格已线性化',
  deepdoc_figures_not_described: '含未描述的图片',
  deepdoc_empty_page: '存在空白页',
  xlsx_rows_truncated: '表格行数超上限，仅索引前若干行',
};

export function warningLabel(code) {
  return WARNING_LABELS[code] || code;
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
  // 块内偏移（DeepDoc）每块都从 0 开始，按比例画会得出错误的覆盖关系；
  // 只有偏移在文档内单调递增时才认为可以等比例排列。
  const starts = spans.map(span => span.start);
  const monotonic = spans.every((span, index) => index === 0 || starts[index] > starts[index - 1]);
  const usable = spans.length > 0 && monotonic
    && spans.every(span => span.start !== null && span.end !== null && span.end > span.start);
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
  const blockPrecision = items.length > 0
    && items.every(item => (item.locator || {}).precision === 'block');
  return { count: spans.length, chars, avg: spans.length ? Math.round(chars / spans.length) : 0,
    usable, blockPrecision, maxEnd, overlaps, pages, segments };
}

/* ------------------------------------------------------------ 文件浏览 */

export function readableSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const MEDIA_LABELS = [
  ['application/pdf', 'PDF'],
  ['image/png', 'PNG'],
  ['image/jpeg', 'JPEG'],
  ['image/tiff', 'TIFF'],
  ['application/vnd.openxmlformats-officedocument.wordprocessingml', 'DOCX'],
  ['application/vnd.openxmlformats-officedocument.spreadsheetml', 'XLSX'],
  ['text/markdown', 'Markdown'],
  ['text/plain', 'TXT'],
];

export function mediaLabel(mediaType) {
  if (!mediaType) return '未知格式';
  const found = MEDIA_LABELS.find(([prefix]) => mediaType.startsWith(prefix));
  if (found) return found[1];
  return mediaType.split('/').pop().toUpperCase();
}

const DEEPDOC_MEDIA = ['application/pdf', 'image/png', 'image/jpeg', 'image/tiff'];

/**
 * 解析器标签。DeepDoc 只处理 PDF 与图片，DOCX / Markdown 会回落到旧解析器；
 * 构建记录里存的是「配置的策略」，直接照搬会把 DOCX 说成 DeepDoc 解析。
 */
export function parserLabel(profile, mediaType) {
  if (!profile) return '未记录';
  if (profile.startsWith('deepdoc')) {
    if (!mediaType || DEEPDOC_MEDIA.some(prefix => mediaType.startsWith(prefix))) {
      return 'DeepDoc 版面解析';
    }
    return '旧解析器（该格式不走 DeepDoc）';
  }
  return '旧解析器';
}

/** 所有块共有的警告，用于提到文档级只提示一次。 */
export function sharedWarnings(items = []) {
  if (!items.length) return [];
  const [first, ...rest] = items.map(item => new Set(item.warnings || []));
  return [...first].filter(code => rest.every(set => set.has(code))).sort();
}

/**
 * 按页分组，保持原顺序。无页码的分块（DOCX、Markdown）单独成组，
 * 不伪造成第 1 页。
 */
export function groupByPage(items = []) {
  const groups = [];
  items.forEach(item => {
    const page = (item.locator || {}).page ?? null;
    const last = groups[groups.length - 1];
    if (last && last.page === page) last.items.push(item);
    else groups.push({ page, items: [item] });
  });
  return groups;
}

/**
 * 把正文按关键词切成「命中/普通」片段，供界面用 DOM 高亮。
 * 不返回 HTML，避免把文档内容当标签解析。
 */
export function splitHighlight(text, query) {
  const source = text || '';
  const keyword = (query || '').trim();
  if (!keyword) return [{ text: source, hit: false }];
  const parts = [];
  const lower = source.toLowerCase();
  const needle = keyword.toLowerCase();
  let cursor = 0;
  let index = lower.indexOf(needle);
  while (index !== -1) {
    if (index > cursor) parts.push({ text: source.slice(cursor, index), hit: false });
    parts.push({ text: source.slice(index, index + keyword.length), hit: true });
    cursor = index + keyword.length;
    index = lower.indexOf(needle, cursor);
  }
  if (cursor < source.length) parts.push({ text: source.slice(cursor), hit: false });
  return parts.length ? parts : [{ text: source, hit: false }];
}
