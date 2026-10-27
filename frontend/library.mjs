// 知识库列表与知识库详情（文件管理）：卡片网格 + 文件表格 + 上传区。
import {
  el, icon, toast, emptyState, loading, fmtDate, relativeCreated, actionButton, iconButton,
  openMenu, menuItem,
} from './dom.mjs';
import { jobLabel, reindexTarget } from './core.mjs';

const ACCEPT = '.txt,.md,.pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff';
const PATTERN = /\.(txt|md|pdf|docx|png|jpe?g|tiff?)$/i;
const MAX_BYTES = 50 * 1024 * 1024;

/* ---------------------------------------------------------------- 列表页 */

export function renderKnowledge(app) {
  const page = el('div', 'page');
  const head = el('div', 'page-head');
  const text = el('div');
  text.append(el('h1', '', '知识库'), el('p', '', '把分散的资料，整理成随时可检索、可引用的知识空间。'));
  const actions = el('div', 'head-actions');
  const create = actionButton('新建知识库', 'btn primary', () => app.createLibrary(), 'plus');
  actions.append(create);
  head.append(text, actions);
  page.append(head);

  if (!app.state.connected) {
    page.append(connectState(app));
    return page;
  }

  const shoulder = el('div', 'shoulder');
  const field = el('label', 'search-field');
  const input = el('input');
  input.type = 'search';
  input.placeholder = '搜索知识库';
  input.setAttribute('aria-label', '搜索知识库');
  input.value = app.state.kbFilter;
  field.append(icon('search'), input);
  const count = el('span', 'count-note');
  shoulder.append(field, el('div', 'spacer'), count);
  page.append(shoulder);

  const grid = el('div', 'card-grid');
  page.append(grid);

  const paint = () => {
    const keyword = app.state.kbFilter.trim().toLowerCase();
    const items = app.state.libraries
      .filter(kb => !keyword || kb.name.toLowerCase().includes(keyword))
      .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
    count.textContent = keyword
      ? `匹配 ${items.length} / ${app.state.libraries.length} 个知识库`
      : `共 ${app.state.libraries.length} 个知识库`;
    grid.replaceChildren();
    if (!items.length) {
      const cta = el('button', 'btn primary', '新建知识库');
      cta.type = 'button';
      cta.addEventListener('click', () => app.createLibrary());
      grid.append(app.state.libraries.length
        ? emptyState('没有匹配的知识库', '换个关键词，或清空搜索后查看全部。')
        : emptyState('从第一个知识库开始', '新建知识库并上传资料，处理完成后即可检索与问答。', cta));
      return;
    }
    items.forEach((kb, index) => grid.append(card(app, kb, index)));
  };
  input.addEventListener('input', () => { app.state.kbFilter = input.value; paint(); });
  paint();
  return page;
}

function connectState(app) {
  const cta = el('button', 'btn primary', '连接工作台');
  cta.type = 'button';
  cta.addEventListener('click', () => app.openConnection());
  return emptyState(
    '连接后，知识就在这里',
    '使用启动终端里的开发令牌连接，随后即可新建知识库、上传资料并开始问答。',
    cta,
  );
}

function card(app, kb, index) {
  const node = el('article', 'kb-card');

  const top = el('div', 'card-top');
  const mark = el('span', `card-icon tone-${index % 3}`);
  mark.append(icon('library'));
  top.append(mark);

  const menuBtn = iconButton('more', `管理知识库 ${kb.name}`, () => {
    openMenu(menuBtn, menu => {
      menu.append(el('div', 'menu-title', kb.name));
      menu.append(menuItem('打开知识库', { glyph: 'folder', onClick: () => app.openLibrary(kb) }));
      menu.append(menuItem('在此知识库提问', { glyph: 'chat', onClick: () => openChat(app, kb) }));
      menu.append(el('div', 'menu-sep'));
      menu.append(menuItem('删除知识库', { glyph: 'trash', danger: true, onClick: () => removeLibrary(app, kb) }));
    }, { align: 'end' });
  }, 'icon-btn card-menu-btn');
  top.append(menuBtn);
  node.append(top);

  const open = el('button', 'card-open', kb.name);
  open.type = 'button';
  open.title = kb.name;
  open.addEventListener('click', () => app.openLibrary(kb));
  node.append(open);

  node.append(el('p', 'card-sub', relativeCreated(kb.created_at)));

  const foot = el('div', 'card-foot');
  foot.append(el('span', 'tag', '个人知识库'), el('span', 'tag gray', '混合检索'));
  node.append(foot);
  return node;
}

function openChat(app, kb) {
  app.state.scope = kb.id;
  app.navigate('chat');
}

export function removeLibrary(app, kb) {
  app.actionDialog({
    title: '删除知识库',
    description: `确定删除“${kb.name}”及其中的资料吗？提交后立即阻断访问，随后异步清理，且不可撤销。`,
    confirmText: '确认删除',
    danger: true,
    action: async () => {
      await app.api.request(`/knowledge-bases/${kb.id}`, { method: 'DELETE' });
      if (app.state.library?.id === kb.id) app.state.library = null;
      app.state.receipts.clear();
      await app.loadLibraries();
      app.render();
      toast('知识库已停止访问，正在后台清理。');
    },
  });
}

/* ---------------------------------------------------------------- 详情页 */

export function renderKnowledgeDetail(app) {
  const kb = app.state.library;
  const page = el('div', 'page');

  const crumbs = el('div', 'crumbs');
  const back = el('button');
  back.type = 'button';
  back.append(icon('arrow-left'), el('span', '', '全部知识库'));
  back.addEventListener('click', () => app.navigate('knowledge'));
  crumbs.append(back, el('span', 'sep', '/'), el('strong', '', kb.name));
  page.append(crumbs);

  const head = el('div', 'page-head');
  const text = el('div');
  text.append(el('h1', '', kb.name));
  text.append(el('p', '', `创建于 ${fmtDate(kb.created_at)} · 上传资料后由后台解析、分段并向量化，发布完成才对外可检索。`));
  const actions = el('div', 'head-actions');
  actions.append(
    actionButton('在此知识库提问', 'btn ghost', () => openChat(app, kb), 'chat'),
  );
  head.append(text, actions);
  page.append(head);

  const tabs = el('div', 'tabs');
  const tabFiles = el('button', 'tab active', `文件`);
  tabFiles.type = 'button';
  const tabCount = el('span', 'count', String(app.state.documents.length));
  tabCount.id = 'doc-tab-count';
  tabFiles.append(tabCount);
  const tabSearch = el('button', 'tab', '检索');
  tabSearch.type = 'button';
  tabSearch.addEventListener('click', () => {
    app.state.scope = kb.id;
    app.navigate('search');
  });
  tabs.append(tabFiles, tabSearch);
  page.append(tabs);

  page.append(detailShoulder(app, kb));
  page.append(uploadZone(app, kb));
  page.append(feedbackBox());

  const panel = el('div', 'table-wrap');
  panel.id = 'document-list';
  panel.append(loading('正在读取文档…'));
  page.append(panel);

  // 首帧直接使用已有数据，避免切换到详情页时闪一下空表。
  queueMicrotask(() => drawDocuments(app));
  return page;
}

function detailShoulder(app, kb) {
  const shoulder = el('div', 'shoulder');
  const field = el('label', 'search-field');
  const input = el('input');
  input.type = 'search';
  input.placeholder = '按文件名筛选';
  input.setAttribute('aria-label', '按文件名筛选');
  input.value = app.state.docFilter;
  field.append(icon('search'), input);

  const refresh = iconButton('refresh', '刷新文档列表', () => app.loadDocuments());
  const upload = actionButton('上传文档', 'btn primary', () => {
    document.querySelector('.upload-zone input[type=file]')?.click();
  }, 'upload');

  const counter = el('span', 'count-note', `${app.state.documents.length} 个文档`);
  counter.id = 'doc-count';

  shoulder.append(field, el('div', 'spacer'), counter, refresh, upload);
  input.addEventListener('input', () => { app.state.docFilter = input.value; drawDocuments(app); });
  return shoulder;
}

function uploadZone(app, kb) {
  const zone = el('div', 'upload-zone');
  const copy = el('div', 'upload-copy');
  const mark = el('span', 'upload-mark');
  mark.append(icon('upload'));
  const text = el('div');
  text.append(el('strong', '', '添加知识资料'), el('p', '', 'TXT / MD / PDF / DOCX；扫描件需后端启用 DeepDoc · 单文件不超过 50 MB'));
  copy.append(mark, text);

  const input = el('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = ACCEPT;
  input.hidden = true;
  input.setAttribute('aria-label', '选择知识库文档');

  const actions = el('div', 'upload-actions');
  const choose = actionButton('选择文件', 'btn ghost', () => input.click(), 'upload');
  actions.append(choose, input);

  zone.append(copy, actions);

  input.addEventListener('change', () => {
    const files = [...input.files];
    input.value = '';
    uploadFiles(app, kb, files);
  });
  zone.addEventListener('dragover', event => {
    event.preventDefault();
    zone.classList.add('dragging');
  });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragging'));
  zone.addEventListener('drop', event => {
    event.preventDefault();
    zone.classList.remove('dragging');
    uploadFiles(app, kb, [...event.dataTransfer.files]);
  });
  return zone;
}

function feedbackBox() {
  const box = el('div', 'upload-feedback');
  box.id = 'upload-feedback';
  box.setAttribute('aria-live', 'polite');
  return box;
}

function feedback(message, kind = '') {
  const box = document.querySelector('#upload-feedback');
  if (!box) return;
  box.append(el('p', kind, message));
}

async function uploadFiles(app, kb, files) {
  if (!files.length) return;
  if (app.state.busy) { toast('上一个上传还在进行，请稍候。'); return; }
  document.querySelector('#upload-feedback')?.replaceChildren();
  app.state.busy = true;
  try {
    for (const file of files) {
      if (!PATTERN.test(file.name) || file.size > MAX_BYTES) {
        feedback(`${file.name}：已跳过，格式不支持或超过 50 MB。`, 'warning');
        continue;
      }
      const form = new FormData();
      form.append('file', file);
      try {
        const ack = await app.api.request(`/knowledge-bases/${kb.id}/documents`, { method: 'POST', body: form });
        app.state.receipts.set(ack.document_id, ack.version_id);
        feedback(`${file.name}：${ack.deduplicated ? '相同内容已存在，未新建任务' : '上传已接收，等待后台处理'}`);
      } catch (error) {
        feedback(`${file.name}：${app.errorText(error)}`, 'warning');
      }
    }
    await app.loadDocuments();
    toast('上传已接收，后台任务正在处理。');
  } finally {
    app.state.busy = false;
  }
}

/* ------------------------------------------------------------ 文件表格 */

export function drawDocuments(app) {
  const panel = document.querySelector('#document-list');
  if (!panel) return;

  const keyword = app.state.docFilter.trim().toLowerCase();
  const items = app.state.documents
    .filter(doc => doc.status === 'active')
    .filter(doc => !keyword || (doc.title || '').toLowerCase().includes(keyword));

  if (!items.length) {
    panel.replaceChildren(app.state.documents.length
      ? emptyState('没有匹配的文件', '换个文件名关键词再试一次。')
      : emptyState('还没有文档', '上传一份资料，处理成功后即可检索与提问。'));
    return;
  }

  const table = el('table', 'document-table');
  const thead = el('thead');
  const headRow = el('tr');
  ['文件', '处理状态', '已发布版本', '操作'].forEach(title => headRow.append(el('th', '', title)));
  thead.append(headRow);

  const tbody = el('tbody');
  items.forEach(doc => tbody.append(documentRow(app, doc)));
  table.append(thead, tbody);

  const scroll = el('div', 'table-scroll');
  scroll.append(table);
  panel.replaceChildren(scroll);
}

function documentRow(app, doc) {
  const row = el('tr');

  const nameCell = el('td');
  const cell = el('div', 'file-cell');
  const mark = el('span', 'file-ico');
  mark.append(icon('file'));
  const meta = el('div', 'file-meta');
  meta.append(el('strong', 'file-name', doc.title));
  meta.append(el('small', 'file-sub', `代次 ${doc.generation}${doc.active_build_id ? ' · 已有可用内容' : ' · 暂无发布内容'}`));
  cell.append(mark, meta);
  nameCell.append(cell);

  const statusCell = el('td');
  const badge = el('span', `status ${doc.job?.state || ''}`);
  badge.append(el('i'), document.createTextNode(jobLabel(doc)));
  statusCell.append(badge);
  if (doc.job?.state === 'failed') {
    statusCell.append(el('small', 'status-detail', doc.job.last_error?.code || '处理失败'));
  }

  const versionCell = el('td', 'version-cell');
  versionCell.append(document.createTextNode(doc.version_no ? `V${doc.version_no}` : '尚未发布'));
  versionCell.append(el('small', '', doc.version_no ? '已对外可检索' : '等待首次发布'));

  const actionsCell = el('td');
  actionsCell.append(actionBar(app, doc));

  row.append(nameCell, statusCell, versionCell, actionsCell);
  return row;
}

function actionBar(app, doc) {
  const bar = el('div', 'row-actions');

  const fileInput = el('input');
  fileInput.type = 'file';
  fileInput.accept = ACCEPT;
  fileInput.hidden = true;
  fileInput.setAttribute('aria-label', `更新 ${doc.title}`);
  fileInput.addEventListener('change', () => {
    const selected = fileInput.files[0];
    fileInput.value = '';
    if (selected) replaceVersion(app, doc, selected);
  });

  const update = el('button', 'link-btn', '更新');
  update.type = 'button';
  update.addEventListener('click', () => fileInput.click());

  const target = reindexTarget(doc, app.state.receipts);
  const rebuild = el('button', 'link-btn', '重建');
  rebuild.type = 'button';
  rebuild.disabled = !target || ['running', 'queued'].includes(doc.job?.state);
  rebuild.title = target ? '按原模型与分段配置创建新构建' : '缺少失败版本的上传回执，不能把旧发布版本当作恢复目标';
  rebuild.addEventListener('click', async () => {
    rebuild.disabled = true;
    try {
      const ack = await app.api.request(`/documents/${doc.id}/reindex`, {
        method: 'POST',
        body: { version_id: target, expected_generation: doc.generation },
      });
      app.state.receipts.set(doc.id, ack.version_id);
      await app.loadDocuments();
      toast('已创建新构建，旧发布内容保持可用。');
    } catch (error) {
      app.notify(app.errorText(error));
    } finally {
      rebuild.disabled = false;
    }
  });

  const chunks = el('button', 'link-btn', '分块');
  chunks.type = 'button';
  // 没有已发布内容时明确置灰，而不是点开一个空抽屉。
  chunks.disabled = !doc.active_build_id;
  chunks.title = doc.active_build_id ? '查看当前版本的分段结果' : '还没有已发布内容，无法查看分块';
  chunks.addEventListener('click', () => app.openChunks(doc));

  const remove = el('button', 'link-btn danger', '删除');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    app.actionDialog({
      title: '删除文档',
      description: `确定删除“${doc.title}”吗？删除提交后，该文档及其引用立即停止访问。`,
      confirmText: '确认删除',
      danger: true,
      action: async () => {
        await app.api.request(`/documents/${doc.id}`, { method: 'DELETE' });
        app.state.receipts.delete(doc.id);
        await app.loadDocuments();
        toast('文档已停止访问，正在后台清理。');
      },
    });
  });

  bar.append(update, fileInput, rebuild, chunks, remove);
  return bar;
}

async function replaceVersion(app, doc, file) {
  if (!PATTERN.test(file.name) || file.size > MAX_BYTES) {
    toast('文件不支持或超过 50 MB，未上传。');
    return;
  }
  if (app.state.busy) { toast('另一个上传仍在进行，请稍候。'); return; }
  app.state.busy = true;
  try {
    const form = new FormData();
    form.append('file', file);
    const ack = await app.api.request(
      `/documents/${doc.id}/versions?expected_generation=${doc.generation}`,
      { method: 'POST', body: form },
    );
    app.state.receipts.set(doc.id, ack.version_id);
    await app.loadDocuments();
    toast(ack.deduplicated ? '相同内容已存在，未新建版本。' : '新版本已接收，发布前旧版本继续可用。');
  } catch (error) {
    app.notify(app.errorText(error));
  } finally {
    app.state.busy = false;
  }
}
