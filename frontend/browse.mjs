// 文件浏览：按已发布分块还原文档正文，支持分页分组、页内查找与块定位。
// 只读视图，不提供编辑与上传；写入操作仍在所属知识库内完成。
import { el, icon, emptyState, loading, actionButton } from './dom.mjs';
import {
  jobLabel, locatorText, warningLabel, readableSize, mediaLabel, parserLabel,
  groupByPage, splitHighlight, sharedWarnings,
} from './core.mjs';

const LIMIT = 500;
const SHEET_MEDIA = 'application/vnd.openxmlformats-officedocument.spreadsheetml';
const DOCX_MEDIA = 'application/vnd.openxmlformats-officedocument.wordprocessingml';
const TIFF_MEDIA = 'image/tiff';
// 原文视图里能直接解码显示的文本上限；超过就只给下载。
const PLAIN_TEXT_LIMIT = 512 * 1024;

export function emptyBrowse() {
  return {
    doc: null, detail: null, items: [], total: 0, truncated: false,
    loading: false, error: null, query: '', grouped: true,
    focusOrdinal: null, focusLocator: null,
    // 原文视图：PDF 与图片默认直接看原件；其余格式默认看解析正文。
    tab: 'text', tabChosen: false,
    // 表格预览：浏览器渲染不了 xlsx，改由服务端转成二维数据
    sheets: null, sheetsLoading: false, sheetsError: null, sheetIndex: 0,
    // DOCX 结构化预览：段落与表格，不做保真排版
    docx: null, docxLoading: false, docxError: null,
    // 从哪个页面打开：决定左上角返回按钮的文字与去向
    returnPage: 'files', returnLabel: '全部文件',
    original: { url: null, loading: false, error: null, mediaType: null, text: null },
  };
}

export async function loadBrowse(app) {
  const browse = app.state.browse;
  if (!browse?.doc) return;
  browse.loading = true;
  browse.error = null;
  paint(app);
  try {
    const [detail, chunks] = await Promise.all([
      app.api.request(`/documents/${browse.doc.id}`),
      app.api.request(`/documents/${browse.doc.id}/chunks?limit=${LIMIT}`),
    ]);
    browse.detail = detail;
    browse.items = chunks.items || [];
    browse.total = chunks.total || browse.items.length;
    browse.truncated = Boolean(chunks.truncated);
    // 用户没手动切换时，PDF/图片默认看原文，其余看正文。
    if (!browse.tabChosen) browse.tab = previewable(detail.media_type) ? 'original' : 'text';
  } catch (error) {
    browse.error = error;
    browse.items = [];
  } finally {
    browse.loading = false;
    // 默认视图可能从「正文」改为「原文」，工具条上的选中态要一起重建。
    app.render();
    if (browse.tab === 'original') {
      if (isDocx(browse.detail?.media_type)) loadDocx(app);
      else if (!isSheet(browse.detail?.media_type)) loadOriginal(app);
    }
  }
}

/** 能在「原文」里直接看到的格式：PDF、图片、表格；其余（DOCX 等）默认落到正文视图。 */
export function previewable(mediaType) {
  if (!mediaType) return false;
  return mediaType.startsWith('image/')
    || mediaType.startsWith('application/pdf')
    || isSheet(mediaType)
    || isDocx(mediaType);
}

export function isDocx(mediaType) {
  return Boolean(mediaType) && mediaType.startsWith(DOCX_MEDIA);
}

export function isSheet(mediaType) {
  return Boolean(mediaType)
    && (mediaType.startsWith(SHEET_MEDIA) || mediaType === 'text/csv');
}

function isTiff(mediaType) {
  return Boolean(mediaType) && mediaType.startsWith(TIFF_MEDIA);
}

export async function downloadOriginal(app) {
  const browse = app.state.browse;
  if (!browse?.doc) return;
  if (!browse.original.url) await loadOriginal(app);
  const url = app.state.browse.original.url;
  if (!url) return;
  const link = document.createElement('a');
  link.href = url;
  link.download = browse.doc.title || 'document';
  document.body.append(link);
  link.click();
  link.remove();
}

export function disposeBrowse(app) {
  const url = app.state.browse?.original?.url;
  if (url) URL.revokeObjectURL(url);
}

export async function loadOriginal(app) {
  const browse = app.state.browse;
  if (!browse?.doc || browse.original.url || browse.original.loading) return;
  browse.original.loading = true;
  paint(app);
  try {
    // TIFF 等浏览器不支持的位图：让服务端转码后再内联展示，原件仍可单独下载
    const convert = isTiff(browse.detail?.media_type || browse.original.mediaType);
    const response = await app.api.raw(`/documents/${browse.doc.id}/file${convert ? '?as=png' : ''}`);
    const blob = await response.blob();
    if (browse.original.url) URL.revokeObjectURL(browse.original.url);
    browse.original.url = URL.createObjectURL(blob);
    browse.original.mediaType = response.headers.get('content-type') || browse.detail?.media_type || '';
    // 纯文本与 Markdown 浏览器能直接显示，取一次解码结果，避免「原文」标签下只有一句看得见却打不开的解释。
    browse.original.text = browse.original.mediaType.startsWith('text/') && blob.size <= PLAIN_TEXT_LIMIT
      ? await blob.text()
      : null;
    browse.original.error = null;
  } catch (error) {
    browse.original.error = error;
  } finally {
    browse.original.loading = false;
    // 下载入口与内嵌预览都依赖 blob 地址，取到后要重建工具条。
    app.render();
  }
}

export function renderBrowse(app) {
  const browse = app.state.browse;
  const page = el('div', 'page browse-page');
  if (!browse?.doc) {
    page.append(emptyState('没有打开的文件', '从「全部文件」里选一个文件查看正文。',
      actionButton('返回全部文件', 'btn primary', () => app.closeBrowse(), 'arrow-left')));
    return page;
  }

  const head = el('div', 'page-head browse-head');
  const text = el('div', 'browse-heading');
  const back = el('button', 'link-btn back-link');
  back.type = 'button';
  back.append(icon('arrow-left'), document.createTextNode(browse.returnLabel || '全部文件'));
  back.title = browse.returnLabel || '返回';
  back.addEventListener('click', () => app.closeBrowse());
  text.append(back);
  const title = el('h1', '', browse.doc.title || '未命名文件');
  title.title = browse.doc.title || '';
  text.append(title);
  text.append(metaLine(app));

  const actions = el('div', 'head-actions');
  actions.append(actionButton('刷新', 'btn ghost', () => loadBrowse(app), 'refresh'));
  // 原文下载与当前视图无关：不预览 DOCX 也应能取回原件。
  actions.append(actionButton('下载原文件', 'btn ghost', () => downloadOriginal(app), 'download'));
  actions.append(actionButton('分块视图', 'btn ghost', () => app.openChunks(browse.doc), 'layers'));
  head.append(text, actions);
  page.append(head);

  page.append(toolbar(app));

  const body = el('div', 'browse-body');
  body.id = 'browse-body';
  page.append(body);
  // 此刻 page 还没挂进文档，按 id 查不到自己，直接传入节点。
  paint(app, body);
  return page;
}

/** 页内查找与分组开关；输入只影响本地渲染，不重新请求。 */
function tabs(app) {
  const browse = app.state.browse;
  const wrap = el('div', 'browse-tabs');
  wrap.setAttribute('role', 'tablist');
  [['original', '原文'], ['text', '提取正文']].forEach(([key, label]) => {
    const tab = el('button', key === browse.tab ? 'browse-tab active' : 'browse-tab', label);
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(key === browse.tab));
    tab.addEventListener('click', () => {
      if (browse.tab === key) return;
      browse.tab = key;
      browse.tabChosen = true;
      app.render();  // 切换视图需要重建工具条与正文区
      if (key === 'original') loadOriginal(app);
    });
    wrap.append(tab);
  });
  return wrap;
}

function toolbar(app) {
  const browse = app.state.browse;
  const bar = el('div', 'browse-toolbar');
  bar.append(tabs(app));

  if (browse.tab === 'original') {
    bar.append(el('div', 'spacer'));
    return bar;
  }

  const field = el('label', 'search-field browse-search');
  const input = el('input');
  input.type = 'search';
  input.placeholder = '在本文件中查找';
  input.setAttribute('aria-label', '在本文件中查找');
  input.value = browse.query;
  field.append(icon('search'), input);

  const count = el('span', 'browse-hit-count', '');
  count.id = 'browse-hit-count';

  const toggle = el('label', 'browse-toggle');
  const box = el('input');
  box.type = 'checkbox';
  box.checked = browse.grouped;
  box.addEventListener('change', () => {
    browse.grouped = box.checked;
    paint(app);
  });
  toggle.append(box, document.createTextNode('按页分组'));

  input.addEventListener('input', () => {
    browse.query = input.value;
    paint(app);
  });
  // 查找框里按 Esc 清空，不必手动删字。
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      input.value = '';
      browse.query = '';
      paint(app);
    }
  });

  bar.append(field, count, el('div', 'spacer'), toggle);
  return bar;
}

// 记下当前页的元信息节点：首次渲染时它还没挂进文档，按 id 查不到，只能直接持有引用。
let metaNode = null;

function metaLine(app) {
  metaNode = el('div', 'browse-meta');
  metaNode.id = 'browse-meta';
  fillMeta(app, metaNode);
  return metaNode;
}

/** 元信息依赖详情接口，加载完成后要重新填一次，否则停留在「未知格式」。 */
function fillMeta(app, node) {
  if (!node) return;
  const browse = app.state.browse;
  if (!browse.detail) {
    // 详情还没回来：给「读取中」而不是「未发布」，后者会被当成结论。
    node.replaceChildren(el('span', 'tag gray', browse.error ? '读取失败' : '读取中…'));
    return;
  }
  const detail = browse.detail || {};
  const pages = new Set(browse.items.map(item => (item.locator || {}).page).filter(Boolean));
  const chips = [
    detail.media_type ? mediaLabel(detail.media_type) : null,
    detail.byte_size ? readableSize(detail.byte_size) : null,
    detail.version_no ? `V${detail.version_no}` : '未发布',
    `代次 ${detail.generation ?? browse.doc?.generation ?? '—'}`,
    browse.total ? `${browse.total} 个分块` : null,
    pages.size ? `${pages.size} 页` : null,
    detail.parser_profile ? parserLabel(detail.parser_profile, detail.media_type) : null,
  ].filter(Boolean);
  node.replaceChildren(...chips.map(label => el('span', 'tag gray', label)));
}

function paint(app, target) {
  const browse = app.state.browse;
  const body = target || document.querySelector('#browse-body');
  const hitCount = document.querySelector('#browse-hit-count');
  if (!body || !browse) return;
  // 详情是异步回来的，每次重绘都要重新填一遍；用持有的节点，避免页面尚未挂载时查不到而漏更新。
  fillMeta(app, metaNode || document.querySelector('#browse-meta'));
  body.replaceChildren();

  if (browse.tab === 'original') {
    paintOriginal(app, body);
    return;
  }

  if (browse.loading) {
    body.append(loading('正在读取文件正文…'));
    if (hitCount) hitCount.textContent = '';
    return;
  }
  if (browse.error) {
    body.append(app.errorBox(browse.error));
    return;
  }

  if (!browse.items.length) {
    const job = browse.detail?.job || browse.doc?.job;
    const running = ['queued', 'running'].includes(job?.state);
    // 引用抽屉进来时只带 id 与标题，没有归属知识库，不能给出一个点不动的入口。
    const jump = browse.doc.kb_id
      ? actionButton('去所属知识库', 'btn ghost', () => app.openLibraryById(browse.doc.kb_id), 'folder')
      : null;
    body.append(emptyState(
      running ? '文件正在处理中' : '这个文件还没有可读内容',
      running
        ? '解析与向量化完成后即可阅读；处理进度可在知识库内查看。'
        : '还没有已发布的分块。到所属知识库执行「重建」后再查看。',
      jump,
    ));
    if (hitCount) hitCount.textContent = '';
    return;
  }

  const keyword = browse.query.trim();
  const matched = keyword
    ? browse.items.filter(item => (item.text || '').toLowerCase().includes(keyword.toLowerCase()))
    : browse.items;
  if (hitCount) {
    hitCount.textContent = keyword ? `命中 ${matched.length} / ${browse.items.length} 块` : '';
  }

  if (browse.truncated) {
    const note = el('div', 'notice-line warn');
    note.append(icon('alert'),
      el('span', '', `文件共 ${browse.total} 个分块，当前只读前 ${browse.items.length} 个；超出部分请在分块视图查看。`));
    body.append(note);
  }

  const visible = keyword ? matched : browse.items;
  const shared = sharedWarnings(browse.items);
  if (shared.length) {
    const line = el('div', 'notice-line warn browse-shared');
    line.append(icon('alert'), el('span', '', `全文 ${shared.map(warningLabel).join('、')}`));
    body.append(line);
  }
  if (!visible.length) {
    body.append(emptyState('没有匹配的内容', '换个关键词，或清空查找框查看全文。'));
    return;
  }

  const useGroups = browse.grouped && visible.some(item => (item.locator || {}).page != null);
  if (useGroups) {
    groupByPage(visible).forEach(group => {
      const section = el('section', 'browse-page-group');
      const head = el('div', 'browse-page-head');
      const label = group.page == null
        ? '未标注页码'
        : group.type === 'pptx' ? `第 ${group.page} 张幻灯片` : `第 ${group.page} 页`;
      head.append(el('span', 'browse-page-no', label));
      head.append(el('span', 'browse-page-count', `${group.items.length} 块`));
      section.append(head);
      group.items.forEach(item => section.append(block(app, item, keyword, shared)));
      body.append(section);
    });
  } else {
    visible.forEach(item => body.append(block(app, item, keyword, shared)));
  }

  // 从引用或检索跳进来时滚到目标块，避免还要手动翻。
  const focusNode = browse.focusOrdinal != null
    ? body.querySelector(`[data-ordinal="${browse.focusOrdinal}"]`)
    : browse.focusLocator ? matchBlock(body, browse.focusLocator) : null;
  if (focusNode) {
    focusNode.classList.add('focus');
    focusNode.scrollIntoView({ block: 'center' });
  }
  browse.focusOrdinal = null;
  browse.focusLocator = null;
}

export async function loadDocx(app) {
  const browse = app.state.browse;
  if (!browse?.doc || browse.docx || browse.docxLoading) return;
  browse.docxLoading = true;
  browse.docxError = null;
  paint(app);
  try {
    browse.docx = await app.api.request(`/documents/${browse.doc.id}/docx`);
  } catch (error) {
    browse.docxError = error;
  } finally {
    browse.docxLoading = false;
    paint(app);
  }
}

function paintDocx(app, body) {
  const browse = app.state.browse;
  if (browse.docxError) {
    body.append(app.errorBox(browse.docxError));
    return;
  }
  if (!browse.docx) {
    body.append(loading('正在整理文档结构…'));
    loadDocx(app);
    return;
  }

  const note = el('div', 'plain-note');
  note.append(icon('info'), el('span', '',
    '结构化预览：保留段落与表格文字，不保留字体、分页与图片；原件可下载。'));
  body.append(note);

  const wrap = el('div', 'docx-preview');
  (browse.docx.blocks || []).forEach(item => {
    if (item.type === 'table') {
      const table = el('table', 'document-table');
      const head = el('thead');
      const headRow = el('tr');
      (item.rows[0] || []).forEach(cell => headRow.append(el('th', '', cell)));
      head.append(headRow);
      const tbody = el('tbody');
      item.rows.slice(1).forEach(row => {
        const tr = el('tr');
        row.forEach(cell => tr.append(el('td', '', cell)));
        tbody.append(tr);
      });
      table.append(head, tbody);
      const scroll = el('div', 'browse-table-wrap');
      scroll.append(table);
      wrap.append(scroll);
      return;
    }
    wrap.append(el(item.heading ? 'h3' : 'p',
      item.heading ? 'docx-heading' : 'docx-para', item.text));
  });
  if (!wrap.childNodes.length) {
    wrap.append(emptyState('文档没有可提取的文字', '可以下载原件用本地软件打开。'));
  }
  body.append(wrap);

  if (browse.docx.truncated) {
    const line = el('div', 'notice-line warn');
    line.append(icon('alert'), el('span', '', '文档内容较多，预览只展示前一部分。'));
    body.append(line);
  }
}

export async function loadSheets(app) {
  const browse = app.state.browse;
  if (!browse?.doc || browse.sheets || browse.sheetsLoading) return;
  browse.sheetsLoading = true;
  browse.sheetsError = null;
  paint(app);
  try {
    browse.sheets = await app.api.request(`/documents/${browse.doc.id}/sheets`);
  } catch (error) {
    browse.sheetsError = error;
  } finally {
    browse.sheetsLoading = false;
    paint(app);
  }
}

/** 表格预览：按工作表分页，首个非空行作表头。 */
function paintSheets(app, body) {
  const browse = app.state.browse;
  if (browse.sheetsError) {
    body.append(app.errorBox(browse.sheetsError));
    return;
  }
  if (!browse.sheets) {
    body.append(loading('正在读取表格内容…'));
    loadSheets(app);
    return;
  }

  const sheets = browse.sheets.sheets || [];
  if (!sheets.length) {
    body.append(emptyState('这个表格里没有工作表', '可以下载原件用本地软件打开。'));
    return;
  }
  const index = Math.min(browse.sheetIndex, sheets.length - 1);
  const sheet = sheets[index];

  if (sheets.length > 1) {
    const tabs = el('div', 'sheet-tabs');
    tabs.setAttribute('role', 'tablist');
    sheets.forEach((item, position) => {
      const tab = el('button', position === index ? 'sheet-tab active' : 'sheet-tab', item.name);
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(position === index));
      tab.append(el('span', 'sheet-tab-count', String(item.rows.length)));
      tab.addEventListener('click', () => {
        if (browse.sheetIndex === position) return;
        browse.sheetIndex = position;
        paint(app);
      });
      tabs.append(tab);
    });
    body.append(tabs);
  } else {
    body.append(el('div', 'sheet-single', sheets[0].name));
  }

  const note = el('div', 'plain-note');
  const limits = browse.sheets.limits || {};
  const truncated = sheet.truncated || browse.sheets.truncated;
  note.append(icon('info'), el('span', '',
    `只读预览：共 ${sheet.rows.length} 行` + (truncated
      ? `，已按上限截断（最多 ${limits.rows} 行 / ${limits.cols} 列），完整内容请下载原文件`
      : '，完整内容请以原件为准')));
  body.append(note);

  if (!sheet.rows.length) {
    body.append(emptyState('这个工作表是空的', '换一个工作表，或下载原件查看。'));
    return;
  }

  const [header, ...rest] = sheet.rows;
  const table = el('table', 'browse-table sheet-table');
  const headRow = el('tr');
  header.forEach((cell, column) => headRow.append(el('th', '', cell || `列 ${column + 1}`)));
  const thead = el('thead');
  thead.append(headRow);
  table.append(thead);
  const tbody = el('tbody');
  rest.forEach(row => {
    const tr = el('tr');
    header.forEach((_, column) => tr.append(el('td', '', row[column] ?? '')));
    tbody.append(tr);
  });
  table.append(tbody);

  const scroll = el('div', 'browse-table-wrap');
  scroll.append(table);
  body.append(scroll);
}

function paintOriginal(app, body) {
  const browse = app.state.browse;
  const original = browse.original;

  // DOCX 不取原件字节：走 /docx 的段落与表格；表格走 /sheets 的二维数据
  if (isDocx(browse.detail?.media_type)) {
    paintDocx(app, body);
    return;
  }
  if (isSheet(browse.detail?.media_type)) {
    paintSheets(app, body);
    return;
  }

  if (original.error) {
    body.append(app.errorBox(original.error));
    // 404 多半是旧运行留下的文档：库里还有元数据，对象卷却换过了。
    if (original.error.status === 404) {
      const hint = el('div', 'notice-line');
      hint.append(icon('info'), el('span', '',
        '原始文件取不到时仍可阅读解析正文；如需原件，请在所属知识库重新上传同一文件。'));
      body.append(hint);
      const retry = el('button', 'btn ghost', '重试读取原文');
      retry.type = 'button';
      retry.addEventListener('click', () => {
        browse.original.error = null;
        paint(app);
      });
      body.append(retry);
    }
    return;
  }
  if (!original.url) {
    body.append(loading('正在读取原始文件…'));
    loadOriginal(app);
    return;
  }

  const media = original.mediaType || browse.detail?.media_type || '';
  if (media.startsWith('application/pdf')) {
    const frame = el('iframe', 'browse-frame');
    frame.src = original.url;
    frame.title = browse.doc.title || '原始文件';
    const hint = el('div', 'browse-frame-hint');
    hint.append(el('span', '', '若页面没有显示内容（部分浏览器不带内置 PDF 阅读器），'));
    const open = el('a', 'link-btn', '在新标签打开');
    open.href = original.url;
    open.target = '_blank';
    open.rel = 'noopener';
    hint.append(open);
    body.append(frame, hint);
    return;
  }
  if (media.startsWith('image/')) {
    const box = el('div', 'browse-image');
    const image = el('img');
    image.src = original.url;
    image.alt = browse.doc.title || '原始图片';
    box.append(image);
    body.append(box);
    return;
  }

  // 纯文本与 Markdown：浏览器本来就能显示，按原样给出，不做 Markdown 渲染。
  if (media.startsWith('text/') && original.text != null) {
    const note = el('div', 'plain-note');
    note.append(icon('info'),
      el('span', '', `按文件原始内容显示，未渲染 ${mediaLabel(media)} 样式；排版以「提取正文」为准。`));
    const pre = el('pre', 'plain-body');
    pre.textContent = original.text;
    const wrap = el('div', 'browse-plain');
    wrap.append(note, pre);
    body.append(wrap);
    return;
  }

  const card = el('div', 'browse-fallback');
  const mark = el('span', 'fallback-mark');
  mark.append(icon('file'));
  const info = el('div', 'fallback-info');
  info.append(el('h3', '', media.startsWith('text/') ? '文件过大，未在页面内展开' : '该格式无法在浏览器内直接预览'));
  info.append(el('p', '', media.startsWith('text/')
    ? `${mediaLabel(media)} 超过 ${Math.round(PLAIN_TEXT_LIMIT / 1024)} KB，页面里直接展开会拖慢滚动；可下载后查看，或切到「提取正文」按分段阅读。`
    : `${mediaLabel(media)} 不是浏览器能渲染的格式，下载后用本地软件打开；若要按分段阅读正文，切到「提取正文」。`));
  const metas = [
    mediaLabel(media),
    browse.detail?.byte_size ? readableSize(browse.detail.byte_size) : null,
    browse.detail?.version_no ? `V${browse.detail.version_no}` : null,
    browse.total ? `${browse.total} 个分块` : null,
  ].filter(Boolean);
  const tags = el('div', 'fallback-meta');
  metas.forEach(label => tags.append(el('span', 'tag gray', label)));
  info.append(tags);
  card.append(mark, info);

  const actions = el('div', 'fallback-actions');
  const link = el('a', 'btn primary', '下载原文件');
  link.href = original.url;
  link.download = browse.doc.title || 'document';
  const text = el('button', 'btn ghost', '查看提取正文');
  text.type = 'button';
  text.addEventListener('click', () => {
    browse.tab = 'text';
    browse.tabChosen = true;
    app.render();
  });
  actions.append(link, text);
  card.append(actions);
  body.append(card);
}

/** 按 locator 的页与块号定位卡片；页码缺失时退回块内字符区间。 */
function matchBlock(body, locator = {}) {
  const cards = [...body.querySelectorAll('.browse-block')];
  return cards.find(card => (
    Number(card.dataset.page) === Number(locator.page)
    && Number(card.dataset.block) === Number(locator.block)
  )) || cards.find(card => (
    Number(card.dataset.charStart) === Number(locator.char_start)
    && Number(card.dataset.charEnd) === Number(locator.char_end)
  )) || null;
}

function block(app, item, keyword, shared = []) {
  const locator = item.locator || {};
  const card = el('article', 'browse-block');
  card.dataset.ordinal = String(item.ordinal);
  card.dataset.page = locator.page ?? '';
  card.dataset.block = locator.block ?? '';
  card.dataset.charStart = locator.char_start ?? '';
  card.dataset.charEnd = locator.char_end ?? '';

  const head = el('div', 'browse-block-head');
  head.append(el('span', 'browse-block-no', `#${item.ordinal + 1}`));
  head.append(el('span', 'browse-block-loc', locatorText(locator)));
  head.append(el('span', 'browse-block-chars', `${(item.text || '').length} 字`));
  // 已提到顶部的共有警告不再逐块重复，只留本块特有的。
  (item.warnings || [])
    .filter(code => !shared.includes(code))
    .forEach(code => head.append(el('span', 'chunk-warn', warningLabel(code))));

  const copy = el('button', 'link-btn browse-copy', '复制');
  copy.type = 'button';
  copy.title = '复制本块原文';
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(item.text || '');
      app.notify('已复制本块原文。');
    } catch {
      app.notify('浏览器拒绝写入剪贴板，请手动选择文本。');
    }
  });
  head.append(copy);

  // 表格块：制表符在比例字体下无法对齐，改渲染成真正的表格；复制仍给原始制表符文本。
  const sheetBlock = (item.locator || {}).precision === 'sheet';
  const sheet = sheetBlock ? sheetTable(item.text || '', keyword) : null;
  if (sheet) {
    // 定位信息已经在块头里（工作表 + 行区间），表格上方不再重复一遍
    card.append(head);
    const scroll = el('div', 'browse-table-wrap');
    scroll.append(sheet.table);
    card.append(scroll);
    return card;
  }

  const text = el('p', 'browse-text');
  splitHighlight(item.text || '', keyword).forEach(part => {
    if (part.hit) text.append(el('mark', '', part.text));
    else text.append(document.createTextNode(part.text));
  });

  card.append(head, text);
  return card;
}

/**
 * 把「工作表《X》第a-b行 + 制表符行」还原成表格。
 * 首行没有制表符时当作说明，首行有制表符时当作表头；不足两行则退回文本渲染。
 */
export function sheetTable(source, keyword) {
  const lines = source.split('\n').filter(line => line.trim());
  if (lines.length < 2) return null;
  if (!lines[0].includes('\t')) lines.shift();  // 首行是「工作表《X》第a-b行」说明，块头已展示
  if (lines.length < 2) return null;
  const rows = lines.map(line => line.split('\t'));
  const width = Math.max(...rows.map(row => row.length));
  const [header, ...body] = rows;

  const table = el('table', 'browse-table');
  const headRow = el('tr');
  for (let index = 0; index < width; index += 1) headRow.append(el('th', '', header[index] || ''));
  const thead = el('thead');
  thead.append(headRow);
  table.append(thead);

  const tbody = el('tbody');
  body.forEach(cells => {
    const row = el('tr');
    for (let index = 0; index < width; index += 1) {
      const cell = el('td', index === 0 ? 'col-key' : '');
      splitHighlight(cells[index] || '', keyword).forEach(part => {
        if (part.hit) cell.append(el('mark', '', part.text));
        else cell.append(document.createTextNode(part.text));
      });
      row.append(cell);
    }
    tbody.append(row);
  });
  table.append(tbody);
  return { table };
}
