// 全部文件：把各知识库的文档汇总到一张表，只做只读视图 + 分块预览。
import { el, icon, emptyState, loading, actionButton, openMenu, menuItem } from './dom.mjs';
import { jobLabel } from './core.mjs';

const COLUMNS = ['文件', '所属知识库', '处理状态', '已发布版本', '操作'];

export function renderFiles(app) {
  const page = el('div', 'page files-page');
  const head = el('div', 'page-head');
  const text = el('div');
  text.append(el('h1', '', '全部文件'), el('p', '', '汇总各知识库的文档与处理状态；更新与删除在所属知识库内完成。'));
  const actions = el('div', 'head-actions');
  actions.append(actionButton('刷新', 'btn ghost', () => app.loadAllFiles(true), 'refresh'));
  head.append(text, actions);
  page.append(head);

  if (!app.state.connected) {
    const cta = el('button', 'btn primary', '连接工作台');
    cta.type = 'button';
    cta.addEventListener('click', () => app.openConnection());
    page.append(emptyState('连接后查看全部文件', '使用开发令牌连接后端；汇总结果全部来自各知识库的真实接口。', cta));
    return page;
  }

  if (!app.state.libraries.length) {
    page.append(emptyState('还没有知识库', '先创建一个知识库，再上传资料。',
      actionButton('新建知识库', 'btn primary', () => app.createLibrary(), 'plus')));
    return page;
  }

  const stats = el('div');
  stats.id = 'file-stats';
  stats.append(statsRow(app));
  page.append(stats);
  // 读取失败的知识库要留在页面上，而不是一个转瞬即逝的提示：
  // 否则「只读到 1 个库」会被误读成「一共只有 1 个文件」。
  const errors = el('div');
  errors.id = 'file-errors';
  page.append(errors);
  paintErrors(app, errors);
  page.append(shoulder(app));
  // 刷新时列表保留旧数据，必须另给一条状态提示，否则看不出正在更新
  const status = el('div', 'file-status');
  status.id = 'file-status';
  status.setAttribute('aria-live', 'polite');
  page.append(status);
  const panel = el('div', 'table-wrap');
  panel.id = 'file-list';
  page.append(panel);
  // 先置加载态再画，避免首帧闪成「没有匹配的文件」；加载完成后置 fileLoaded。
  const needsLoad = !app.state.fileLoaded && !app.state.fileLoading;
  if (needsLoad) app.state.fileLoading = true;
  paint(app, panel);
  if (needsLoad) app.loadAllFiles();
  return page;
}

function statsRow(app) {
  const all = app.state.allFiles;
  const ready = all.filter(item => item.version_no).length;
  const running = all.filter(item => ['running', 'queued'].includes(item.job?.state)).length;
  const failed = all.filter(item => item.job?.state === 'failed').length;
  const row = el('div', 'stat-row');
  const partial = (app.state.fileErrors || []).length;
  [
    ['文档总数', all.length, partial
      ? `${app.state.libraries.length} 个知识库 · ${partial} 个未读取`
      : `${app.state.libraries.length} 个知识库`],
    ['可检索', ready, '已发布构建'],
    ['处理中', running, '解析 / 分段 / 向量化'],
    ['失败', failed, '可在知识库内重建'],
  ].forEach(([label, value, hint]) => {
    const card = el('div', 'stat-card');
    card.append(el('span', '', label), el('strong', '', String(value)), el('small', '', hint));
    row.append(card);
  });
  return row;
}

function shoulder(app) {
  const row = el('div', 'shoulder');
  const field = el('label', 'search-field');
  const input = el('input');
  input.type = 'search';
  input.placeholder = '按文件名筛选';
  input.setAttribute('aria-label', '按文件名筛选全部文件');
  input.value = app.state.fileFilter;
  field.append(icon('search'), input);

  const select = kbFilter(app);

  row.append(field, select, el('div', 'spacer'));

  input.addEventListener('input', () => {
    app.state.fileFilter = input.value;
    paint(app, document.querySelector('#file-list'));
  });
  return row;
}

/**
 * 知识库筛选。原生 select 在不同浏览器里的点按热区与样式都不可控，
 * 这里改成与「检索范围」一致的菜单按钮：点开即选，选中后立刻重画列表。
 */
function kbFilter(app) {
  const btn = el('button', 'filter-btn');
  btn.type = 'button';
  btn.setAttribute('aria-haspopup', 'menu');
  const current = () => app.state.libraries.find(kb => kb.id === app.state.fileKb);
  const paintLabel = () => {
    const kb = current();
    btn.replaceChildren(icon(kb ? 'library' : 'folder'),
      el('span', 'filter-label', kb ? kb.name : '全部知识库'), icon('chevron'));
    btn.title = kb ? `只看《${kb.name}》的文件` : '当前显示全部知识库的文件';
    btn.classList.toggle('active', Boolean(kb));
  };
  paintLabel();

  btn.addEventListener('click', () => {
    openMenu(btn, menu => {
      menu.append(el('div', 'menu-title', '按知识库筛选'));
      menu.append(menuItem('全部知识库', {
        glyph: 'folder',
        checked: !app.state.fileKb,
        onClick: () => {
          app.state.fileKb = '';
          paintLabel();
          paint(app, document.querySelector('#file-list'));
        },
      }));
      app.state.libraries.forEach(kb => {
        menu.append(menuItem(kb.name, {
          glyph: 'library',
          checked: app.state.fileKb === kb.id,
          onClick: () => {
            app.state.fileKb = kb.id;
            paintLabel();
            paint(app, document.querySelector('#file-list'));
          },
        }));
      });
    }, { align: 'start' });
  });
  return btn;
}

export function updateFilesView(app) {
  paint(app, document.querySelector('#file-list'));
}

function paint(app, panel) {
  if (!panel) return;
  const stats = document.querySelector('#file-stats');
  if (stats) stats.replaceChildren(statsRow(app));
  paintErrors(app);
  paintStatus(app);
  panel.replaceChildren();
  // 已有数据时刷新不清空列表：先显示旧行，避免页面闪成空白再填回来。
  if (app.state.fileLoading && !app.state.allFiles.length) {
    panel.append(loading('正在汇总各知识库的文档…'));
    return;
  }

  const keyword = app.state.fileFilter.trim().toLowerCase();
  const items = app.state.allFiles
    .filter(item => !app.state.fileKb || item.kb_id === app.state.fileKb)
    .filter(item => !keyword || (item.title || '').toLowerCase().includes(keyword));

  if (!items.length) {
    panel.append(emptyState('没有匹配的文件', '调整筛选条件，或到知识库里上传资料。'));
    return;
  }

  const table = el('table', 'document-table');
  const thead = el('thead');
  const headRow = el('tr');
  COLUMNS.forEach(title => headRow.append(el('th', '', title)));
  thead.append(headRow);
  const tbody = el('tbody');
  items.forEach(item => tbody.append(fileRow(app, item)));
  table.append(thead, tbody);

  const scroll = el('div', 'table-scroll');
  scroll.append(table);
  panel.replaceChildren(scroll);
}

/** 正在刷新时的状态行：列表此时显示的是上一次的结果。 */
function paintStatus(app) {
  const box = document.querySelector('#file-status');
  if (!box) return;
  box.replaceChildren();
  const refreshing = app.state.fileLoading && app.state.allFiles.length > 0;
  box.hidden = !refreshing;
  if (!refreshing) return;
  box.append(el('span', 'spinner'),
    el('span', '', `正在刷新 ${app.state.libraries.length} 个知识库的文档…（下方仍是上一次结果）`));
}

/** 部分知识库读取失败时的常驻提示，带手动重试。 */
function paintErrors(app, target) {
  const box = target || document.querySelector('#file-errors');
  if (!box) return;
  box.replaceChildren();
  const errors = app.state.fileErrors || [];
  if (!errors.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const names = errors.slice(0, 3).map(item => item.name).join('、');
  const more = errors.length > 3 ? ` 等 ${errors.length} 个知识库` : '';
  const line = el('div', 'notice-line warn file-error-line');
  line.append(icon('alert'), el('span', '',
    `部分知识库读取失败：${names}${more}。下面的统计与列表只包含已读取的部分，不代表全部文件。`));
  const retry = el('button', 'link-btn', '重新读取');
  retry.type = 'button';
  retry.addEventListener('click', () => app.loadAllFiles());
  line.append(retry);
  box.append(line);
}

function fileRow(app, item) {
  const tr = el('tr');

  const nameCell = el('td');
  const cell = el('div', 'file-cell');
  const mark = el('span', 'file-ico');
  mark.append(icon('file'));
  const meta = el('div', 'file-meta');
  const name = el('button', 'link-btn file-name', item.title);
  name.type = 'button';
  name.title = '打开文件正文';
  name.addEventListener('click', () => app.openBrowse(item));
  meta.append(name);
  meta.append(el('small', 'file-sub', `代次 ${item.generation}${item.active_build_id ? ' · 已有可用内容' : ' · 暂无发布内容'}`));
  cell.append(mark, meta);
  nameCell.append(cell);

  const kbCell = el('td');
  const open = el('button', 'link-btn');
  open.type = 'button';
  open.textContent = item.kb_name || '未知知识库';
  open.addEventListener('click', () => app.openLibraryById(item.kb_id));
  kbCell.append(open);

  const statusCell = el('td');
  const badge = el('span', `status ${item.job?.state || ''}`);
  badge.append(el('i'), document.createTextNode(jobLabel(item)));
  statusCell.append(badge);

  const versionCell = el('td', 'version-cell');
  versionCell.append(document.createTextNode(item.version_no ? `V${item.version_no}` : '尚未发布'));

  const actionCell = el('td');
  const bar = el('div', 'row-actions');
  const browse = el('button', 'link-btn', '浏览');
  browse.type = 'button';
  browse.addEventListener('click', () => app.openBrowse(item));
  const chunks = el('button', 'link-btn', '分块');
  chunks.type = 'button';
  chunks.addEventListener('click', () => app.openChunks(item));
  const jump = el('button', 'link-btn', '所在知识库');
  jump.type = 'button';
  jump.addEventListener('click', () => app.openLibraryById(item.kb_id));
  bar.append(browse, chunks, jump);
  actionCell.append(bar);

  tr.append(nameCell, kbCell, statusCell, versionCell, actionCell);
  return tr;
}
