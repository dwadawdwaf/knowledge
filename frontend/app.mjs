import { ApiClient, ApiError, reindexTarget, jobLabel, locatorText } from './core.mjs';

const paths = {
  library: '<path d="M3 7h18v14H3zM5 3h14v4M8 11v6m4-6v6m4-6v6"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  chat: '<path d="M4 4h16v12H9l-5 4zM8 8h8m-8 4h5"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8zm-9 10 9 5 9-5M3 18l9 4 9-4"/>',
  book: '<path d="M3 4h7l2 2 2-2h7v15h-7l-2 2-2-2H3zm9 2v15"/>',
  settings: '<path d="m10 3-1 3-3 1-2 3 2 2v4l3 2 1 3h4l1-3 3-2v-4l2-2-2-3-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3v.1"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  list: '<path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
  file: '<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8m-8 4h5"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6"/>',
  refresh: '<path d="M20 10a8 8 0 1 0-1 7M20 3v7h-7"/>',
  spark: '<path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3zm7 0v4m-2-2h4"/>',
  send: '<path d="m3 3 18 9-18 9 4-9zm4 9h14"/>',
};
function icon(name) {
  const span = document.createElement('span');
  // 仅固定图标模板使用 innerHTML；文件名、问题、答案和引用一律作为文本渲染。
  span.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
  return span;
}
function el(tag, className = '', text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(label, className, action, glyph) {
  const node = el('button', className);
  node.type = 'button';
  if (glyph) node.append(icon(glyph));
  node.append(document.createTextNode(label));
  node.addEventListener('click', action);
  return node;
}
document.querySelectorAll('[data-icon]').forEach(node => node.replaceWith(icon(node.dataset.icon)));
document.querySelectorAll('[data-close]').forEach(node => node.addEventListener('click', () => node.closest('dialog').close()));
const api = new ApiClient();
const state = { page: 'knowledge', libraries: [], library: null, documents: [], scope: '', receipts: new Map(),
  filter: '', sort: 'latest', list: false, connected: false, revision: 0, busy: false, pollError: false };
const main = document.querySelector('#content');
const connection = document.querySelector('#connection-dialog');
let timer, noticeTimer, pollBusy = false;
function notify(message) {
  const node = document.querySelector('#notice'); node.textContent = message; node.hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => { node.hidden = true; }, 6500);
}
function failure(error) {
  if (error.status === 401) { disconnect(); connection.showModal(); }
  const panel = el('div', 'error-box', error instanceof ApiError ? error.message : '操作未完成，请稍后刷新重试。');
  panel.setAttribute('role', 'alert');
  if (error.requestId) panel.append(el('small', '', `请求编号：${error.requestId}`));
  return panel;
}
function requireConnection() { if (state.connected) return true; connection.showModal(); return false; }
function disconnect() {
  clearInterval(timer); api.token = ''; state.connected = false; state.libraries = []; state.documents = [];
  state.receipts.clear(); state.library = null; state.scope = ''; state.busy = false; state.revision++;
  document.querySelector('#connection-label').textContent = '未连接';
  document.querySelector('#source-dialog').close(); document.querySelector('#source-content').replaceChildren(); render();
}
async function loadLibraries() {
  const result = await api.request('/knowledge-bases');
  state.libraries = result.items.filter(kb => kb.status === 'active');
  if (!state.libraries.some(kb => kb.id === state.scope)) state.scope = state.libraries[0]?.id || '';
}
async function refreshLibraries() {
  if (!requireConnection()) return;
  try { await loadLibraries(); render(); } catch (error) { notify(failure(error).textContent); }
}
function navigate(page, library = null) {
  if (state.busy) { notify('当前请求仍在进行，请等待结束后切换页面。'); return; }
  clearInterval(timer); state.page = page; state.library = library; state.revision++; state.pollError = false;
  if (library) state.scope = library.id;
  history.replaceState(null, '', `#${page}`); render();
  if (library && state.connected) {
    loadDocuments();
    timer = setInterval(() => {
      // 避免刷新移除正在操作的按钮/文件选择器，或打断键盘焦点。
      if (!document.hidden && !pollBusy && !state.busy && !document.querySelector('dialog[open]') && !document.activeElement?.closest('#document-list')) loadDocuments(true);
    }, 2500);
  }
}
function pageHeading(title, description, action) {
  const heading = el('div', 'page-heading'); const text = el('div');
  text.append(el('h1', '', title), el('p', 'muted', description)); heading.append(text);
  if (action) heading.append(action); return heading;
}
function blank(title, description, action) {
  const node = el('div', 'empty'); const mark = el('div', 'empty-icon'); mark.append(icon('library'));
  node.append(mark, el('h3', '', title), el('p', '', description)); if (action) node.append(action); return node;
}
function render() {
  document.querySelectorAll('[data-page]').forEach(node => {
    const active = node.dataset.page === state.page; node.classList.toggle('active', active);
    if (active) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current');
  });
  document.querySelector('#breadcrumb').textContent = { knowledge: state.library?.title || state.library?.name || '知识库', search: '检索测试', chat: '智能问答' }[state.page];
  main.replaceChildren();
  if (state.page === 'search') renderSearch(); else if (state.page === 'chat') renderChat();
  else if (state.library) renderDocuments(); else renderLibraries();
}
function createLibrary() {
  if (!requireConnection()) return;
  actionDialog('新建知识库', '为你的资料建立一个清晰的知识空间。当前仅支持个人知识库。', true, async name => {
    if (!name.trim()) throw new Error('请输入知识库名称。');
    await api.request('/knowledge-bases', { method: 'POST', body: { name: name.trim() } });
    await loadLibraries(); render(); notify('知识库已创建，可以开始添加资料。');
  });
}
function actionDialog(title, description, input, action, danger = false) {
  const dialog = document.querySelector('#action-dialog'), form = document.querySelector('#action-form');
  document.querySelector('#action-title').textContent = title;
  document.querySelector('#action-description').textContent = description;
  const field = document.querySelector('#action-input'); field.hidden = !input; field.required = input; field.value = '';
  document.querySelector('#action-label').hidden = !input; document.querySelector('#action-error').textContent = '';
  const submit = document.querySelector('#action-submit'); submit.className = danger ? 'danger' : 'primary'; submit.textContent = danger ? '确认删除' : '创建知识库';
  form.onsubmit = async event => {
    event.preventDefault(); submit.disabled = true;
    try { await action(field.value); dialog.close(); }
    catch (error) {
      if (error.status === 401) { dialog.close(); failure(error); }
      else document.querySelector('#action-error').textContent = error instanceof ApiError ? error.message : '操作未完成，请检查输入或刷新状态。';
    }
    finally { submit.disabled = false; }
  }; dialog.showModal();
}
function renderLibraries() {
  main.append(pageHeading('知识库', '把分散的资料，整理成随时可用的知识。', button('新建知识库', 'primary', createLibrary, 'plus')));
  const toolbar = el('div', 'toolbar'), field = el('label', 'search-input');
  const input = el('input'); input.placeholder = '搜索知识库名称'; input.setAttribute('aria-label', '搜索知识库名称'); input.value = state.filter;
  field.append(icon('search'), input); toolbar.append(field, el('div', 'toolbar-spacer'));
  const sort = el('select'); sort.setAttribute('aria-label', '知识库排序');
  [['latest', '最近创建'], ['name', '按名称排序']].forEach(([value, label]) => { const option = el('option', '', label); option.value = value; sort.append(option); });
  sort.value = state.sort;
  const toggle = el('div', 'view-toggle');
  toggle.append(button('', !state.list ? 'active' : '', () => { state.list = false; render(); }, 'grid'), button('', state.list ? 'active' : '', () => { state.list = true; render(); }, 'list'));
  toggle.children[0].setAttribute('aria-label', '卡片视图'); toggle.children[1].setAttribute('aria-label', '列表视图');
  toolbar.append(sort, toggle, button('刷新', 'secondary', refreshLibraries, 'refresh')); main.append(toolbar);
  const caption = el('div', 'section-caption'); caption.append(el('strong', '', '全部知识库'), el('span', '', state.connected ? `${state.libraries.length} 个个人知识库` : '连接后加载实际数据')); main.append(caption);
  const grid = el('div', state.list ? 'card-grid card-list' : 'card-grid'); main.append(grid);
  function updateCards() {
    grid.replaceChildren();
    let items = state.libraries.filter(kb => kb.name.toLocaleLowerCase().includes(state.filter.toLocaleLowerCase()));
    items = [...items].sort((a, b) => state.sort === 'name' ? a.name.localeCompare(b.name, 'zh-CN') : b.created_at.localeCompare(a.created_at));
    items.forEach((kb, index) => {
      const card = el('article', 'kb-card'), top = el('div', 'card-top'), mark = el('div', `library-icon tone-${index % 3}`); mark.append(icon('library'));
      const remove = button('', 'icon-button card-delete', () => actionDialog('删除知识库', `确定删除“${kb.name}”及其中的资料吗？提交后立即阻断访问，随后异步清理。此操作不可撤销。`, false, async () => {
        await api.request(`/knowledge-bases/${kb.id}`, { method: 'DELETE' }); state.receipts.clear(); await loadLibraries(); render(); notify('知识库已停止访问，正在后台清理。');
      }, true), 'trash'); remove.setAttribute('aria-label', `删除知识库 ${kb.name}`); top.append(mark, remove);
      const title = el('h3'); title.append(button(kb.name, 'card-open', () => navigate('knowledge', kb)));
      const foot = el('div', 'card-foot'); foot.append(el('span', '', `创建于 ${new Date(kb.created_at).toLocaleDateString('zh-CN')}`), el('span', 'tag', '个人知识库'));
      card.append(top, title, el('p', '', '文档管理 · 内容检索 · 有据问答'), foot); grid.append(card);
    });
    if (!items.length) grid.append(blank(state.connected ? '从第一个知识库开始' : '连接后，知识就在这里', state.connected ? '新建知识库，上传自己的资料，再开始检索与问答。' : '使用启动终端的开发令牌连接，加载真实接口数据。', button(state.connected ? '新建知识库' : '连接工作台', 'primary', state.connected ? createLibrary : () => connection.showModal(), state.connected ? 'plus' : 'arrow')));
  }
  input.addEventListener('input', () => { state.filter = input.value; updateCards(); });
  sort.addEventListener('change', () => { state.sort = sort.value; updateCards(); }); updateCards();
  const note = el('div', 'welcome-note'); const text = el('div');
  text.append(el('strong', '', '一个简单、可靠的知识使用流程'), el('p', '', '添加资料并等待处理完成 → 在检索测试中确认命中 → 在智能问答中查看回答与引用。当前为开发体验环境，不用于甲方真实资料。'));
  note.append(icon('spark'), text); main.append(note);
}
async function loadDocuments(quiet = false) {
  const revision = state.revision, library = state.library; if (!library || !state.connected) return;
  pollBusy = true;
  try {
    const result = await api.request(`/knowledge-bases/${library.id}/documents`, { timeout: 15000 });
    if (revision !== state.revision) return;
    state.documents = result.items.filter(doc => doc.status === 'active'); state.pollError = false; drawDocuments();
  } catch (error) {
    if (revision === state.revision && (!quiet || !state.pollError || error.status === 401)) notify(failure(error).textContent);
    state.pollError = true;
  }
  finally { pollBusy = false; }
}
function renderDocuments() {
  state.documents = [];
  main.append(button('全部知识库', 'back-link', () => navigate('knowledge'), 'back'));
  main.append(pageHeading(state.library.name, '管理文档与处理任务，让资料成为可检索的知识。', button('在此知识库提问', 'primary', () => navigate('chat'), 'chat')));
  const upload = el('div', 'upload-zone'), text = el('div'); text.append(el('strong', '', '添加你的知识资料'), el('p', '', '拖放文件到这里，或选择文件上传 · TXT / MD / 文本 PDF / DOCX · 单文件不超过 50 MB'));
  const file = el('input'); file.type = 'file'; file.multiple = true; file.accept = '.txt,.md,.pdf,.docx'; file.setAttribute('aria-label', '选择知识库文档');
  const choose = button('上传文档', 'secondary', () => file.click(), 'upload'); upload.append(text, choose, file);
  const feedback = el('div'); feedback.setAttribute('aria-live', 'polite');
  const uploadFiles = async files => {
    if (!requireConnection() || state.busy) return;
    const library = state.library; state.busy = true; choose.disabled = true;
    try {
      for (const selected of files) {
        if (!/\.(txt|md|pdf|docx)$/i.test(selected.name) || selected.size > 50 * 1024 * 1024) { feedback.append(el('p', 'warning', `${selected.name}：已跳过，不支持该格式或文件超过 50 MB。`)); continue; }
        const form = new FormData(); form.append('file', selected);
        const ack = await api.request(`/knowledge-bases/${library.id}/documents`, { method: 'POST', body: form });
        state.receipts.set(ack.document_id, ack.version_id);
        feedback.append(el('p', 'muted', `${selected.name}：${ack.deduplicated ? '相同内容已存在，未新建任务' : '上传已接收，等待后台处理'}`));
        notify(ack.deduplicated ? '相同内容已存在，本次未创建处理任务。' : '上传已接收，后台任务正在处理。');
      }
      await loadDocuments();
    } catch (error) { feedback.append(failure(error), el('p', 'warning', '上传已中断，当前及剩余未成功接收的文件需确认后重新上传。')); }
    finally { state.busy = false; choose.disabled = false; file.value = ''; }
  };
  file.addEventListener('change', () => uploadFiles([...file.files]));
  upload.addEventListener('dragover', event => { event.preventDefault(); upload.classList.add('dragging'); });
  upload.addEventListener('dragleave', () => upload.classList.remove('dragging'));
  upload.addEventListener('drop', event => { event.preventDefault(); upload.classList.remove('dragging'); uploadFiles([...event.dataTransfer.files]); });
  main.append(upload, feedback);
  const caption = el('div', 'section-caption'); caption.append(el('strong', '', '知识库文档'), el('span', '', '每 2.5 秒自动刷新处理状态')); main.append(caption);
  const panel = el('div', 'document-panel'); panel.id = 'document-list'; panel.append(el('div', 'loading', '正在读取文档…')); main.append(panel);
}
function drawDocuments() {
  const panel = document.querySelector('#document-list'); if (!panel) return;
  if (!state.documents.length) { panel.replaceChildren(blank('还没有文档', '上传一份资料，处理成功后即可检索和提问。')); return; }
  const table = el('table', 'document-table'), head = el('thead'), row = el('tr');
  ['文档名称', '处理状态', '已发布版本', '操作'].forEach(title => row.append(el('th', '', title))); head.append(row); table.append(head);
  const body = el('tbody');
  state.documents.forEach(doc => {
    const row = el('tr'), name = el('td'), block = el('div', 'doc-name'), mark = el('span', 'file-icon'); mark.append(icon('file'));
    const text = el('div'); text.append(el('strong', '', doc.title), el('small', '', `代次 ${doc.generation}${doc.active_build_id ? ' · 已有可用内容' : ' · 暂无发布内容'}`)); block.append(mark, text); name.append(block);
    const status = el('td'), badge = el('span', `badge ${doc.job?.state || ''}`); badge.append(el('i'), document.createTextNode(jobLabel(doc))); status.append(badge);
    if (doc.job?.state === 'failed') status.append(el('small', 'error-text', doc.job.last_error?.code || '处理失败'));
    const actions = el('td'), bar = el('div', 'row-actions');
    const target = reindexTarget(doc, state.receipts);
    const rebuild = button('重建', 'text-button', async () => {
      rebuild.disabled = true;
      try {
        const ack = await api.request(`/documents/${doc.id}/reindex`, { method: 'POST', body: { version_id: target, expected_generation: doc.generation } });
        state.receipts.set(doc.id, ack.version_id); await loadDocuments(); notify('已创建新构建，旧发布内容保持可用。');
      } catch (error) { notify(failure(error).textContent); } finally { rebuild.disabled = false; }
    });
    rebuild.disabled = !target || ['running', 'queued'].includes(doc.job?.state);
    rebuild.title = !target ? '缺少失败版本的上传回执，不能将旧发布版本当作恢复目标。' : '按原模型和分段配置创建新构建';
    const versionFile = el('input'); versionFile.type = 'file'; versionFile.accept = '.txt,.md,.pdf,.docx'; versionFile.setAttribute('aria-label', `更新 ${doc.title}`);
    const update = button('更新', 'text-button', () => versionFile.click());
    versionFile.addEventListener('change', async () => {
      const selected = versionFile.files[0]; if (!selected || state.busy) return;
      if (!/\.(txt|md|pdf|docx)$/i.test(selected.name) || selected.size > 50 * 1024 * 1024) { notify('文件不支持或超过 50 MB，未上传。'); return; }
      state.busy = true; update.disabled = true;
      try {
        const form = new FormData(); form.append('file', selected);
        const ack = await api.request(`/documents/${doc.id}/versions?expected_generation=${doc.generation}`, { method: 'POST', body: form });
        state.receipts.set(doc.id, ack.version_id); await loadDocuments(); notify(ack.deduplicated ? '相同内容已存在，未新建版本。' : '新版本已接收，发布前旧版本继续可用。');
      } catch (error) { notify(failure(error).textContent); }
      finally { state.busy = false; update.disabled = false; versionFile.value = ''; }
    });
    bar.append(update, versionFile, rebuild, button('删除', 'text-button danger-text', () => actionDialog('删除文档', `确定删除“${doc.title}”吗？删除提交后，该文档及引用立即停止访问。`, false, async () => {
      await api.request(`/documents/${doc.id}`, { method: 'DELETE' }); state.receipts.delete(doc.id); await loadDocuments(); notify('文档已停止访问，正在后台清理。');
    }, true))); actions.append(bar);
    row.append(name, status, el('td', 'muted', doc.version_no ? `V${doc.version_no}` : '尚未发布'), actions); body.append(row);
  }); table.append(body); panel.replaceChildren(table);
}
function scopeSelect() {
  const select = el('select'); select.setAttribute('aria-label', '选择知识库范围');
  const placeholder = el('option', '', '请选择知识库'); placeholder.value = ''; select.append(placeholder);
  state.libraries.forEach(kb => { const option = el('option', '', kb.name); option.value = kb.id; select.append(option); }); select.value = state.scope;
  select.addEventListener('change', () => { if (state.busy) { select.value = state.scope; return; } state.scope = select.value; navigate(state.page); }); return select;
}
function renderSearch() {
  main.append(pageHeading('检索测试', '先看资料是否被正确找到，再判断答案是否有据可依。'));
  const layout = el('div', 'query-layout'), config = el('aside', 'config-panel');
  config.append(el('h3', '', '检索配置'), el('label', '', '知识范围'), scopeSelect(), el('label', '', '检索方式'));
  const mode = el('select'); mode.setAttribute('aria-label', '检索方式');
  [['hybrid', '混合检索'], ['semantic', '语义检索'], ['keyword', '关键词检索']].forEach(([value, title]) => { const item = el('option', '', title); item.value = value; mode.append(item); });
  config.append(mode, el('p', '', '仅调用检索接口，不生成答案。返回前 5 个片段；分数仅能在同一检索方式下比较，不表示正确率。'));
  const area = el('section'), form = el('form', 'query-box'), input = el('textarea'); input.placeholder = '输入一个典型问题，测试资料的召回效果…'; input.required = true; input.maxLength = 2000; input.setAttribute('aria-label', '检索问题');
  const bottom = el('div', 'form-bottom'), submit = el('button', 'primary', '开始检索'); submit.type = 'submit'; bottom.append(el('span', '', '结果仅来自所选知识库'), submit); form.append(input, bottom);
  const results = el('div'); results.setAttribute('aria-live', 'polite'); results.append(blank('检查每一次命中的依据', '检索后可查看片段、来源与排序分数。'));
  form.onsubmit = async event => {
    event.preventDefault(); if (!requireConnection() || state.busy) return;
    if (!state.scope) { notify('请先选择一个知识库。'); return; }
    state.busy = true; submit.disabled = true; results.replaceChildren(el('div', 'loading', '正在检索资料…'));
    try {
      const result = await api.request('/search', { method: 'POST', body: { query: input.value.trim(), knowledge_base_ids: [state.scope], mode: mode.value, top_k: 5 } });
      results.replaceChildren(el('p', 'muted', `找到 ${result.hits.length} 个片段 · ${result.score_type}${result.degraded ? ' · 已降级' : ''}`));
      if (result.degraded) results.append(el('div', 'warning', '本次检索发生降级，请结合后端说明核查。'));
      if (!result.hits.length) results.append(blank('未找到相关片段', '检查资料处理状态、知识范围，或换一种提问方式。'));
      result.hits.forEach((hit, index) => {
        const card = el('article', 'result-card'), header = el('header');
        header.append(button(`${index + 1}. ${hit.title}`, 'text-button', () => openSource(hit.chunk_id), 'file'), el('span', 'result-score', `${hit.score_type} · ${Number(hit.score).toFixed(4)}`));
        card.append(header, el('p', '', hit.excerpt), el('div', 'source-location', locatorText(hit.locator))); results.append(card);
      });
    } catch (error) { results.replaceChildren(failure(error)); }
    finally { state.busy = false; submit.disabled = false; }
  };
  area.append(form, results); layout.append(config, area); main.append(layout);
}
function renderChat() {
  const page = el('section', 'chat-page'), top = el('div', 'chat-top'); top.append(scopeSelect(), button('清空当前展示', 'text-button', () => navigate('chat'), 'refresh'));
  const hero = el('div', 'chat-hero'), mark = el('div', 'chat-emblem'); mark.append(icon('spark'));
  hero.append(mark, el('h1', '', '让知识，成为答案。'), el('p', '', '向你的知识库提问，获取有来源、可追溯的回答。'));
  const suggestions = el('div', 'suggestions'), input = el('textarea');
  ['会议室使用后需要做什么？', '预约会议室需要提前多久？', '帮我查找资料中的具体规定'].forEach(question => suggestions.append(button(question, 'suggestion', () => { input.value = question; input.focus(); }, 'spark')));
  const messages = el('div'); messages.setAttribute('aria-live', 'polite');
  const form = el('form', 'chat-compose'); input.placeholder = '你想从资料中了解什么？'; input.required = true; input.maxLength = 2000; input.setAttribute('aria-label', '输入问题');
  const bottom = el('div', 'form-bottom'), submit = el('button', 'primary'); submit.type = 'submit'; submit.append(document.createTextNode('发送问题'), icon('send'));
  bottom.append(el('span', '', '仅依据所选知识库 · 每次提问独立'), submit); form.append(input, bottom);
  form.onsubmit = async event => {
    event.preventDefault(); if (!requireConnection() || state.busy) return;
    const question = input.value.trim(); if (!question || !state.scope) { notify('请先选择知识库并输入问题。'); return; }
    state.busy = true; submit.disabled = true; hero.hidden = true; suggestions.hidden = true;
    const message = el('article', 'message'), response = el('div', 'answer-message'), waiting = el('div', 'loading'); waiting.append(el('span', 'spinner'), el('span', '', '正在检索与生成，请稍候…'));
    message.append(el('div', 'question-message', question), response); response.append(waiting); messages.append(message); input.value = '';
    try {
      const answer = await api.request('/qa', { method: 'POST', body: { question, knowledge_base_ids: [state.scope] } });
      response.replaceChildren(); const header = el('header'); header.append(icon('spark'), el('strong', '', answer.answer_status === 'no_evidence' ? '未找到回答依据' : '知识库回答'));
      response.append(header, el('div', 'answer-body', answer.answer));
      const citations = el('div', 'citations'); answer.citations.forEach(cite => citations.append(button(`${cite.index} · ${cite.title}`, 'citation-chip', () => openSource(cite.chunk_id), 'file')));
      response.append(citations);
      (answer.warnings || []).forEach(warning => response.append(el('div', 'warning', warning)));
      if (answer.retrieval?.degraded) response.append(el('div', 'warning', '本次回答使用了降级检索。'));
    } catch (error) { response.replaceChildren(failure(error)); }
    finally { state.busy = false; submit.disabled = false; }
  };
  page.append(top, hero, suggestions, messages, form, el('p', 'chat-disclaimer', '回答可能存在偏差，请结合引用原文核对。无证据、引用校验失败与模型故障会分别展示，不伪装为成功。'));
  main.append(page);
}
async function openSource(id) {
  const dialog = document.querySelector('#source-dialog'), content = document.querySelector('#source-content');
  content.replaceChildren(el('div', 'loading', '正在读取当前可访问的原文…')); dialog.showModal();
  try {
    const chunk = await api.request(`/chunks/${id}`);
    content.replaceChildren(el('h3', '', chunk.title), el('div', 'source-location', locatorText(chunk.locator)), el('div', 'source-text', chunk.text));
  } catch (error) { content.replaceChildren(failure(error)); }
}
document.querySelectorAll('[data-page]').forEach(node => node.addEventListener('click', () => navigate(node.dataset.page)));
document.querySelector('.brand').addEventListener('click', event => { event.preventDefault(); navigate('knowledge'); });
document.querySelector('#connection').addEventListener('click', () => {
  if (state.connected) { disconnect(); notify('已断开连接，并清除页面中的令牌与问答展示。'); }
  connection.showModal();
});
document.querySelector('#top-connection').addEventListener('click', () => document.querySelector('#connection').click());
document.querySelector('#help').addEventListener('click', () => notify('先在启动终端输入 /token，连接后新建或打开知识库 → 上传文档 → 等待就绪 → 检索或提问。开发体验请只使用自制资料。'));
document.querySelector('#connection-form').addEventListener('submit', async event => {
  event.preventDefault(); const submit = document.querySelector('#connect-submit'), field = document.querySelector('#token');
  submit.disabled = true; document.querySelector('#connect-error').textContent = ''; api.token = field.value.trim().replace(/^Bearer\s+/i, '');
  try {
    await loadLibraries(); state.connected = true; field.value = ''; state.revision++;
    document.querySelector('#connection-label').textContent = '已连接 · 开发身份'; connection.close(); render(); notify('已连接后端，正在展示当前身份的数据。');
  } catch (error) { api.token = ''; document.querySelector('#connect-error').textContent = error instanceof ApiError ? error.message : '连接失败，请检查后端。'; }
  finally { submit.disabled = false; }
});
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); main.querySelector('input,textarea')?.focus(); }
});
render();
