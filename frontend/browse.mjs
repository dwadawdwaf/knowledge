// 文件浏览：按已发布分块还原文档正文，支持分页分组、页内查找与块定位。
// 只读视图，不提供编辑与上传；写入操作仍在所属知识库内完成。
import { el, icon, emptyState, loading, actionButton } from './dom.mjs';
import {
  jobLabel, locatorText, warningLabel, readableSize, mediaLabel, parserLabel,
  groupByPage, splitHighlight, sharedWarnings,
} from './core.mjs';

const LIMIT = 500;

export function emptyBrowse() {
  return {
    doc: null, detail: null, items: [], total: 0, truncated: false,
    loading: false, error: null, query: '', grouped: true,
    focusOrdinal: null, focusLocator: null,
    // 原文视图：PDF 与图片默认直接看原件；其余格式默认看解析正文。
    tab: 'text', tabChosen: false, original: { url: null, loading: false, error: null, mediaType: null },
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
    if (browse.tab === 'original') loadOriginal(app);
  }
}

/** 能内嵌预览的格式；其余（DOCX / Markdown）默认落到正文视图。 */
export function previewable(mediaType) {
  return Boolean(mediaType) && (mediaType.startsWith('image/') || mediaType.startsWith('application/pdf'));
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
    const response = await app.api.raw(`/documents/${browse.doc.id}/file`);
    const blob = await response.blob();
    if (browse.original.url) URL.revokeObjectURL(browse.original.url);
    browse.original.url = URL.createObjectURL(blob);
    browse.original.mediaType = response.headers.get('content-type') || browse.detail?.media_type || '';
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
  back.append(icon('arrow-left'), document.createTextNode('全部文件'));
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

function metaLine(app) {
  const meta = el('div', 'browse-meta');
  meta.id = 'browse-meta';
  fillMeta(app, meta);
  return meta;
}

/** 元信息依赖详情接口，加载完成后要重新填一次，否则停留在「未知格式」。 */
function fillMeta(app, node) {
  if (!node) return;
  const browse = app.state.browse;
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
  fillMeta(app, document.querySelector('#browse-meta'));
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
    body.append(emptyState(
      running ? '文件正在处理中' : '这个文件还没有可读内容',
      running
        ? '解析与向量化完成后即可阅读；处理进度可在知识库内查看。'
        : '还没有已发布的分块。到所属知识库执行「重建」后再查看。',
      actionButton('去所属知识库', 'btn ghost', () => app.openLibraryById(browse.doc.kb_id), 'folder'),
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
      head.append(el('span', 'browse-page-no', group.page == null ? '未标注页码' : `第 ${group.page} 页`));
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

function paintOriginal(app, body) {
  const browse = app.state.browse;
  const original = browse.original;

  if (original.error) {
    body.append(app.errorBox(original.error));
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

  const card = el('div', 'browse-fallback');
  card.append(el('h3', '', '该格式无法在浏览器内直接预览'));
  card.append(el('p', '', `${mediaLabel(media)} 文件已取回，可下载后用本地软件打开；`
    + '若要按分段阅读正文，切到「提取正文」。'));
  const link = el('a', 'btn primary', '下载原文件');
  link.href = original.url;
  link.download = browse.doc.title || 'document';
  card.append(link);
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

  // 表格块：制表符在比例字体下无法对齐，显示时换成可见分隔；复制仍给原始制表符。
  const sheetBlock = (item.locator || {}).precision === 'sheet';
  const shown = sheetBlock ? (item.text || '').replace(/\t/g, ' │ ') : (item.text || '');
  const text = el('p', sheetBlock ? 'browse-text sheet' : 'browse-text');
  splitHighlight(shown, keyword).forEach(part => {
    if (part.hit) text.append(el('mark', '', part.text));
    else text.append(document.createTextNode(part.text));
  });

  card.append(head, text);
  return card;
}
