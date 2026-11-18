// 协议与展示转换保持独立，复用浏览器 Fetch，不引入客户端框架或存储。
const messages = {
  UNAUTHENTICATED: '访问令牌无效或已过期，请重新连接。',
  RESOURCE_NOT_FOUND: '内容不存在、已删除，或当前身份无权访问。',
  VERSION_CONFLICT: '文档状态已变化或已有处理任务，请刷新后再操作。',
  SOURCE_CHANGED: '回答期间资料发生变化，请重新提问。',
  FILE_TOO_LARGE: '文件超过后端大小上限。',
  UNSUPPORTED_FORMAT: '文件格式或所选解析方案不支持。DOC 和图片需要 MinerU。',
  VALIDATION_ERROR: '请求参数不符合要求，请检查输入。',
  MODEL_UNAVAILABLE: '模型服务暂不可用，未返回答案。',
  MODEL_TIMEOUT: '模型服务超时，未返回答案。',
  CANCELLED: '请求等待已取消。',
  STALE_SESSION: '连接身份已变化，请重新读取。',
  STREAM_INTERRUPTED: '回答过程中连接中断，请重新提问。',
  KEYWORD_INDEX_UNAVAILABLE: '所选资料的检索索引需要更新，暂时无法完成关键词检索或问答。文件管理仍可使用，请联系维护人员修复索引。',
  CITATION_VALIDATION_FAILED: '答案未通过引用校验，未返回正文。这不是正常拒答；已知同主题无答案场景也可能触发此错误。',
};

export function retrievalStatusMessage(status) {
  const parts = [];
  if (status?.missing_keyword_chunks > 0) {
    parts.push(`有 ${status.missing_keyword_chunks} 个历史片段需要更新检索索引。相关资料的关键词检索与问答暂不可用，文件管理仍可使用。请联系维护人员修复索引。`);
  }
  if (status?.legacy_pending_jobs > 0) {
    parts.push(`有 ${status.legacy_pending_jobs} 个旧版处理任务需要维护，文件处理暂时暂停。`);
  }
  return parts.join(' ');
}
export class ApiError extends Error {
  constructor(code, status = 0, requestId = '') {
    super(messages[code] || '服务请求失败，请检查后端状态。');
    this.code = code; this.status = status; this.requestId = requestId;
  }
}
export class ApiClient {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
    this._token = '';
    this.revision = 0;
    this.pending = new Set();
  }
  get token() { return this._token; }
  set token(value) {
    this.cancelAll();
    this._token = value;
    this.revision += 1;
  }
  cancelAll() { this.pending.forEach(controller => controller.abort()); }
  async request(path, { method = 'GET', body, timeout = 120000, signal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const revision = this.revision;
    this.pending.add(controller);
    try {
      const headers = this.token ? { Authorization: `Bearer ${this.token}` } : {};
      const isFile = body instanceof FormData;
      if (body !== undefined && !isFile) headers['Content-Type'] = 'application/json';
      const response = await this.fetcher(`/api/v1${path}`, { method, headers,
        body: body === undefined ? undefined : isFile ? body : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
        credentials: 'omit', cache: 'no-store' });
      let data;
      try { data = await response.json(); } catch { throw new ApiError('INVALID_RESPONSE', response.status); }
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (!response.ok) throw new ApiError(data?.error?.code, response.status, data?.meta?.request_id || '');
      if (!data || !Object.hasOwn(data, 'data')) throw new ApiError('INVALID_RESPONSE', response.status);
      return data.data;
    } catch (error) {
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (signal?.aborted) throw new ApiError('CANCELLED');
      if (error instanceof ApiError) throw error;
      throw new ApiError('NETWORK_ERROR');
    } finally { clearTimeout(timer); this.pending.delete(controller); }
  }

  /**
   * 发起流式请求，返回响应体供 SSE 逐条读取。
   * `release()` 在读完后调用，负责释放中止器与超时计时器；
   * 超时是整轮上限，不是首字节，回答较长时由后端自己的模型超时兜底。
   */
  async stream(path, { method = 'POST', body, timeout = 300000, signal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const revision = this.revision;
    const release = () => {
      clearTimeout(timer);
      this.pending.delete(controller);
    };
    this.pending.add(controller);
    try {
      const headers = this.token ? { Authorization: `Bearer ${this.token}` } : {};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const response = await this.fetcher(`/api/v1${path}`, {
        method, headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
        credentials: 'omit', cache: 'no-store',
      });
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (!response.ok) {
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        await response.body?.cancel().catch(() => {});
        throw new ApiError(payload?.error?.code, response.status, payload?.meta?.request_id || '');
      }
      if (!response.body) throw new ApiError('INVALID_RESPONSE', response.status);
      return { response, release };
    } catch (error) {
      release();
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (signal?.aborted) throw new ApiError('CANCELLED');
      if (error instanceof ApiError) throw error;
      throw new ApiError('NETWORK_ERROR');
    }
  }

  /**
   * 取原始响应（文件字节用）。错误仍然按统一错误信封解析，
   * 避免把 404 的 JSON 当成文件内容。
   */
  async raw(path, { method = 'GET', timeout = 120000, signal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    const revision = this.revision;
    this.pending.add(controller);
    try {
      const headers = this.token ? { Authorization: `Bearer ${this.token}` } : {};
      const response = await this.fetcher(`/api/v1${path}`, {
        method, headers, signal: signal ? AbortSignal.any([signal, controller.signal]) : controller.signal,
        credentials: 'omit', cache: 'no-store',
      });
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (!response.ok) {
        let payload = null;
        try { payload = await response.json(); } catch { payload = null; }
        throw new ApiError(payload?.error?.code, response.status, payload?.meta?.request_id || '');
      }
      return response;
    } catch (error) {
      if (this.revision !== revision) throw new ApiError('STALE_SESSION');
      if (signal?.aborted) throw new ApiError('CANCELLED');
      if (error instanceof ApiError) throw error;
      throw new ApiError('NETWORK_ERROR');
    } finally { clearTimeout(timer); this.pending.delete(controller); }
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
  const stages = { parsing: '正在解析', chunking: '正在分段', enhancing: '正在增强索引文本', embedding: '正在向量化', publishing: '正在发布' };
  if (job.state === 'running') return stages[job.stage] || '处理中';
  return { queued: '排队中', failed: '处理失败', cancelled: '已取消', succeeded: '已就绪' }[job.state] || '未知状态';
}
export function locatorText(locator = {}) {
  const parts = [];
  if (locator.precision === 'composite' && Array.isArray(locator.sources)) {
    const pages = [...new Set(locator.sources.map(source => source.locator?.page).filter(Number.isInteger))].sort((a, b) => a - b);
    return pages.length ? `第 ${pages.join('、')} 页 · ${locator.sources.length} 处原文`
      : `${locator.sources.length} 处原文 · 分段定位`;
  }
  if (locator.type === 'docx' && Number.isInteger(locator.block) && Number.isInteger(locator.row_start)) {
    return `表格 ${locator.block} · 第 ${locator.row_start} 行`;
  }
  if (locator.type === 'pptx' && locator.page != null) {
    parts.push(`第 ${locator.page} 张幻灯片`);
  } else if (locator.page != null) {
    parts.push(`第 ${locator.page} 页`);
  }
  if (locator.type === 'pdf') {
    if (locator.pdf_column === 'left') parts.push('左栏');
    if (locator.pdf_column === 'right') parts.push('右栏');
    if (locator.pdf_column === 'full') parts.push('通栏');
  }
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
    // 解析器与 DOCX 只给块/段内偏移，标成“字符”会被误读成文档级位置。
    const scope = !['block', 'page'].includes(locator.precision) ? '字符'
      : locator.paragraph != null ? '段落内' : '块内';
    parts.push(`${scope} ${range}`);
  }
  return parts.join(' · ') || '原文分块';
}

// 解析器的内部实现细节不在界面上提示：这些码仍会出现在接口里，但不再翻译成文案。
const HIDDEN_WARNING_CODES = new Set([
  'mineru_requires_review',
  'mineru_geometry_unavailable',
  'mineru_native_location_unavailable',
]);

const WARNING_LABELS = {
  parser_direct_text: '所选方案对纯文本直接读取，未使用 OCR',
  mineru_table_linearized: 'MinerU 表格/HTML已转纯文本',
  mineru_content_skipped: 'MinerU存在未提取的非文本或不支持块，内容可能不完整',
  query_terms_empty: '没有可用的关键词，已使用语义检索',
  mineru_nul_replaced: 'MinerU 无效 NUL 字符已替换为 �，对应文字需复核',
  docx_images_require_mineru: '基础解析未识别 DOCX 内图片，请选用 MinerU',
  word_image_ocr_requires_review: '此片段来自 Word 内图片的 OCR，需核对原图',
  word_image_ocr_asset_unavailable: 'Word 内部分图片不可读取或为外链，未进行 OCR',
  word_image_ocr_format_unsupported: 'Word 内部分图片格式不支持 OCR',
  word_image_ocr_budget_exceeded: '已达到 Word 图片识别上限，部分图片未识别',
  word_image_ocr_no_text: 'Word 内部分图片未识别出文字',
  xlsx_rows_truncated: '表格行数超上限，仅索引前若干行',
  csv_rows_truncated: '表格行数超上限，仅索引前若干行',
  table_header_candidate: '按首行关联候选表头，需核对列名与单位',
  cross_page_continuation_candidate: '按章节和续句线索关联跨页内容，需核对阅读顺序',
  cross_column_continuation_candidate: '跨栏续接，需核对阅读顺序',
  pdf_layout_uncertain: 'PDF 版面无法确认，已保留解析顺序',
  pdf_ocr_requires_review: 'PDF 使用了文字识别，需核对原页',
  running_matter_omitted: '检索已排除解析器明确标注的页眉页脚，原件保留',
};

export function warningLabel(code) {
  return WARNING_LABELS[code] || code;
}

/** 过滤掉只属于解析实现细节、不需要展示给使用者的提示码。 */
export function visibleWarnings(codes = []) {
  return (codes || []).filter(code => !HIDDEN_WARNING_CODES.has(code));
}
// 分块视图只做度量与相对定位；缺字符偏移时退化为等宽，不伪造覆盖比例。
function absoluteRanges(locator) {
  const valid = part => Number.isInteger(part?.char_start) && Number.isInteger(part?.char_end)
    && part.char_start >= 0 && part.char_end > part.char_start;
  if (locator.precision === 'composite') {
    const sources = (locator.sources || []).map(source => source.locator);
    if (!sources.length || !sources.every((part, index) => part?.precision === 'char'
      && valid(part) && (!index || part.char_start >= sources[index - 1].char_end))) return [];
    return sources.map(part => [part.char_start, part.char_end]);
  }
  return (!locator.precision || locator.precision === 'char') && valid(locator)
    ? [[locator.char_start, locator.char_end]] : [];
}

export function chunkMetrics(items = []) {
  const hasRange = loc => Number.isFinite(loc?.char_start) && Number.isFinite(loc?.char_end)
    && loc.char_start >= 0 && loc.char_end > loc.char_start;
  const spans = items.map(item => {
    const locator = item.locator || {};
    const sources = locator.sources?.map(source => source.locator) || [];
    const documentRange = locator.precision === 'composite'
      && ['txt', 'md'].includes(locator.type)
      && sources.length > 1
      && sources.every((source, index) => source?.type === locator.type && source.precision === 'char'
        && hasRange(source) && (index === 0 || source.char_start >= sources[index - 1].char_end));
    const absolute = documentRange || (['txt', 'md'].includes(locator.type) && locator.precision === 'char');
    const local = !absolute && ['block', 'page', 'sheet', 'composite'].includes(locator.precision);
    const start = documentRange ? sources[0].char_start
      : Number.isFinite(locator.char_start) ? locator.char_start : null;
    const end = documentRange ? sources.at(-1).char_end
      : Number.isFinite(locator.char_end) ? locator.char_end : null;
    const ranges = documentRange ? sources.map(source => [source.char_start, source.char_end]) : [[start, end]];
    return { ordinal: item.ordinal, chars: (item.text || '').length, start, end, page: locator.page ?? null,
      documentRange, ranges, absolute, local };
  });
  const chars = spans.reduce((sum, span) => sum + span.chars, 0);
  // 文档级偏移可重复。未标注的旧偏移仍要求起点递增，明确的块内偏移始终等宽。
  const monotonic = spans.every((span, index) => index === 0 || span.start > spans[index - 1].start);
  const usable = spans.length > 0 && (spans.every(span => span.absolute) || monotonic)
    && spans.every(span => !span.local && span.start !== null && span.start >= 0 && span.end !== null && span.end > span.start);
  const maxEnd = usable ? Math.max(...spans.map(span => span.end)) : 0;
  let overlaps = 0;
  if (usable) for (let index = 1; index < spans.length; index += 1) {
    const previous = spans[index - 1].ranges;
    const current = spans[index].ranges;
    let i = 0, j = 0;
    while (i < previous.length && j < current.length) {
      const [a, b] = previous[i];
      const [c, d] = current[j];
      overlaps += Math.max(0, Math.min(b, d) - Math.max(a, c));
      if (b <= d) i += 1;
      else j += 1;
    }
  }
  const pages = [...new Set(items.flatMap(item => item.locator?.pages || [item.locator?.page]).filter(page => page != null))].sort((left, right) => left - right);
  const step = spans.length ? 100 / spans.length : 0;
  const segments = spans.map((span, index) => {
    if (!usable) return { ordinal: span.ordinal, left: index * step, width: Math.max(step - 0.5, 1), start: null, end: null, chars: span.chars, page: span.page };
    const left = (span.start / maxEnd) * 100;
    const length = span.end - span.start;
    const width = (length / maxEnd) * 100;
    const pieces = span.documentRange ? span.ranges.map(([start, end]) => ({
      left: ((start - span.start) / length) * 100, width: ((end - start) / length) * 100,
    })) : [];
    return { ordinal: span.ordinal, left, width: pieces.length ? width : Math.max(width, 1.2), pieces,
      start: span.start, end: span.end, chars: span.chars, page: span.page };
  });
  const blockPrecision = items.length > 0
    && items.every(item => (item.locator || {}).precision === 'block');
  return { count: spans.length, chars, avg: spans.length ? Math.round(chars / spans.length) : 0,
    usable, blockPrecision, compound: spans.some(span => span.documentRange), maxEnd, overlaps, pages, segments };
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
  ['application/msword', 'DOC'],
  ['image/png', 'PNG'],
  ['image/jpeg', 'JPEG'],
  ['image/tiff', 'TIFF'],
  ['application/vnd.openxmlformats-officedocument.wordprocessingml', 'DOCX'],
  ['application/vnd.openxmlformats-officedocument.spreadsheetml', 'XLSX'],
  ['application/vnd.openxmlformats-officedocument.presentationml', 'PPTX'],
  ['text/csv', 'CSV'],
  ['text/markdown', 'Markdown'],
  ['text/plain', 'TXT'],
];

export function mediaLabel(mediaType) {
  if (!mediaType) return '未知格式';
  const found = MEDIA_LABELS.find(([prefix]) => mediaType.startsWith(prefix));
  if (found) return found[1];
  return mediaType.split('/').pop().toUpperCase();
}

/**
 * 解析器标签。只有显式走 MinerU 的文档才显示；基础解析不贴质量标签。
 */
export function parserLabel(profile) {
  if (profile === 'pdf-auto-v1') return 'PDF 自动选择解析方式';
  if (profile === 'mineru-api4.1-v1') return 'MinerU 文档解析';
  return '';
}

export const PARSER_OPTIONS = [
  ['', '沿用服务默认'], ['legacy-v1', '基础解析'], ['mineru-api4.1-v1', 'MinerU'],
];

export function uploadForm(file, profile = '') {
  if (!PARSER_OPTIONS.some(([value]) => value === profile)) throw new Error('未知解析方案');
  const form = new FormData();
  form.append('file', file);
  if (profile) form.append('parser_profile', profile);
  return form;
}

/** 所有块共有的警告，用于提到文档级只提示一次。 */
export function sharedWarnings(items = []) {
  if (!items.length) return [];
  const [first, ...rest] = items.map(item => new Set(item.warnings || []));
  return visibleWarnings([...first].filter(code => rest.every(set => set.has(code)))).sort();
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
    else groups.push({ page, type: (item.locator || {}).type || null, items: [item] });
  });
  return groups;
}

/** 把服务端消息还原成对话轮次；assistant 消息挂到上一条 user 消息上。 */
export function turnsFromMessages(messages = []) {
  const turns = [];
  let pending = null;
  messages.forEach(message => {
    const at = message.created_at ? new Date(message.created_at) : new Date();
    if (message.role === 'user') {
      pending = { id: message.id, question: message.body || '', at, status: 'done', answer: null };
      turns.push(pending);
    } else if (message.role === 'assistant' && pending) {
      pending.answer = {
        answer_status: message.answer_status || '',
        answer: message.body || '',
        citations: message.citations || [],
        retrieval: {},
        warnings: [],
      };
    }
  });
  turns.forEach(turn => {
    if (!turn.answer) {
      turn.status = 'error';
      turn.error = new Error('这条提问没有找到保存的回答。');
    }
  });
  return turns;
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
