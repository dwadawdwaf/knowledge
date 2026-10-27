// 检索测试：只调用 /search，不触发生成模型。
import { el, icon, toast, emptyState, loading, autoGrow } from './dom.mjs';
import { scopeControl, scopeIds } from './scope.mjs';
import { locatorText, warningLabel } from './core.mjs';

const MODES = [
  ['hybrid', '混合检索'],
  ['semantic', '语义检索'],
  ['keyword', '关键词检索'],
];

export function renderSearch(app) {
  const page = el('div', 'page narrow');

  const head = el('div', 'page-head');
  const text = el('div');
  text.append(el('h1', '', '检索测试'), el('p', '', '先确认资料能被正确召回，再判断回答是否有据可依。'));
  const actions = el('div', 'head-actions');
  actions.append(scopeControl(app));
  head.append(text, actions);
  page.append(head);

  const tabs = el('div', 'tabs');
  const tab = el('button', 'tab active', '片段检索');
  tab.type = 'button';
  tabs.append(tab);
  page.append(tabs);

  const form = el('form', 'query-box');
  const input = el('textarea');
  input.placeholder = '输入一个典型问题，检查资料的召回效果…';
  input.maxLength = 2000;
  input.required = true;
  input.setAttribute('aria-label', '检索问题');
  input.value = app.state.query || '';
  input.addEventListener('input', () => { app.state.query = input.value; autoGrow(input, 200); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  const bar = el('div', 'query-bar');
  bar.append(el('span', 'hint', 'Enter 检索 · Shift + Enter 换行'));

  const mode = el('select', 'mini-select');
  mode.setAttribute('aria-label', '检索方式');
  MODES.forEach(([value, label]) => {
    const option = el('option', '', label);
    option.value = value;
    mode.append(option);
  });
  mode.value = app.state.mode;

  const spacer = el('div', 'spacer');
  const send = el('button', 'send-btn');
  send.type = 'submit';
  send.setAttribute('aria-label', '开始检索');
  send.append(icon('search'));
  bar.append(mode, spacer, send);
  form.append(input, bar);
  page.append(form);

  const results = el('div', 'results');
  results.id = 'search-results';
  page.append(results);

  if (app.state.searchResult) {
    paintResults(app, results, app.state.searchResult);
  } else {
    results.append(emptyState('检查每一次命中的依据', '检索后可查看片段、来源与排序分数；分数只在同一检索方式内可比。'));
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) return;
    if (!app.state.connected) { app.openConnection(); return; }
    const ids = scopeIds(app);
    if (!ids.length) { toast('还没有可检索的知识库。'); return; }
    if (app.state.busy) { toast('上一次检索还在进行，请稍候。'); return; }

    app.state.mode = mode.value;
    app.state.busy = true;
    send.disabled = true;
    results.replaceChildren(loading('正在检索资料…'));
    try {
      const payload = await app.api.request('/search', {
        method: 'POST',
        body: { query, knowledge_base_ids: ids, mode: mode.value, top_k: 5 },
      });
      app.state.searchResult = payload;
      paintResults(app, results, payload);
    } catch (error) {
      app.state.searchResult = null;
      results.replaceChildren(app.errorBox(error));
    } finally {
      app.state.busy = false;
      send.disabled = false;
    }
  });

  queueMicrotask(() => autoGrow(input, 200));
  return page;
}

function paintResults(app, container, result) {
  container.replaceChildren();
  const hits = result.hits || [];
  const summary = el('div', 'result-summary');
  summary.append(el('span', '', `找到 ${hits.length} 个片段`));
  const chip = el('span', 'chip', MODES.find(([value]) => value === result.mode)?.[1] || result.score_type || result.mode);
  summary.append(chip);
  if (result.score_type) summary.append(el('span', 'chip', result.score_type));
  if (result.degraded) summary.append(el('span', 'chip', '已降级'));
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

  hits.forEach((hit, index) => {
    const card = el('article', 'result-card');
    const head = el('div', 'result-head');
    const title = el('button', 'result-title');
    title.type = 'button';
    title.append(icon('file'), el('span', '', `${index + 1}. ${hit.title}`));
    title.addEventListener('click', () => app.openSource(hit.chunk_id));
    const score = el('span', 'result-score', `${hit.score_type || result.score_type} · ${Number(hit.score).toFixed(4)}`);
    head.append(title, score);
    card.append(head);
    if (hit.excerpt) card.append(el('p', 'result-excerpt', hit.excerpt));
    const location = el('div', 'source-location');
    location.append(icon('quote'), el('span', '', locatorText(hit.locator)));
    card.append(location);
    container.append(card);
  });
}
