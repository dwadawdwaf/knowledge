// 应用装配：状态、路由、数据加载、连接与轮询。视图逻辑在各自模块中。
import { ApiClient, ApiError, turnsFromMessages } from './core.mjs';
import { el, icon, hydrateIcons, toast, openMenu, menuItem } from './dom.mjs';
import { renderChat } from './chat.mjs';
import { renderKnowledge, renderKnowledgeDetail, drawDocuments } from './library.mjs';
import { renderFiles, updateFilesView } from './files.mjs';
import { renderBrowse, loadBrowse, emptyBrowse, disposeBrowse } from './browse.mjs';
import { renderEvaluation } from './evaluation.mjs';
import { openChunks, openSource } from './reader.mjs';

const main = document.querySelector('#content');
const api = new ApiClient();

const state = {
  page: 'knowledge',
  // 当前已渲染视图的标识（见 viewKey）：用于区分「同名页换了内容」和「重复点同一个入口」。
  renderedKey: '',
  browsePushed: false,
  libraries: [],
  library: null,
  libraryTab: 'files',
  documents: [],
  docSignature: '',
  // 首次/手动读取文档列表：为真时列表区显示「正在读取」，而不是空态
  docLoading: false,
  docError: null,
  allFiles: [],
  fileErrors: [],
  browse: emptyBrowse(),
  fileLoading: false,
  fileLoaded: false,
  fileFilter: '',
  fileKb: '',
  kbFilter: '',
  docFilter: '',
  scope: '',
  query: '',
  mode: 'hybrid',
  searchResult: null,
  sessions: [],
  sessionId: '',
  historySelection: '',
  conversationsError: null,
  receipts: new Map(),
  connected: false,
  busy: false,
  pollError: false,
};

let pollTimer = null;
let pollBusy = false;

/* ------------------------------------------------------------ 错误与提示 */

function errorText(error) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return '操作未完成，请稍后重试。';
}

function errorBox(error) {
  if (error?.status === 401) {
    disconnect(true);
  }
  const box = el('div', 'error-box', errorText(error));
  box.setAttribute('role', 'alert');
  if (error?.requestId) box.append(el('small', '', `请求编号：${error.requestId}`));
  return box;
}

function notify(message) {
  toast(message);
}

/* -------------------------------------------------------------- 数据加载 */

async function loadLibraries() {
  const result = await api.request('/knowledge-bases');
  state.libraries = (result.items || []).filter(kb => kb.status === 'active');
  if (state.scope && !state.libraries.some(kb => kb.id === state.scope)) state.scope = '';
  if (state.library && !state.libraries.some(kb => kb.id === state.library.id)) state.library = null;
  if (state.fileKb && !state.libraries.some(kb => kb.id === state.fileKb)) state.fileKb = '';
  // 知识库集合变化后，全部文件的汇总结果也要重新读取。
  state.fileLoaded = false;
}

/**
 * 列表内容指纹：轮询只在数据真的变了才重绘。
 * 每 2.5 秒无差别重建表格，会把用户正要点的按钮换成新节点，表现就是「点了没反应」。
 */
function documentsSignature(items) {
  return items
    .map(doc => [
      doc.id, doc.status, doc.generation, doc.version_no, doc.active_build_id,
      doc.job?.state, doc.job?.stage, doc.job?.last_error?.code,
    ].join(':'))
    .join('|');
}

async function loadDocuments(quiet = false) {
  const library = state.library;
  if (!library || !state.connected) return;
  // 只有显式加载才占用「空列表」的占位；轮询静默进行，避免空库每 2.5 秒闪一次读取中
  if (!quiet) state.docLoading = true;
  pollBusy = true;
  try {
    const result = await api.request(`/knowledge-bases/${library.id}/documents`, { timeout: 15000 });
    if (state.library?.id !== library.id) return;
    const items = (result.items || []).filter(doc => doc.status === 'active');
    const signature = documentsSignature(items);
    const unchanged = signature === state.docSignature;
    state.documents = items;
    state.docSignature = signature;
    state.docError = null;
    state.pollError = false;
    if (state.page === 'knowledge' && !unchanged) {
      drawDocuments(app);
      const counter = document.querySelector('#doc-count');
      if (counter) counter.textContent = `${state.documents.length} 个文档`;
      const tabCount = document.querySelector('#doc-tab-count');
      if (tabCount) tabCount.textContent = String(state.documents.length);
    }
  } catch (error) {
    if (error.status !== 401 && (!quiet || !state.pollError)) notify(errorText(error));
    state.docError = error;
    state.pollError = true;
  } finally {
    pollBusy = false;
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/**
 * 汇总一个知识库的文档；超时/抖动重试一次再判失败。
 * 远端库经隧道访问时会偶发慢查询，单次失败不足以说明这个库是空的。
 */
async function fetchKbDocuments(kb) {
  let lastError = null;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const data = await api.request(`/knowledge-bases/${kb.id}/documents`, { timeout: 20000 });
      return { kb, items: data.items || [], error: null };
    } catch (error) {
      lastError = error;
      if (error.status === 401) break;
      if (attempt === 0) await sleep(400);
    }
  }
  return { kb, items: [], error: lastError };
}

let filesRequest = 0;

async function loadAllFiles(quiet = false) {
  if (!state.connected || !state.libraries.length) return;
  // 连点刷新会并发两轮：只让最后发起的那一轮写结果，避免旧结果覆盖新结果。
  const seq = ++filesRequest;
  state.fileLoading = true;
  state.fileErrors = [];
  updateFilesView(app);
  try {
    const results = await Promise.all(state.libraries.map(fetchKbDocuments));
    if (seq !== filesRequest) return;
    state.fileErrors = results
      .filter(result => result.error)
      .map(result => ({ name: result.kb.name, message: errorText(result.error) }));
    state.allFiles = results.flatMap(({ kb, items }) => items
      .filter(doc => doc.status === 'active')
      .map(doc => ({ ...doc, kb_name: kb.name })));
    if (!quiet && state.fileErrors.length) {
      notify(`有 ${state.fileErrors.length} 个知识库未能读取，列表只是部分结果。`);
    }
  } finally {
    if (seq === filesRequest) {
      state.fileLoading = false;
      state.fileLoaded = true;
      updateFilesView(app);
    }
  }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    // 避免刷新移除正在操作的按钮、打断键盘焦点，或在弹窗打开时重绘。
    if (document.hidden || pollBusy || state.busy) return;
    if (document.querySelector('dialog[open]')) return;
    if (document.activeElement?.closest('#document-list')) return;
    loadDocuments(true);
  }, 2500);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

/* ---------------------------------------------------------------- 会话 */

function uid() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function currentSession() {
  return state.sessions.find(item => item.id === state.sessionId) || null;
}

function ensureSession() {
  let session = currentSession();
  if (session) return session;
  session = draftSession();
  state.sessions.unshift(session);
  state.sessionId = session.id;
  return session;
}

function draftSession() {
  return {
    id: uid(), title: '', turns: [], createdAt: new Date(), serverId: false, loaded: true,
  };
}

function newSession() {
  if (currentSession() && !currentSession().turns.length) {
    navigate('chat');
    return;
  }
  const session = draftSession();
  state.sessions.unshift(session);
  state.sessionId = session.id;
  navigate('chat');
}

/** 连接后从后端恢复会话列表；选中最近一条并加载消息。 */
async function loadConversations() {
  const result = await api.request('/conversations');
  const items = result.items || [];
  state.sessions = items.map(item => ({
    id: item.id,
    title: item.title || '',
    createdAt: item.created_at ? new Date(item.created_at) : new Date(),
    turns: [],
    serverId: true,
    loaded: false,
  }));
  if (!state.sessions.length) {
    const draft = draftSession();
    state.sessions = [draft];
    state.sessionId = draft.id;
    return;
  }
  state.sessionId = state.sessions[0].id;
  await loadSession(state.sessions[0]);
}

async function loadSession(session) {
  if (!session?.serverId || session.loaded) return;
  session.loading = true;
  try {
    const data = await api.request(`/conversations/${session.id}`);
    session.title = data.title || session.title;
    session.turns = turnsFromMessages(data.messages || []);
    session.loaded = true;
  } finally {
    session.loading = false;
  }
}

/** 首次提问时创建会话，之后的问题都带 conversation_id 写入历史。 */
async function ensureConversation(session, title) {
  if (session.serverId) return session;
  const created = await api.request('/conversations', {
    method: 'POST',
    body: { title: String(title || '').slice(0, 200) },
  });
  session.id = created.id;
  session.serverId = true;
  session.loaded = true;
  if (!session.title) session.title = created.title || String(title || '').slice(0, 40);
  state.sessionId = session.id;
  return session;
}

async function selectSession(session) {
  state.sessionId = session.id;
  if (!session.loaded) session.loading = true;
  navigate('chat', { historySelection: session.id });
  closeMobileNav();
  if (session.loaded) return;
  try {
    await loadSession(session);
  } catch (error) {
    notify(`对话历史读取失败：${errorText(error)}`);
  } finally {
    session.loading = false;
  }
  if (state.sessionId === session.id) render();
}

function renderSessions() {
  const list = document.querySelector('#session-list');
  const empty = document.querySelector('#session-empty');
  const count = document.querySelector('#session-count');
  if (!list) return;
  list.replaceChildren();
  if (count) count.textContent = state.sessions.length ? String(state.sessions.length) : '';
  if (empty) empty.hidden = state.sessions.length > 0;

  state.sessions.forEach(session => {
    const active = state.page === 'chat' && session.id === state.historySelection;
    const item = el('button', active ? 'session-item active' : 'session-item');
    if (active) item.setAttribute('aria-current', 'page');
    item.type = 'button';
    item.title = session.title || '新对话';
    item.append(icon('chat'), el('span', '', session.title || '新对话'));
    item.addEventListener('click', () => selectSession(session));
    list.append(item);
  });
}

/* ---------------------------------------------------------------- 路由 */

function keyFor(page) {
  // 知识库列表与详情共用一个页名；对话页共用页名但会切换会话。两者都要进标识。
  if (page === 'knowledge') return state.library ? `knowledge:${state.library.id}:${state.libraryTab}` : 'knowledge:list';
  if (page === 'chat') return `chat:${state.sessionId || 'new'}`;
  return page;
}

function viewKey() {
  return keyFor(state.page);
}

function navigate(page, { historySelection = '' } = {}) {
  state.historySelection = page === 'chat' ? historySelection : '';
  renderSessions();
  if (page === 'chat' && !state.sessionId) ensureSession();
  // 只有真正停在同一个视图上才跳过重绘；同名页换了内容（详情→列表、换会话）必须重画。
  if (keyFor(page) === state.renderedKey) { closeMobileNav(); return; }
  if (state.page === 'browse') {
    disposeBrowse(app);
    state.browse = emptyBrowse();
    state.browsePushed = false;
  }
  stopPolling();
  state.page = page;
  state.pollError = false;
  history.replaceState(null, '', `#${page}`);
  render();
  if (page === 'knowledge' && state.library) startPolling();
  closeMobileNav();
}

function openBrowse(doc, focus = null) {
  disposeBrowse(app);  // 换文件时释放上一份的 blob URL
  // 从知识库详情进来时，返回应该回到那个知识库，而不是「全部文件」。
  const fromLibrary = state.page === 'knowledge' && state.library ? state.library : null;
  state.browse = {
    ...emptyBrowse(),
    doc,
    loading: true,  // 首帧就显示「正在读取」，不要先闪一下空态
    focusOrdinal: focus?.ordinal ?? null,
    focusLocator: focus?.locator ?? null,
    returnPage: fromLibrary ? 'knowledge' : 'files',
    returnLabel: fromLibrary ? `返回「${fromLibrary.name}」` : '全部文件',
  };
  state.page = 'browse';
  stopPolling();
  // 压入历史而不是替换：浏览器返回键与页面内的「全部文件」都能回到列表。
  history.pushState({ page: 'browse' }, '', '#browse');
  state.browsePushed = true;
  render();
  loadBrowse(app);
  closeMobileNav();
}

function closeBrowse() {
  if (state.browsePushed && location.hash === '#browse') {
    state.browsePushed = false;
    history.back();  // 由 hashchange 统一收口，避免两处各画一次
    return;
  }
  const back = state.browse?.returnPage === 'knowledge' && state.library ? 'knowledge' : 'files';
  disposeBrowse(app);
  state.browse = emptyBrowse();
  state.page = back;
  history.replaceState(null, '', `#${back}`);
  render();
}

function openLibrary(kb) {
  state.library = kb;
  state.libraryTab = 'files';
  state.searchResult = null;
  state.documents = [];
  state.docSignature = '';
  state.docError = null;
  state.docFilter = '';
  state.page = 'knowledge';
  stopPolling();
  history.replaceState(null, '', '#knowledge');
  render();
  startPolling();
  loadDocuments();
  closeMobileNav();
}

function openLibraryById(kbId) {
  const kb = state.libraries.find(item => item.id === kbId);
  if (kb) openLibrary(kb);
}

function render() {
  renderSessions();
  // 浏览页高亮来源入口：从知识库进来就高亮「知识库」，从全部文件进来就高亮「全部文件」。
  const activePage = state.page === 'browse'
    ? (state.browse.returnPage === 'knowledge' ? 'knowledge' : 'files')
    : state.page;
  document.querySelectorAll('[data-page]').forEach(node => {
    const active = node.dataset.page === activePage;
    node.classList.toggle('active', active);
    if (active) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });

  main.replaceChildren();
  if (state.page === 'chat') main.append(renderChat(app));
  else if (state.page === 'browse' && state.browse.doc) main.append(renderBrowse(app));
  else if (state.page === 'files') main.append(renderFiles(app));
  else if (state.page === 'search') main.append(renderEvaluation(app));
  else if (state.library) main.append(renderKnowledgeDetail(app));
  else main.append(renderKnowledge(app));
  state.renderedKey = viewKey();
  main.scrollTop = 0;
}

/* -------------------------------------------------------------- 交互弹窗 */

function actionDialog({ title, description = '', input = false, value = '', placeholder = '', confirmText = '确认', danger = false, validate, action }) {
  const dialog = document.querySelector('#action-dialog');
  const form = document.querySelector('#action-form');
  const desc = document.querySelector('#action-description');
  const field = document.querySelector('#action-input');
  const label = document.querySelector('#action-label');
  const error = document.querySelector('#action-error');
  const submit = document.querySelector('#action-submit');

  document.querySelector('#action-title').textContent = title;
  desc.textContent = description;
  desc.hidden = !description;
  field.hidden = !input;
  label.hidden = !input;
  field.required = Boolean(input);
  field.value = value;
  field.placeholder = placeholder || '';
  error.textContent = '';
  submit.textContent = confirmText;
  submit.className = danger ? 'btn danger' : 'btn primary';

  form.onsubmit = async event => {
    event.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      if (validate) validate(field.value);
      await action(field.value);
      dialog.close();
    } catch (error_) {
      error.textContent = errorText(error_);
    } finally {
      submit.disabled = false;
    }
  };

  dialog.showModal();
  if (input) setTimeout(() => field.focus(), 20);
  else dialog.focus();
}

function createLibrary() {
  if (!requireConnection()) return;
  actionDialog({
    title: '新建知识库',
    description: '为资料建立一个独立的知识空间，名称只用于本地识别。',
    input: true,
    value: '',
    placeholder: '例如：行政制度',
    confirmText: '创建知识库',
    validate: text => {
      if (!text.trim()) throw new Error('请输入知识库名称。');
    },
    action: async text => {
      await api.request('/knowledge-bases', { method: 'POST', body: { name: text.trim() } });
      await loadLibraries();
      render();
      notify('知识库已创建，可以开始添加资料。');
    },
  });
}

/* ------------------------------------------------------------ 连接与账号 */

const connectionDialog = document.querySelector('#connection-dialog');
const connectionLabel = document.querySelector('#connection-label');

function openConnection() {
  document.querySelector('#connect-error').textContent = '';
  document.querySelector('#token').value = '';
  if (!connectionDialog.open) connectionDialog.showModal();
  setTimeout(() => document.querySelector('#token').focus(), 20);
}

function requireConnection() {
  if (state.connected) return true;
  openConnection();
  return false;
}

function disconnect(silent = false) {
  stopPolling();
  api.token = '';
  state.connected = false;
  state.libraries = [];
  state.library = null;
  state.documents = [];
  state.docSignature = '';
  state.allFiles = [];
  state.fileLoaded = false;
  state.searchResult = null;
  state.sessions = [];
  state.sessionId = '';
  state.conversationsError = null;
  state.receipts.clear();
  connectionLabel.textContent = '未连接';
  if (!silent) renderSessions();
  render();
}

/* -------------------------------------------------------------- 移动端 */

function closeMobileNav() {
  document.querySelector('#app').classList.remove('nav-open');
  document.querySelector('#scrim').hidden = true;
}

function toggleMobileNav() {
  const app = document.querySelector('#app');
  const open = app.classList.toggle('nav-open');
  document.querySelector('#scrim').hidden = !open;
}

/* ---------------------------------------------------------------- 装配 */

const app = {
  api,
  state,
  navigate,
  render,
  refreshView: () => render(),
  renderSessions,
  currentSession,
  ensureSession,
  ensureConversation,
  loadConversations,
  loadSession,
  selectSession,
  newSession,
  notify,
  errorBox,
  errorText,
  actionDialog,
  createLibrary,
  requireConnection,
  openConnection,
  loadLibraries,
  loadDocuments,
  loadAllFiles,
  openLibrary,
  openLibraryById,
  openLibrarySearch: (kbId, query = '') => {
    if (!state.libraries.some(kb => kb.id === kbId)) { notify('评测资料已删除或不再可访问。'); return; }
    openLibraryById(kbId);
    state.libraryTab = 'search';
    state.query = query;
    state.searchResult = null;
    render();
  },
  openKnowledgeList: () => { state.library = null; navigate('knowledge'); },
  openBrowse: (doc, focus) => openBrowse(doc, focus),
  closeBrowse: () => closeBrowse(),
  openChunks: doc => openChunks(app, doc),
  openSource: id => openSource(app, id),
};

hydrateIcons();

document.querySelectorAll('[data-page]').forEach(node => {
  node.addEventListener('click', () => {
    if (node.dataset.page === 'knowledge') state.library = null;
    navigate(node.dataset.page);
  });
});

document.querySelector('#brand').addEventListener('click', event => {
  event.preventDefault();
  state.library = null;
  navigate('knowledge');
});

document.querySelector('#new-chat').addEventListener('click', () => { newSession(); closeMobileNav(); });
document.querySelector('#mobile-new-chat').addEventListener('click', () => { newSession(); closeMobileNav(); });
document.querySelector('#nav-toggle').addEventListener('click', toggleMobileNav);
document.querySelector('#scrim').addEventListener('click', closeMobileNav);

document.querySelector('#collapse').addEventListener('click', event => {
  const collapsed = document.querySelector('#app').classList.toggle('collapsed');
  const button = event.currentTarget;
  const label = collapsed ? '展开侧边栏' : '收起侧边栏';
  button.setAttribute('aria-label', label);
  button.title = label;
});

document.querySelector('#theme-toggle').addEventListener('click', event => {
  const root = document.documentElement;
  const dark = root.dataset.theme === 'dark';
  root.dataset.theme = dark ? 'light' : 'dark';
  const button = event.currentTarget;
  button.replaceChildren(icon(dark ? 'moon' : 'sun'));
});

document.querySelector('#connection').addEventListener('click', event => {
  if (!state.connected) { openConnection(); return; }
  const anchor = event.currentTarget;
  openMenu(anchor, menu => {
    menu.append(el('div', 'menu-title', '开发身份已连接'));
    menu.append(menuItem('重新连接', { glyph: 'refresh', onClick: openConnection }));
    menu.append(el('div', 'menu-sep'));
    menu.append(menuItem('断开连接', {
      glyph: 'x',
      danger: true,
      onClick: () => {
        disconnect();
        notify('已断开连接，页面中的令牌与问答展示已清空。');
      },
    }));
  }, { align: 'end' });
});

document.querySelectorAll('[data-close]').forEach(node => {
  node.addEventListener('click', () => node.closest('dialog')?.close());
});

// 点遮罩关闭：目标是 dialog 本体说明落在 backdrop 上，避免“点了没反应”。
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
});

async function connect(value) {
  api.token = String(value || '').trim().replace(/^Bearer\s+/i, '');
  try {
    await loadLibraries();
    state.connected = true;
    try {
      await loadConversations();
      state.conversationsError = null;
    } catch (error) {
      // 历史读取失败不应阻断连接；页面会保留新对话入口。
      state.conversationsError = error;
    }
    // 标签最后再改：冒烟与用户都以「已连接」作为可交互信号，此时列表必须已画好。
    render();
    renderSessions();
    connectionLabel.textContent = '已连接 · 开发身份';
    return true;
  } catch (error) {
    api.token = '';
    throw error;
  }
}

document.querySelector('#connection-form').addEventListener('submit', async event => {
  event.preventDefault();
  const submit = document.querySelector('#connect-submit');
  const field = document.querySelector('#token');
  const error = document.querySelector('#connect-error');
  submit.disabled = true;
  error.textContent = '';
  try {
    await connect(field.value);
    field.value = '';
    connectionDialog.close();
    render();
    renderSessions();
    notify('已连接后端，正在展示当前身份的数据。');
  } catch (error_) {
    error.textContent = errorText(error_);
  } finally {
    submit.disabled = false;
  }
});

document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    main.querySelector('input:not([type=file]), textarea')?.focus();
  }
  if (event.key === 'Escape') closeMobileNav();
});

window.addEventListener('hashchange', () => {
  const page = location.hash.replace('#', '');
  if (['chat', 'knowledge', 'files', 'search'].includes(page) && page !== state.page) navigate(page);
});

function consumeStartupToken() {
  const match = /^#t=(.+)$/.exec(location.hash);
  if (!match) return '';
  // 读到就抹掉：令牌不留在地址栏，也不会随刷新再次出现。
  history.replaceState(null, '', location.pathname + location.search);
  return decodeURIComponent(match[1]);
}

async function bootstrap() {
  const token = consumeStartupToken();
  const initial = location.hash.replace('#', '');
  if (['chat', 'knowledge', 'files', 'search'].includes(initial)) {
    state.page = initial;
    if (initial === 'chat') ensureSession();
  }
  render();
  renderSessions();
  if (!token) { openConnection(); return; }
  try {
    await connect(token);
    render();
    renderSessions();
    notify('已自动连接本次工作台。');
  } catch (error) {
    notify(`自动连接失败：${errorText(error)}`);
    openConnection();
  }
}

bootstrap();
