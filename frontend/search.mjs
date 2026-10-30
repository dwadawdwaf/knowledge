// 检索测试：只调用 /search，不触发生成模型。
// 版面按「条件面板 + 结果流」两栏组织：条件固定在左，结果占满剩余宽度。
import { el, icon, toast, emptyState, loading, autoGrow } from './dom.mjs';
import { scopeControl, scopeIds } from './scope.mjs';
import { locatorText, warningLabel } from './core.mjs';

const MODES = [
  ['hybrid', '混合检索', '关键词与语义各召回一路，再融合排序'],
  ['semantic', '语义检索', '按向量距离找同义表述，可跨措辞命中'],
  ['keyword', '关键词检索', '按字面相似度命中，适合核对专有名词'],
];

const GUIDE = [
  ['quote', '命中片段', '截取命中的那段原文，可直接复制或跳到文件定位'],
  ['layers', '排序分数', '每条结果给出本次得分与名次，用于判断召回是否合理'],
  ['info', '同一方式内可比', '三种检索方式的刻度不同，分数只在同一种方式内比较'],
];

// 示例问法取自随仓库提供的演示资料，点一下即可填入并检索。
const EXAMPLES = [
  '会议室使用后需要做什么？',
  '差旅住宿费按什么标准报销？',
  '新员工入职要准备哪些材料？',
];

export function renderSearch(app, { embedded = false } = {}) {
  const page = el('div', 'page search-page');

  const head = el('div', 'page-head');
  const text = el('div');
  text.append(el('h1', '', '资料检索'));
  head.append(text);
  if (!embedded) page.append(head);
  else page.classList.add('embedded-search');

  const layout = el('div', 'search-layout');
  layout.append(consolePanel(app, embedded));
  const results = el('div', 'results search-results');
  results.id = 'search-results';
  layout.append(results);
  page.append(layout);

  if (app.state.searchResult) paintResults(app, results, app.state.searchResult);
  else {
    results.append(guide());
    const scope = embedded ? null : scopeSummary(app);
    if (scope) results.append(scope);
  }

  queueMicrotask(() => autoGrow(document.querySelector('#search-query'), 160));
  return page;
}

/** 检索条件：独占一行，方式与范围并排；结果在下方纵向排列。 */
function consolePanel(app, embedded) {
  const panel = el('section', 'search-panel');

  const top = el('div', 'search-panel-head');
  top.append(el('span', 'search-panel-title', '检索条件'), el('span', 'count-note', '取前 5 条'));
  panel.append(top);

  const form = el('form', 'query-box');
  const input = el('textarea');
  input.id = 'search-query';
  input.placeholder = '输入一个典型问题，检查资料的召回效果…';
  input.maxLength = 2000;
  input.required = true;
  input.setAttribute('aria-label', '检索问题');
  input.value = app.state.query || '';
  input.addEventListener('input', () => {
    app.state.query = input.value;
    autoGrow(input, 160);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  const bar = el('div', 'query-bar');
  bar.append(el('span', 'hint', 'Enter 检索 · Shift + Enter 换行'));
  const send = el('button', 'btn primary search-submit');
  send.type = 'submit';
  send.append(icon('search'), document.createTextNode('检索'));
  bar.append(el('div', 'spacer'), send);
  form.append(input, bar);
  panel.append(form);

  const modeBlock = el('div', 'field-block');
  modeBlock.append(el('span', 'field-label', '检索方式'));
  const segmented = el('div', 'segmented');
  segmented.setAttribute('role', 'radiogroup');
  segmented.setAttribute('aria-label', '检索方式');
  const buttons = new Map();
  MODES.forEach(([value, label, hint]) => {
    const button = el('button', 'seg', label);
    button.type = 'button';
    button.title = hint;
    button.setAttribute('role', 'radio');
    button.dataset.mode = value;
    button.addEventListener('click', () => {
      app.state.mode = value;
      paintModes();
    });
    buttons.set(value, button);
    segmented.append(button);
  });
  const paintModes = () => {
    buttons.forEach((button, value) => {
      const active = value === app.state.mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-checked', String(active));
    });
  };
  paintModes();
  modeBlock.append(segmented);
  const modeHint = el('p', 'field-hint', MODES.find(([value]) => value === app.state.mode)?.[2] || '');
  modeBlock.append(modeHint);

  const scopeBlock = el('div', 'field-block');
  scopeBlock.append(el('span', 'field-label', '知识范围'));
  scopeBlock.append(embedded ? el('span', 'chip', app.state.library.name) : scopeControl(app));

  // 方式与范围并排，减少条件区高度；窄屏由 CSS 退回单列。
  const options = el('div', 'search-options');
  options.append(modeBlock, scopeBlock);
  panel.append(options);

  const foot = el('div', 'search-foot');
  foot.append(icon('info'),
    el('p', '', '分数只在同一种检索方式内可比：混合、语义、关键词的刻度不同。'));
  panel.append(foot);

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    if (!app.state.connected) { app.openConnection(); return; }
    const ids = embedded ? [app.state.library.id] : scopeIds(app);
    if (!ids.length) { toast('还没有可检索的知识库。'); return; }
    if (app.state.busy) { toast('上一次检索还在进行，请稍候。'); return; }

    const results = document.querySelector('#search-results');
    app.state.busy = true;
    send.disabled = true;
    if (results) results.replaceChildren(loading('正在检索资料…'));
    try {
      const payload = await app.api.request('/search', {
        method: 'POST',
        body: { query, knowledge_base_ids: ids, mode: app.state.mode, top_k: 5 },
      });
      app.state.searchResult = payload;
      app.state.query = query;
      if (results) paintResults(app, results, payload);
    } catch (error) {
      app.state.searchResult = null;
      if (results) results.replaceChildren(app.errorBox(error));
    } finally {
      app.state.busy = false;
      send.disabled = false;
    }
  });

  // 切换方式后说明文字跟着变，不重绘整页。
  segmented.addEventListener('click', () => {
    modeHint.textContent = MODES.find(([value]) => value === app.state.mode)?.[2] || '';
  });

  return panel;
}

/**
 * 检索范围概览：列出这次会被检索的知识库。
 * 只做展示与快捷切换，计数直接取已加载的知识库列表，不额外请求。
 */
function scopeSummary(app) {
  const libraries = app.state.libraries || [];
  if (!libraries.length) return null;

  const card = el('section', 'scope-card');
  const head = el('div', 'scope-card-head');
  head.append(el('h3', '', '本次检索覆盖的资料'));
  const inScope = scopeIds(app).length;
  head.append(el('span', 'count-note', `${inScope} / ${libraries.length} 个知识库`));
  card.append(head);
  card.append(el('p', '', '点一下即可把范围收窄到单个知识库，再点一次恢复全部。'));

  const list = el('div', 'scope-list');
  const shown = libraries.slice(0, 8);
  shown.forEach(kb => {
    const item = el('button', app.state.scope === kb.id ? 'scope-item active' : 'scope-item');
    item.type = 'button';
    item.title = kb.name;
    item.append(icon('library'), el('span', '', kb.name));
    item.addEventListener('click', () => {
      app.state.scope = app.state.scope === kb.id ? '' : kb.id;
      app.render();
    });
    list.append(item);
  });
  if (libraries.length > shown.length) {
    list.append(el('span', 'scope-more', `另有 ${libraries.length - shown.length} 个`));
  }
  card.append(list);
  return card;
}

/** 首次进入时的说明：把「能看什么」讲清楚，而不是丢一句孤零零的提示。 */
function guide(app) {
  const box = el('div', 'search-guide');
  box.append(el('h3', '', '检索后这里会给出'));
  box.append(el('p', '', '输入问题并选择检索方式，就能逐条核对召回结果是否站得住。'));
  const list = el('div', 'guide-list');
  GUIDE.forEach(([glyph, title, description]) => {
    const item = el('div', 'guide-item');
    const mark = el('span', 'g-ico');
    mark.append(icon(glyph));
    const body = el('div');
    body.append(el('strong', '', title), el('span', '', description));
    item.append(mark, body);
    list.append(item);
  });
  box.append(list);

  // 给几个可直接点用的问法，省去「不知道问什么」，也顺便验证召回是否正常。
  const samples = el('div', 'guide-samples');
  samples.append(el('span', 'guide-samples-label', '示例问题'));
  EXAMPLES.forEach(question => {
    const chip = el('button', 'sample-chip', question);
    chip.type = 'button';
    chip.addEventListener('click', () => {
      const input = document.querySelector('#search-query');
      if (!input) return;
      input.value = question;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    });
    samples.append(chip);
  });
  box.append(samples);
  return box;
}

function paintResults(app, container, result) {
  container.replaceChildren();
  const hits = result.hits || [];

  const summary = el('div', 'result-summary');
  const asked = (app.state.query || '').trim();
  if (asked) summary.append(el('span', 'summary-query', `“${asked}”`));
  summary.append(el('strong', '', `${hits.length} 个片段`));
  const chip = el('span', 'chip', MODES.find(([value]) => value === result.mode)?.[1] || result.score_type || result.mode);
  summary.append(chip);
  if (result.score_type) summary.append(el('span', 'chip mono', result.score_type));
  if (result.degraded) summary.append(el('span', 'chip warn', '已降级'));
  container.append(summary);

  if (result.degraded) {
    const line = el('div', 'notice-line warn');
    line.append(icon('alert'), el('span', '', '本次检索发生降级，命中可能不完整，请结合后端说明核查。'));
    container.append(line);
  }
  (result.warnings || []).forEach(warning => {
    const line = el('div', 'notice-line warn');
    line.append(icon('alert'), el('span', '', warningLabel(warning)));
    container.append(line);
  });

  if (!hits.length) {
    container.append(emptyState('未找到相关片段', '检查资料处理状态、知识范围，或换一种提问方式。'));
    return;
  }

  const list = el('div', 'result-list');
  hits.forEach((hit, index) => list.append(card(app, hit, index, result)));
  container.append(list);
}

function card(app, hit, index, result) {
  const node = el('article', 'result-card');
  const head = el('div', 'result-head');

  const rank = el('span', 'result-rank', String(index + 1));
  const title = el('button', 'result-title');
  title.type = 'button';
  title.title = `打开《${hit.title}》并定位到本段`;
  title.append(el('span', '', hit.title));
  title.addEventListener('click', () => app.openSource(hit.chunk_id));

  const score = el('span', 'result-score', `${hit.score_type || result.score_type} · ${Number(hit.score).toFixed(4)}`);
  const kb = el('button', 'result-kb');
  kb.type = 'button';
  kb.title = hit.kb_name ? `所属知识库：${hit.kb_name}` : '未知知识库';
  kb.append(icon('library'), el('span', '', hit.kb_name || '未知知识库'));
  kb.addEventListener('click', () => {
    if (hit.kb_id) app.openLibraryById(hit.kb_id);
  });
  head.append(rank, title, kb, el('div', 'spacer'), score);
  node.append(head);

  if (hit.excerpt) node.append(el('p', 'result-excerpt', hit.excerpt));

  const foot = el('div', 'result-foot');
  const location = el('span', 'source-location');
  location.append(icon('quote'), el('span', '', locatorText(hit.locator)));
  foot.append(location);
  const browse = el('button', 'link-btn', '在文件中查看');
  browse.type = 'button';
  browse.addEventListener('click', () => app.openSource(hit.chunk_id));
  foot.append(browse);
  node.append(foot);
  return node;
}
